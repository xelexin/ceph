import logging
import os
import errno
import time
import tempfile
from ceph_volume import conf, terminal, process
from ceph_volume.util import prepare as prepare_utils
from ceph_volume.util import system, disk
from ceph_volume.util import encryption as encryption_utils
from typing import Dict, Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from ceph_volume.api.lvm import Volume


logger = logging.getLogger(__name__)


class BaseObjectStore:
    def __init__(self, args: "argparse.Namespace") -> None:
        self.args: "argparse.Namespace" = args
        # FIXME we don't allow re-using a keyring, we always generate one
        # for the OSD, this needs to be fixed. This could either be a file (!)
        # or a string (!!) or some flags that we would need to compound
        # into a dict so that we can convert to JSON (!!!)
        self.secrets: Dict[str, str] = {'cephx_secret': prepare_utils.create_key()}
        self.cephx_secret: str = self.secrets.get('cephx_secret',
                                                  prepare_utils.create_key())
        self.encrypted: int = 0
        self.tags: Dict[str, Any] = {}
        self.osd_id: str = ''
        self.osd_fsid: str = ''
        self.cephx_lockbox_secret: str = ''
        self.objectstore: str = getattr(args, "objectstore", '')
        self.osd_mkfs_cmd: List[str] = []
        self.block_device_path: str = ''
        self.dmcrypt_key: str = encryption_utils.create_dmcrypt_key()
        self.with_tpm: int = int(getattr(self.args, 'with_tpm', False))
        self.method: str = ''
        self.osd_path: str = ''
        self.key: Optional[str] = None
        self.block_device_path: str = ''
        self.wal_device_path: str = ''
        self.db_device_path: str = ''
        self.block_lv: Optional[Volume] = None
        if getattr(self.args, 'dmcrypt', False):
            self.encrypted = 1
            if not self.with_tpm:
                self.cephx_lockbox_secret = prepare_utils.create_key()
                self.secrets['cephx_lockbox_secret'] = \
                    self.cephx_lockbox_secret

    def get_ptuuid(self, argument: str) -> str:
        uuid = disk.get_partuuid(argument)
        if not uuid:
            terminal.error('blkid could not detect a PARTUUID for device: %s' %
                           argument)
            raise RuntimeError('unable to use device')
        return uuid

    def get_osdspec_affinity(self) -> str:
        return os.environ.get('CEPH_VOLUME_OSDSPEC_AFFINITY', '')

    def pre_prepare(self) -> None:
        raise NotImplementedError()

    def prepare_data_device(self,
                            device_type: str,
                            osd_uuid: str) -> Optional["Volume"]:
        raise NotImplementedError()

    def safe_prepare(self, args: Optional["argparse.Namespace"] = None) -> None:
        raise NotImplementedError()

    def add_objectstore_opts(self) -> None:
        """
        Create the files for the OSD to function. A normal call will look like:

            ceph-osd --cluster ceph --mkfs --mkkey -i 0 \
                    --monmap /var/lib/ceph/osd/ceph-0/activate.monmap \
                    --osd-data /var/lib/ceph/osd/ceph-0 \
                    --osd-uuid 8d208665-89ae-4733-8888-5d3bfbeeec6c \
                    --keyring /var/lib/ceph/osd/ceph-0/keyring \
                    --setuser ceph --setgroup ceph

        In some cases it is required to use the keyring, when it is passed
        in as a keyword argument it is used as part of the ceph-osd command
        """

        if self.wal_device_path:
            self.osd_mkfs_cmd.extend(
                ['--bluestore-block-wal-path', self.wal_device_path]
            )
            system.chown(self.wal_device_path)

        if self.db_device_path:
            self.osd_mkfs_cmd.extend(
                ['--bluestore-block-db-path', self.db_device_path]
            )
            system.chown(self.db_device_path)

        if self.get_osdspec_affinity():
            self.osd_mkfs_cmd.extend(['--osdspec-affinity',
                                      self.get_osdspec_affinity()])

    def unlink_bs_symlinks(self) -> None:
        for link_name in ['block', 'block.db', 'block.wal']:
            link_path = os.path.join(self.osd_path, link_name)
            if os.path.exists(link_path):
                os.unlink(os.path.join(self.osd_path, link_name))

    def prepare_osd_req(self, tmpfs: bool = True) -> None:
        # create the directory
        prepare_utils.create_osd_path(self.osd_id, tmpfs=tmpfs)
        # symlink the block
        prepare_utils.link_block(self.block_device_path, self.osd_id)
        # get the latest monmap
        prepare_utils.get_monmap(self.osd_id)
        # write the OSD keyring if it doesn't exist already
        prepare_utils.write_keyring(self.osd_id, self.cephx_secret)

    def prepare(self) -> None:
        raise NotImplementedError()

    def prepare_dmcrypt(self) -> None:
        raise NotImplementedError()

    def get_cluster_fsid(self) -> str:
        """
        Allows using --cluster-fsid as an argument, but can fallback to reading
        from ceph.conf if that is unset (the default behavior).
        """
        if self.args.cluster_fsid:
            return self.args.cluster_fsid
        else:
            return conf.ceph.get('global', 'fsid')

    def get_osd_path(self) -> str:
        return '/var/lib/ceph/osd/%s-%s/' % (conf.cluster, self.osd_id)

    def build_osd_mkfs_cmd(self) -> List[str]:
        self.supplementary_command = [
            '--osd-data', self.osd_path,
            '--osd-uuid', self.osd_fsid,
            '--setuser', 'ceph',
            '--setgroup', 'ceph'
        ]
        self.osd_mkfs_cmd = [
            'ceph-osd',
            '--cluster', conf.cluster,
            '--osd-objectstore', self.objectstore,
            '--mkfs',
            '-i', self.osd_id,
            '--monmap', self.monmap,
        ]
        if self.cephx_secret is not None:
            self.osd_mkfs_cmd.extend(['--keyfile', '-'])

        self.add_objectstore_opts()

        self.osd_mkfs_cmd.extend(self.supplementary_command)
        return self.osd_mkfs_cmd

    def osd_mkfs(self) -> None:
        self.osd_path = self.get_osd_path()
        self.monmap = os.path.join(self.osd_path, 'activate.monmap')
        cmd = self.build_osd_mkfs_cmd()

        system.chown(self.osd_path)
        """
        When running in containers the --mkfs on raw device sometimes fails
        to acquire a lock through flock() on the device because systemd-udevd holds one temporarily.
        See KernelDevice.cc and _lock() to understand how ceph-osd acquires the lock.
        Because this is really transient, we retry up to 5 times and wait for 1 sec in-between
        """
        for retry in range(5):
            _, _, returncode = process.call(cmd,
                                            stdin=self.cephx_secret,
                                            terminal_verbose=True,
                                            show_command=True)
            if returncode == 0:
                break
            else:
                if returncode == errno.EWOULDBLOCK:
                    time.sleep(1)
                    logger.info('disk is held by another process, '
                                'trying to mkfs again... (%s/5 attempt)' %
                                retry)
                    continue
                else:
                    raise RuntimeError('Command failed with exit code %s: %s' %
                                       (returncode, ' '.join(cmd)))

        mapping: Dict[str, Any] = {'raw': ['data', 'block_db', 'block_wal'],
                                   'lvm': ['ceph.block_device', 'ceph.db_device', 'ceph.wal_device']}
        if self.args.dmcrypt:
            for dev_type in mapping[self.method]:
                if self.method == 'raw':
                    path = self.args.__dict__.get(dev_type, None)
                else:
                    if self.block_lv is not None:
                        path = self.block_lv.tags.get(dev_type, None)
                    else:
                        raise RuntimeError('Unexpected error while running bluestore mkfs.')
                if path is not None:
                    encryption_utils.CephLuks2(path).config_luks2({'subsystem': f'ceph_fsid={self.osd_fsid}'})

    def activate(self) -> None:
        raise NotImplementedError()

    def activate_all(self) -> None:
        raise NotImplementedError()

    def enroll_tpm2(self, device: str) -> None:
        """
        Enrolls a device with TPM2 (Trusted Platform Module 2.0) using systemd-cryptenroll.
        This method creates a temporary file to store the dmcrypt key and uses it to enroll the device.

        Args:
            device (str): The device path to be enrolled with TPM2.
        """

        if self.with_tpm:
            tmp_dir: str = '/rootfs/tmp' if os.environ.get('I_AM_IN_A_CONTAINER', False) else '/tmp'
            with tempfile.NamedTemporaryFile(mode='w', delete=True, dir=tmp_dir) as temp_file:
                temp_file.write(self.dmcrypt_key)
                temp_file.flush()
                temp_file_name: str = temp_file.name.replace('/rootfs', '', 1)
                cmd: List[str] = ['systemd-cryptenroll', '--tpm2-device=auto',
                                  device, '--unlock-key-file', temp_file_name,
                                  '--tpm2-pcrs', '9+12', '--wipe-slot', 'tpm2']
                process.call(cmd, run_on_host=True, show_command=True)

    def add_label(self, key: str,
                  value: str,
                  device: str) -> None:
        """Add a label to a BlueStore device.
        Args:
            key (str): The name of the label being added.
            value (str): Value of the label being added.
            device (str): The path of the BlueStore device.
        Raises:
            RuntimeError: If `ceph-bluestore-tool` command doesn't success.
        """

        command: List[str] = ['ceph-bluestore-tool',
                              'set-label-key',
                              '-k',
                              key,
                              '-v',
                              value,
                              '--dev',
                              device]

        _, err, rc = process.call(command,
                                  terminal_verbose=True,
                                  show_command=True)
        if rc:
            raise RuntimeError(f"Can't add BlueStore label '{key}' to device {device}: {err}")