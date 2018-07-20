#!/usr/bin/env python
import mongoclusterbackup

if __name__ == '__main__':
    mongos = '127.0.0.1:27017'
    hosts = {
        # dictionary of 'hostname': 'logical volume'.
        # obviously, volumes should be the ones your mongodb data is on.
        'host1.example.com': {'lvol': '/dev/vg0/mongo', 'backup_dir': '/backup', 'mount_point':'/mongodbdata_snapshot'},
        'host2.example.com': {'lvol': '/dev/vg0/mongo', 'backup_dir': '/backup', 'mount_point':'/mongodbdata_snapshot'},
        'host3.example.com': {'lvol': '/dev/vg0/mongo', 'backup_dir': '/backup', 'mount_point':'/mongodbdata_snapshot'},
        'host4.example.com': {'lvol': '/dev/vg0/root', 'backup_dir': '/backup', 'mount_point':'/mongodbdata_snapshot'},
    }
    # path to configuration server backup on the configuration server itself.
    config_basedir = '/usr/local/backup/mongo-config'
    backup = mongoclusterbackup.BackupCluster(mongos, hosts, config_basedir)
    backup.backup()

    # Code your custom logic for actually copying the data from your servers
    # here. All information about the cluster is available in `backup` object.
    # Do not forget to remove the LVM snapshots after you're done.

