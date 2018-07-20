#!/usr/bin/env python
import logging
import pymongo
import time
import subprocess
import random
import os
import datetime
import threading
import Queue

""" Create consistent backup of your mongodb sharded cluster.

    This basically follows mongodb shard backup procedure described in mongodb
    documentation (http://bit.ly/11uNuYa). It:
        - stops cluster balancer;
        - stops one of configuration servers to prevent metadata changes;
        - backs up configuration database;
        - locks all shards. If your shards are replicasets, only one of the
          secondary servers will be locked;
        - creates LVM snapshots on the servers;
        - unlocks the shards;
        - starts the configuration server and enables the balancer.

    This ensures that consistent cluster data is present on the servers. You
    will still have to copy that data to the backup medium of your choice and
    remove LVM snapshots.

    This expects that the machine you run this on has a passwordless ssh
    access to configuration servers and shards.

"""

logging.basicConfig(format="%(asctime)-15s %(message)s", level=logging.INFO)

class BackupAbortedException(Exception): pass


class BackupMongo:

    """ Superclass for all mongo servers """

    def __init__(self, host):
        self.host = host
        logging.info("Initializing %s(%s)" % (self.__class__, self.host))
        self.client = pymongo.MongoClient(self.host, network_timeout=5)


class SSHAble():

    """ Superclass for all ssh-able objects """

    def run(self, command, timeout=120, capture_output=False):
        cmd = ("timeout %d ssh -o StrictHostKeyChecking=no"
               " -o ConnectTimeout=60 -o ServerAliveInterval=20 -l root"
               " %s '%s'" % (timeout, self.host, command))
        logging.info("> %s" % cmd)
        if capture_output:
            return subprocess.check_output(cmd, shell=True)
        else:
            return subprocess.call(cmd, shell=True)

class BackupMongos(BackupMongo):

    """ Mongos instance. We will use it to stop/start balancer and wait for
        any locks.
    """

    def get_shards(self):
        shards = []
        for shard in self.client['config']['shards'].find():
            if '/' in shard['host']:
                # This shard is a replicaset. Connect to it and find a healthy
                # secondary host with minimal replication lag
                hosts = shard['host'].split('/')[1]
                with pymongo.MongoClient(hosts.split(',')) as connection:
                    rs = connection['admin'].command("replSetGetStatus")
                    good_secondaries = [member for member in rs['members']
                            if member['state'] == 2 and int(member['health'])]
                    if len(good_secondaries):
                        best = sorted(good_secondaries,
                                      key=lambda x: x['optimeDate'],
                                      reverse=True)[0]
                        shards.append(best['name'])
                    else:
                        # no healthy secondaries found, try to find the master
                        master = [member for member in rs['members']
                                  if member['state'] == 1][0]
                        shards.append(master['name'])
            else:
                # standalone server rather than a replicaset
                shards.append(shard['host'])
        return shards

    def get_locks(self):
        return [lock
                for lock in self.client['config']['locks'].find({"state": 2})]

    def balancer_stopped(self):
        return self.client['config']['settings'].find({
            "_id": "balancer"
        })[0]["stopped"]

    def stop_balancer(self):
        logging.info("Stopping balancer")
        self.client['config']['settings'].update(
            {"_id":"balancer"}, {"$set": { "stopped": True }})
        if not self.balancer_stopped():
            raise Exception("Could not stop balancer")

    def start_balancer(self):
        logging.info("Starting balancer")
        self.client['config']['settings'].update(
            {"_id":"balancer"}, {"$set": { "stopped": False }})
        if self.balancer_stopped():
            raise Exception("Could not start balancer")

    def get_config_servers(self):
        cmd_line_opts = self.client['admin'].command('getCmdLineOpts')
        servers = cmd_line_opts['parsed']['configdb'].split(',')
        random.shuffle(servers)
        return servers


class BackupShard(BackupMongo):

    """ A specific shard server. We will be locking/unlocking these for backup
        data to be consistent.
    """

    def lock(self, errors):
        logging.info("Locking shard %s" % self.host)
        self.client.fsync(lock=True)
        if self.is_locked:
            logging.info("Locked shard %s" % self.host)
        else:
            err = "Cannot lock shard %s" % self.host
            logging.error(err)
            errors.put(err)

    def unlock(self):
        logging.info("Unlocking shard %s" % self.host)
        self.client.unlock()
        if self.is_locked():
            raise Exception("Cannot unlock shard %s" % self.host)

    def is_locked(self):
        return self.client.is_locked


class BackupConfigServer(SSHAble):

    """ Configuration server that is going to be stopped to prevent any
        metadata changes to the cluster. Also, it will be used to backup
        `config` database.
    """

    def __init__(self, host, backup_path):
        self.host = host
        self.backup_path = backup_path
        logging.info("Initializing BackupConfigServer(%s)" % self.host)
        if not self.is_running():
            raise BackupAbortedException("MongoDB is not running on %s" %
                                         self.host)

    def is_running(self):
        check_mongod = self.run("/etc/init.d/mongodb status")
        return check_mongod == 0

    def stop(self):
        logging.info("Stopping mongo configuration server on %s" % self.host)
        stop_mongod = self.run("/etc/init.d/mongodb stop")
        time.sleep(3)
        if self.is_running():
            raise Exception("Could not stop config server on %s" % self.host)

    def start(self):
        logging.info("Starting mongo configuration server on %s" % self.host)
        start_mongod = self.run("/etc/init.d/mongodb start")
        time.sleep(3)
        if not self.is_running():
            raise Exception("Could not start config server on %s" % self.host)

    def mongodump(self):
        """ Dump config database using mongodump. """
        ret = self.run("mkdir -p %s" % self.backup_path)
        if ret != 0:
            raise Exception("Error dumping config database")
        ret = self.run("mongodump -d config -o %s" % self.backup_path)
        if ret != 0:
            raise Exception("Error dumping config database")

class BackupHost(SSHAble):

    """ Physical server that we will be creating LVM snapshots on. """

    def __init__(self, host, lvol, backup_dir, mount_point):
        logging.info("Initializing BackupHost(%s, %s)" % (host, lvol))
        self.host = host
        self.lvol = lvol
        self.backup_dir = backup_dir
        self.mount_point = mount_point
        self.snapshot_path = ''
        check_lvol = self.run("lvdisplay %s > /dev/null" % self.lvol)
        if check_lvol != 0:
            raise BackupAbortedException("Cannot find logical volume %s on %s" %
                                         (self.lvol, self.host))

    def create_snapshot(self, backup_id, errors):
        """ Create LVM snapshot

            The snapshot size is currently hardcoded to 100% of free extents
            available in a given volume group.

            This function will be executed in a separate thread, so rather than
            raising any exceptions it is supposed to push any error messages
            into the `errors` queue for the main thread to act upon.
        """
        logging.info("Creating a snapshot %s on %s" % (backup_id, self.host))
        create_snap = self.run("lvcreate --snapshot %s --name '%s' --extents"
                               " '100%%free'" % (self.lvol, backup_id))
        if create_snap == 0:
            logging.info("Created snapshot %s on %s" % (backup_id, self.host))
            self.snapshot_path = self.run("lvdisplay|grep 'LV Path' |grep %s |awk '{print \$NF}'" % (backup_id), capture_output=True)
        else:
            err = "Cannot create snapshot %s at %s" % (backup_id, self.host)
            logging.error(err)
            errors.put(err)

    def mount_snapshot(self, backup_id, errors):
        """ Mount snapshot to mount_point

            This function will be executed in a separate thread, so rather than
            raising any exceptions it is supposed to push any error messages
            into the `errors` queue for the main thread to act upon.
        """
        if self.snapshot_path == '':
            err = "Snapshot %s was not created on %s" % (backup_id, self.host)
            logging.err()
            errors.put(err)
        else:
            logging.info("Mounting %s to %s on %s" % (self.snapshot_path, self.mount_point, self.host))
            mount_snap = self.run("mount %s %s" % (self.snapshot_path, self.mount_point))
            if mount_snap == 0:
                logging.info("Snapshot %s mounted to %s on %s" % (backup_id, self.mount_point, self.host))
            else:
                err = "Cannot mount snapshot %s mounted to %s on %s" % (backup_id, self.mount_point, self.host)
                logging.error(err)
                errors.put(err)

    def take_tar_backup(self, backup_id, errors):
        """ Create Backup of LVM snapshot using tar to directory specified as backup_dir

            This function will be executed in a separate thread, so rather than
            raising any exceptions it is supposed to push any error messages
            into the `errors` queue for the main thread to act upon.
        """

        logging.info("Begin tar %s to %s/%s.tar on %s" % (self.mount_point, self.backup_dir, backup_id, self.host))
        tar_snap = self.run("tar -cvf %s/%s.tar %s" % (self.backup_dir, backup_id, self.mount_point))
        if tar_snap == 0:
            logging.info("Tar %s completed sucessfully on %s" % (self.mount_point, self.host))
        else:
            err = "Cannot complete tar of %s on %s" % (self.mount_point, self.host)
            logging.error(err)
            errors.put(err)

    def unmount_snapshot(self, backup_id, errors):
        """ Mount snapshot to mount_point

            This function will be executed in a separate thread, so rather than
            raising any exceptions it is supposed to push any error messages
            into the `errors` queue for the main thread to act upon.
        """
        logging.info("Unmounting %s on %s" % (self.mount_point, self.host))
        unmount_snap = self.run("umount -f %s" % (self.mount_point))
        if unmount_snap == 0:
            logging.info("Snapshot %s unmounted from %s on %s" % (backup_id, self.mount_point, self.host))
        else:
            err = "Cannot unmount snapshot %s from %s on %s" % (backup_id, self.mount_point, self.host)
            logging.error(err)
            errors.put(err)

    def remove_snapshot(self, backup_id, errors):
        """ Remove LVM snapshot

            This function will be executed in a separate thread, so rather than
            raising any exceptions it is supposed to push any error messages
            into the `errors` queue for the main thread to act upon.
        """
        logging.info("Removing a snapshot %s on %s" % (backup_id, self.host))
        create_snap = self.run("lvremove -f %s" % (backup_id))
        if create_snap == 0:
            logging.info("Snapshot %s removed from %s" % (backup_id, self.host))
        else:
            err = "Cannot remove snapshot %s from %s" % (backup_id, self.host)
            logging.error(err)
            errors.put(err)


class BackupCluster:

    """ Main class that does all the hard work """

    def __init__(self, mongos, hosts, config_basedir):
        """ Initialize all children objects, checking input parameters
            sanity and verifying connections to various servers/services
        """
        logging.info("Initializing BackupCluster")
        self.backup_id = self.generate_backup_id()
        logging.info("Backup ID is %s" % self.backup_id)
        self.hosts = [BackupHost(host, hosts[host]['lvol'],hosts[host]['backup_dir'],hosts[host]['mount_point']) for host in hosts]
        self.mongos = BackupMongos(mongos)
        self.shards = [BackupShard(host) for host in self.mongos.get_shards()]
        self.config_server = BackupConfigServer(
                                sorted(self.mongos.get_config_servers())[0],
                                os.path.join(config_basedir,
                                             self.backup_id),
                             )
        self.rollback_steps = []

    def generate_backup_id(self):
        """ Generate unique time-based backup ID that will be used both for
            configuration server backups and LVM snapshots of shard servers
        """
        ts = datetime.datetime.now()
        return ts.strftime('%Y%m%d-%H%M%S')

    def wait_for_locks(self):
        """ Loop until all shard locks are released.
            Give up after 30 minutes.
        """
        retries = 0
        while len(self.mongos.get_locks()) and retries < 360:
            logging.info("Waiting for locks to be released: %s" %
                         self.mongos.get_locks())
            time.sleep(5)
            retries += 1

        if len(self.mongos.get_locks()):
            raise Exception("Something is still locking the cluster,"
                            " aborting backup")

    def lock_shards(self):
        """ Lock all shards. As we would like to minimize the amount of time
            the cluster stays locked, we lock each shard in a separate thread.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for shard in self.shards:
            t = threading.Thread(target=shard.lock, args=(errors,))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def create_snapshots(self):
        """ Create LVM snapshots on the hosts. The cluster is supposed to be
            in a locked state at this point, so we use a separate thread for
            each server to create all the snapshots as fast as possible.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for host in self.hosts:
            t = threading.Thread(target=host.create_snapshot,
                                 args=(self.backup_id, errors))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def unlock_shards(self):
        """ Unlock the shards. This should be pretty fast, so we don't play the
            threading game here, choosing simplicity over speed.
        """
        exceptions = []
        for shard in self.shards:
            try:
                shard.unlock()
            except Exception as e:
                # try to unlock as many shards as possible before throwing an
                # exception
                exceptions += str(e)
        if len(exceptions):
            raise Exception(", ".join(exceptions))

    def mount_snapshots(self):
        """ Mount LVM snapshots to the mount_points on the hosts. The cluster is supposed to be
            in a locked state at this point, so we use a separate thread for
            each server to create all the snapshots as fast as possible.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for host in self.hosts:
            t = threading.Thread(target=host.mount_snapshot,
                                 args=(self.backup_id, errors))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def take_tar_backups(self):
        """ Take tar backup from LVM snapshots on the hosts to backup_dirs. The cluster is supposed to be
            in a locked state at this point, so we use a separate thread for
            each server to create all the snapshots as fast as possible.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for host in self.hosts:
            t = threading.Thread(target=host.take_tar_backup,
                                 args=(self.backup_id, errors))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def unmount_snapshots(self):
        """ Unmount LVM snapshots to the mount_points on the hosts. The cluster is supposed to be
            in a locked state at this point, so we use a separate thread for
            each server to create all the snapshots as fast as possible.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for host in self.hosts:
            t = threading.Thread(target=host.unmount_snapshot,
                                 args=(self.backup_id, errors))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def remove_snapshots(self):
        """ Remove LVM snapshots from the hosts. The cluster is supposed to be
            in a locked state at this point, so we use a separate thread for
            each server to create all the snapshots as fast as possible.
            The queue is used to pass any errors back from the worker threads.
        """
        errors = Queue.Queue()
        threads = []
        for host in self.hosts:
            t = threading.Thread(target=host.remove_snapshot,
                                 args=(self.backup_id, errors))
            threads.append(t)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not errors.empty():
            # We don't really care for all errors, so just through the first one
            raise Exception(errors.get())

    def run_step(self, function, tries=1):
        """ Try executing a function, retrying up to `tries` times if it fails
            with an exception. If the function fails after all tries, roll back
            all the changes - basically, execute all steps from rollback_steps
            ignoring any exceptions. Hopefully, this should bring the cluster
            back into pre-backup state.
        """
        for i in range(tries):
            try:
                logging.debug("Running %s (try #%d)" % (function, i+1))
                function()
                break
            except Exception as e:
                logging.info("Got an exception (%s) while running %s" %
                             (e, function))
                if (i == tries-1):
                    logging.info("Rolling back...")
                    for step in self.rollback_steps:
                        try:
                            step()
                        except (Exception, pymongo.errors.OperationFailure) as e:
                            logging.info("Got an exception (%s) while rolling"
                                         " back (step %s). Ignoring" %
                                         (e, step))
                    raise BackupAbortedException
                time.sleep(2)  # delay before re-trying

    def backup(self):
        """ This is basically a runlist of all steps required to backup a
            cluster. Before executing each step, a corresponding rollback
            function is pushed into the rollback_steps array to be able to
            get cluster back into production state in case something goes
            wrong.
        """
        self.rollback_steps.insert(0, self.mongos.start_balancer)
        self.run_step(self.mongos.stop_balancer, 2)

        self.run_step(self.wait_for_locks)

        self.rollback_steps.insert(0, self.config_server.start)
        self.run_step(self.config_server.stop)

        self.run_step(self.config_server.mongodump, 3)

        self.rollback_steps.insert(0, self.unlock_shards)
        self.run_step(self.lock_shards)

        self.rollback_steps.insert(0, self.remove_snapshots)
        self.run_step(self.create_snapshots)

        self.rollback_steps.remove(self.unlock_shards)
        self.run_step(self.unlock_shards, 2)

        self.rollback_steps.remove(self.config_server.start)
        self.run_step(self.config_server.start, 2)

        self.rollback_steps.remove(self.mongos.start_balancer)
        self.run_step(self.mongos.start_balancer, 4)  # it usually starts on
                                                      # the second try

        self.rollback_steps.insert(0, self.unmount_snapshots)
        self.run_step(self.mount_snapshots)

        self.run_step(self.take_tar_backups)

        self.rollback_steps.remove(self.unmount_snapshots)
        self.run_step(self.unmount_snapshots)

        self.rollback_steps.remove(self.remove_snapshots)
        self.run_step(self.remove_snapshots)

        logging.info("Finished successfully")
