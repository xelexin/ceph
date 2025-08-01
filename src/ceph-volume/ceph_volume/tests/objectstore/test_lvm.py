import pytest
from argparse import Namespace
from unittest.mock import patch, Mock, MagicMock, call
from ceph_volume.objectstore.lvm import Lvm
from ceph_volume.api.lvm import Volume
from ceph_volume.util import system, disk
from typing import Callable


class TestLvm:
    @patch('ceph_volume.objectstore.lvm.prepare_utils.create_key', Mock(return_value=['AQCee6ZkzhOrJRAAZWSvNC3KdXOpC2w8ly4AZQ==']))
    def setup_method(self, m_create_key):
        self.lvm = Lvm([])

    @patch('ceph_volume.conf.cluster', 'ceph')
    @patch('ceph_volume.api.lvm.get_single_lv')
    @patch('ceph_volume.objectstore.lvm.prepare_utils.create_id', Mock(return_value='111'))
    def test_pre_prepare_lv(self, m_get_single_lv, factory):
        args = factory(objectstore='seastore',
                       cluster_fsid='abcd',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       data='vg_foo/lv_foo')
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        self.lvm.encrypted = True
        self.lvm.dmcrypt_key = 'fake-dmcrypt-key'
        self.lvm.args = args
        self.lvm.objectstore = 'seastore'
        self.lvm.pre_prepare()
        assert self.lvm.secrets['dmcrypt_key'] == 'fake-dmcrypt-key'
        assert self.lvm.secrets['crush_device_class'] == 'ssd'
        assert self.lvm.osd_id == '111'
        assert self.lvm.block_device_path == '/fake-path'
        assert self.lvm.tags == {'ceph.osd_fsid': 'abc123',
                                    'ceph.osd_id': '111',
                                    'ceph.cluster_fsid': 'abcd',
                                    'ceph.cluster_name': 'ceph',
                                    'ceph.crush_device_class': 'ssd',
                                    'ceph.osdspec_affinity': '',
                                    'ceph.block_device': '/fake-path',
                                    'ceph.block_uuid': 'fake-uuid',
                                    'ceph.cephx_lockbox_secret': '',
                                    'ceph.objectstore': 'seastore',
                                    'ceph.encrypted': True,
                                    'ceph.vdo': '0',
                                    'ceph.with_tpm': 0}

    @patch('ceph_volume.conf.cluster', 'ceph')
    @patch('ceph_volume.api.lvm.get_single_lv')
    @patch('ceph_volume.objectstore.lvm.prepare_utils.create_id', Mock(return_value='111'))
    def test_pre_prepare_lv_with_dmcrypt_and_tpm(self, m_get_single_lv, factory):
        args = factory(objectstore='seastore',
                       cluster_fsid='abcd',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       data='vg_foo/lv_foo',
                       dmcrypt=True,
                       with_tpm=True)
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        self.lvm.encrypted = True
        self.lvm.with_tpm = True
        self.lvm.dmcrypt_key = 'fake-dmcrypt-key-tpm2'
        self.lvm.args = args
        self.lvm.objectstore = 'seastore'
        self.lvm.pre_prepare()
        assert 'dmcrypt_key' not in self.lvm.secrets.keys()
        assert self.lvm.secrets['crush_device_class'] == 'ssd'
        assert self.lvm.osd_id == '111'
        assert self.lvm.block_device_path == '/fake-path'
        assert self.lvm.tags == {'ceph.osd_fsid': 'abc123',
                                    'ceph.osd_id': '111',
                                    'ceph.cluster_fsid': 'abcd',
                                    'ceph.cluster_name': 'ceph',
                                    'ceph.crush_device_class': 'ssd',
                                    'ceph.osdspec_affinity': '',
                                    'ceph.block_device': '/fake-path',
                                    'ceph.block_uuid': 'fake-uuid',
                                    'ceph.cephx_lockbox_secret': '',
                                    'ceph.encrypted': True,
                                    'ceph.objectstore': 'seastore',
                                    'ceph.vdo': '0',
                                    'ceph.with_tpm': 1}

    @patch('ceph_volume.conf.cluster', 'ceph')
    @patch('ceph_volume.objectstore.lvm.prepare_utils.create_id', Mock(return_value='111'))
    def test_pre_prepare_no_lv(self, factory):
        args = factory(cluster_fsid='abcd',
                       objectstore='seastore',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       data='/dev/foo',
                       dmcrypt_key='fake-dmcrypt-key')
        self.lvm.prepare_data_device = lambda x, y: Volume(lv_name='lv_foo',
                                                           lv_path='/fake-path',
                                                           vg_name='vg_foo',
                                                           lv_tags='',
                                                           lv_uuid='fake-uuid')
        self.lvm.encrypted = True
        self.lvm.dmcrypt_key = 'fake-dmcrypt-key'
        self.lvm.args = args
        self.lvm.objectstore = 'seastore'
        self.lvm.pre_prepare()
        assert self.lvm.secrets['dmcrypt_key'] == 'fake-dmcrypt-key'
        assert self.lvm.secrets['crush_device_class'] == 'ssd'
        assert self.lvm.osd_id == '111'
        assert self.lvm.block_device_path == '/fake-path'
        assert self.lvm.tags == {'ceph.osd_fsid': 'abc123',
                                 'ceph.osd_id': '111',
                                 'ceph.cluster_fsid': 'abcd',
                                 'ceph.cluster_name': 'ceph',
                                 'ceph.crush_device_class': 'ssd',
                                 'ceph.osdspec_affinity': '',
                                 'ceph.block_device': '/fake-path',
                                 'ceph.block_uuid': 'fake-uuid',
                                 'ceph.cephx_lockbox_secret': '',
                                 'ceph.encrypted': True,
                                 'ceph.vdo': '0',
                                 'ceph.with_tpm': 0,
                                 'ceph.objectstore': 'seastore'}

    @patch('ceph_volume.util.disk.is_partition', Mock(return_value=True))
    @patch('ceph_volume.api.lvm.create_lv')
    def test_prepare_data_device(self,
                                 m_create_lv: MagicMock,
                                 factory: Callable[..., Namespace]) -> None:
        args = factory(data='/dev/foo1',
                       data_slots=1,
                       data_size=102400)
        self.lvm.args = args
        m_create_lv.return_value = Volume(lv_name='lv_foo',
                                          lv_path='/fake-path',
                                          vg_name='vg_foo',
                                          lv_tags='',
                                          lv_uuid='abcd')
        assert self.lvm.prepare_data_device('block', 'abcd') == m_create_lv.return_value
        assert self.lvm.args.data_size == 102400

    @patch('ceph_volume.util.disk.is_device', Mock(return_value=False))
    @patch('ceph_volume.util.disk.is_partition', Mock(return_value=False))
    def test_prepare_data_device_fails(self, factory):
        args = factory(data='/dev/foo')
        self.lvm.args = args
        with pytest.raises(RuntimeError) as error:
            self.lvm.prepare_data_device('block', 'abcd')
        assert ('Cannot use device (/dev/foo). '
        'A vg/lv path or an existing device is needed') == str(error.value)

    @patch('ceph_volume.api.lvm.is_ceph_device', Mock(return_value=False))
    @patch('ceph_volume.api.lvm.get_single_lv')
    def test_safe_prepare(self, m_get_single_lv, factory):
        args = factory(data='vg_foo/lv_foo')
        self.lvm.args = args
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        self.lvm.prepare = MagicMock()
        self.lvm.safe_prepare()
        assert self.lvm.prepare.called

    @patch('ceph_volume.objectstore.lvm.Lvm.prepare', Mock(side_effect=Exception))
    @patch('ceph_volume.api.lvm.is_ceph_device', Mock(return_value=False))
    @patch('ceph_volume.objectstore.lvm.rollback_osd')
    @patch('ceph_volume.api.lvm.get_single_lv')
    def test_safe_prepare_raises_exception(self, m_get_single_lv, m_rollback_osd, factory):
        args = factory(data='/dev/foo')
        self.lvm.args = args
        self.lvm.osd_id = '111'
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        m_rollback_osd.return_value = MagicMock()
        with pytest.raises(Exception):
            self.lvm.safe_prepare()
        assert m_rollback_osd.mock_calls == [call('111')]

    @patch('ceph_volume.objectstore.lvm.Lvm.pre_prepare', Mock(return_value=None))
    @patch('ceph_volume.objectstore.lvm.Lvm.prepare_dmcrypt', MagicMock())
    @patch('ceph_volume.objectstore.baseobjectstore.BaseObjectStore.prepare_osd_req', MagicMock())
    @patch('ceph_volume.objectstore.baseobjectstore.BaseObjectStore.osd_mkfs', MagicMock())
    @patch('ceph_volume.util.disk.is_partition', Mock(return_value=True))
    @patch('ceph_volume.objectstore.baseobjectstore.BaseObjectStore.get_ptuuid', Mock(return_value='c6798f59-01'))
    @patch('ceph_volume.api.lvm.Volume.set_tags', MagicMock())
    @patch('ceph_volume.api.lvm.get_single_lv')
    def test_prepare(self,
                     m_get_single_lv: MagicMock,
                     is_root: Callable[..., None],
                     factory: Callable[..., Namespace]) -> None:
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        args = factory(data='vg_foo/lv_foo',
                       block_wal='/dev/foo1',
                       block_db='/dev/foo2',
                       block_wal_size=123,
                       block_db_size=123,
                       block_wal_slots=1,
                       block_db_slots=1,
                       with_tpm=False
                       )
        self.lvm.args = args
        self.lvm.block_lv = MagicMock()
        self.lvm.secrets['dmcrypt_key'] = 'fake-secret'
        self.lvm.prepare()
        assert self.lvm.wal_device_path == '/dev/foo1'
        assert self.lvm.db_device_path == '/dev/foo2'
        assert self.lvm.block_lv.set_tags.mock_calls == [call({
            'ceph.type': 'block',
            })]
        assert not self.lvm.prepare_dmcrypt.called
        assert self.lvm.osd_mkfs.called
        assert self.lvm.prepare_osd_req.called

    def test_prepare_dmcrypt(self):
        self.lvm.secrets = {'dmcrypt_key': 'fake-secret'}
        self.lvm.tags = {'ceph.block_uuid': 'block-uuid1',
                            'ceph.db_uuid': 'db-uuid2',
                            'ceph.wal_uuid': 'wal-uuid3',
                            'ceph.with_tpm': 0}
        self.lvm.block_device_path = '/dev/sdb'
        self.lvm.db_device_path = '/dev/sdc'
        self.lvm.wal_device_path = '/dev/sdb'
        self.lvm.luks_format_and_open = lambda *a: f'/dev/mapper/{a[2]["ceph."+a[1]+"_uuid"]}'
        self.lvm.prepare_dmcrypt()
        assert self.lvm.block_device_path == '/dev/mapper/block-uuid1'
        assert self.lvm.db_device_path == '/dev/mapper/db-uuid2'
        assert self.lvm.wal_device_path == '/dev/mapper/wal-uuid3'

    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_open')
    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_format')
    def test_luks_format_and_open(self, m_luks_format, m_luks_open):
        result = self.lvm.luks_format_and_open('/dev/foo',
                                                  'block',
                                                  {'ceph.block_uuid': 'block-uuid1'})
        assert result == '/dev/mapper/block-uuid1'

    @patch('ceph_volume.objectstore.lvm.Lvm.enroll_tpm2', Mock(return_value=MagicMock()))
    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_open')
    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_format')
    def test_luks_format_and_open_with_tpm(self, m_luks_format, m_luks_open):
        self.lvm.with_tpm = True
        result = self.lvm.luks_format_and_open('/dev/foo',
                                                  'block',
                                                  {'ceph.block_uuid': 'block-uuid1'})
        assert result == '/dev/mapper/block-uuid1'
        self.lvm.enroll_tpm2.assert_called_once()

    def test_luks_format_and_open_not_device(self):
        result = self.lvm.luks_format_and_open('',
                                                  'block',
                                                  {})
        assert result == ''

    @patch('ceph_volume.api.lvm.Volume.set_tags', return_value=MagicMock())
    @patch('ceph_volume.util.system.generate_uuid',
           Mock(return_value='d83fa1ca-bd68-4c75-bdc2-464da58e8abd'))
    @patch('ceph_volume.api.lvm.create_lv')
    @patch('ceph_volume.util.disk.is_device', Mock(return_value=True))
    def test_setup_metadata_devices_is_device(self,
                                              m_create_lv: MagicMock,
                                              m_set_tags: MagicMock,
                                              factory: Callable[..., Namespace]) -> None:
        m_create_lv.return_value = Volume(lv_name='lv_foo',
                                          lv_path='/fake-path',
                                          vg_name='vg_foo',
                                          lv_tags='',
                                          lv_uuid='fake-uuid')
        args = factory(cluster_fsid='abcd',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       block_db='/dev/db',
                       block_db_size=disk.Size(gb=200),
                       block_db_slots=1,
                       block_wal=None,
                       block_wal_size='0',
                       block_wal_slots=None)
        self.lvm.args = args
        self.lvm.setup_metadata_devices()
        assert m_create_lv.mock_calls == [call(name_prefix='osd-db',
                                               uuid='d83fa1ca-bd68-4c75-bdc2-464da58e8abd',
                                               vg=None,
                                               device='/dev/db',
                                               slots=1,
                                               extents=None,
                                               size=disk.Size(gb=200),
                                               tags={'ceph.type': 'db',
                                                     'ceph.vdo': '0',
                                                     'ceph.db_device': '/fake-path',
                                                     'ceph.db_uuid': 'fake-uuid'})]

    @patch('ceph_volume.api.lvm.get_single_lv')
    @patch('ceph_volume.api.lvm.Volume.set_tags', return_value=MagicMock())
    def test_setup_metadata_devices_is_lv(self,
                                          m_set_tags: MagicMock,
                                          m_get_single_lv: MagicMock,
                                          factory: Callable[..., Namespace]) -> None:
        m_get_single_lv.return_value = Volume(lv_name='lv_foo',
                                              lv_path='/fake-path',
                                              vg_name='vg_foo',
                                              lv_tags='',
                                              lv_uuid='fake-uuid')
        args = factory(cluster_fsid='abcd',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       block_db='vg1/lv1',
                       block_db_size=disk.Size(gb=200),
                       block_db_slots=1,
                       block_wal=None,
                       block_wal_size='0',
                       block_wal_slots=None)
        self.lvm.args = args
        self.lvm.setup_metadata_devices()
        assert m_set_tags.mock_calls == [call({
            'ceph.type': 'db',
            'ceph.vdo': '0',
            'ceph.db_uuid': 'fake-uuid',
            'ceph.db_device': '/fake-path'
            })]

    @patch('ceph_volume.util.disk.is_partition', Mock(return_value=True))
    @patch('ceph_volume.objectstore.baseobjectstore.BaseObjectStore.get_ptuuid', Mock(return_value='c6798f59-01'))
    @patch('ceph_volume.api.lvm.Volume.set_tags', return_value=MagicMock())
    @patch('ceph_volume.api.lvm.create_lv')
    def test_setup_metadata_devices_partition(self,
                                              m_create_lv: MagicMock,
                                              m_set_tags: MagicMock,
                                              factory: Callable[..., Namespace]) -> None:
        args = factory(cluster_fsid='abcd',
                       osd_fsid='abc123',
                       crush_device_class='ssd',
                       osd_id='111',
                       block_db='/dev/foo1',
                       block_db_size=disk.Size(gb=200),
                       block_db_slots=1,
                       block_wal=None,
                       block_wal_size='0',
                       block_wal_slots=None)
        self.lvm.args = args
        self.lvm.setup_metadata_devices()
        m_create_lv.assert_not_called()
        m_set_tags.assert_not_called()

    def test_get_osd_device_path_lv_block(self):
        lvs = [Volume(lv_name='lv_foo',
                      lv_path='/fake-path',
                      vg_name='vg_foo',
                      lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid',
                      lv_uuid='fake-block-uuid')]
        assert self.lvm.get_osd_device_path(lvs, 'block') == '/fake-path'

    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_open', MagicMock())
    def test_get_osd_device_path_lv_block_encrypted(self):
        lvs = [Volume(lv_name='lv_foo',
                      lv_path='/fake-path',
                      vg_name='vg_foo',
                      lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid,ceph.encrypted=1',
                      lv_uuid='fake-block-uuid')]
        assert self.lvm.get_osd_device_path(lvs, 'block') == '/dev/mapper/fake-block-uuid'

    def test_get_osd_device_path_lv_db(self):
        lvs = [Volume(lv_name='lv_foo-block',
                      lv_path='/fake-block-path',
                      vg_name='vg_foo',
                      lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid,ceph.db_uuid=fake-db-uuid',
                      lv_uuid='fake-block-uuid'),
               Volume(lv_name='lv_foo-db',
                      lv_path='/fake-db-path',
                      vg_name='vg_foo_db',
                      lv_tags='ceph.type=db,ceph.block_uuid=fake-block-uuid,ceph.db_uuid=fake-db-uuid',
                      lv_uuid='fake-db-uuid')]
        assert self.lvm.get_osd_device_path(lvs, 'db') == '/fake-db-path'

    def test_get_osd_device_path_no_device_uuid(self):
        lvs = [Volume(lv_name='lv_foo-block',
                      lv_path='/fake-block-path',
                      vg_name='vg_foo',
                      lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid',
                      lv_uuid='fake-block-uuid'),
               Volume(lv_name='lv_foo-db',
                      lv_path='/fake-db-path',
                      vg_name='vg_foo_db',
                      lv_tags='ceph.type=db,ceph.block_uuid=fake-block-uuid',
                      lv_uuid='fake-db-uuid')]
        assert not self.lvm.get_osd_device_path(lvs, 'db')

    @patch('ceph_volume.util.disk.get_device_from_partuuid')
    @patch('ceph_volume.objectstore.lvm.encryption_utils.luks_open', MagicMock())
    def test_get_osd_device_path_phys_encrypted(self, m_get_device_from_partuuid):
        m_get_device_from_partuuid.return_value = '/dev/sda1'
        lvs = [Volume(lv_name='lv_foo-block',
                     lv_path='/fake-block-path',
                     vg_name='vg_foo',
                     lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid,ceph.db_uuid=fake-db-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph,ceph.encrypted=1',
                     lv_uuid='fake-block-uuid')]
        assert self.lvm.get_osd_device_path(lvs, 'db') == '/dev/mapper/fake-db-uuid'

    @patch('ceph_volume.util.disk.get_device_from_partuuid')
    def test_get_osd_device_path_phys(self, m_get_device_from_partuuid):
        m_get_device_from_partuuid.return_value = '/dev/sda1'
        lvs = [Volume(lv_name='lv_foo-block',
                     lv_path='/fake-block-path',
                     vg_name='vg_foo',
                     lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid,ceph.db_uuid=fake-db-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph',
                     lv_uuid='fake-block-uuid')]
        self.lvm.get_osd_device_path(lvs, 'db')

    @patch('ceph_volume.util.disk.get_device_from_partuuid')
    def test_get_osd_device_path_phys_raises_exception(self, m_get_device_from_partuuid):
        m_get_device_from_partuuid.return_value = ''
        lvs = [Volume(lv_name='lv_foo-block',
                     lv_path='/fake-block-path',
                     vg_name='vg_foo',
                     lv_tags='ceph.type=block,ceph.block_uuid=fake-block-uuid,ceph.db_uuid=fake-db-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph',
                     lv_uuid='fake-block-uuid')]
        with pytest.raises(RuntimeError):
            self.lvm.get_osd_device_path(lvs, 'db')

    def test__activate_raises_exception(self):
        lvs = [Volume(lv_name='lv_foo-db',
                      lv_path='/fake-path',
                      vg_name='vg_foo',
                      lv_tags='ceph.type=db,ceph.db_uuid=fake-db-uuid',
                      lv_uuid='fake-db-uuid')]
        with pytest.raises(RuntimeError) as error:
            self.lvm._activate(lvs)
        assert str(error.value) == 'could not find a bluestore OSD to activate'

    @patch('ceph_volume.objectstore.lvm.encryption_utils.write_lockbox_keyring', MagicMock())
    @patch('ceph_volume.objectstore.lvm.encryption_utils.get_dmcrypt_key', MagicMock())
    @patch('ceph_volume.objectstore.lvm.prepare_utils.create_osd_path')
    @patch('ceph_volume.terminal.success')
    @pytest.mark.parametrize("encrypted", ["ceph.encrypted=0", "ceph.encrypted=1"])
    def test__activate(self,
                       m_success, m_create_osd_path,
                       monkeypatch, fake_run, fake_call, encrypted, conf_ceph_stub):
        conf_ceph_stub('[global]\nfsid=asdf-lkjh')
        monkeypatch.setattr(system, 'chown', lambda path: 0)
        monkeypatch.setattr('ceph_volume.configuration.load', lambda: None)
        monkeypatch.setattr('ceph_volume.util.system.path_is_mounted', lambda path: False)
        m_create_osd_path.return_value = MagicMock()
        m_success.return_value = MagicMock()
        lvs = [Volume(lv_name='lv_foo-block',
                      lv_path='/fake-block-path',
                      vg_name='vg_foo',
                      lv_tags=f'ceph.type=block,ceph.db_uuid=fake-db-uuid,ceph.block_uuid=fake-block-uuid,ceph.wal_uuid=fake-wal-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph,{encrypted},ceph.cephx_lockbox_secret=abcd',
                      lv_uuid='fake-block-uuid'),
               Volume(lv_name='lv_foo-db',
                      lv_path='/fake-db-path',
                      vg_name='vg_foo_db',
                      lv_tags=f'ceph.type=db,ceph.db_uuid=fake-db-uuid,ceph.block_uuid=fake-block-uuid,ceph.wal_uuid=fake-wal-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph,{encrypted},ceph.cephx_lockbox_secret=abcd',
                      lv_uuid='fake-db-uuid'),
               Volume(lv_name='lv_foo-db',
                      lv_path='/fake-wal-path',
                      vg_name='vg_foo_wal',
                      lv_tags=f'ceph.type=wal,ceph.block_uuid=fake-block-uuid,ceph.wal_uuid=fake-wal-uuid,ceph.db_uuid=fake-db-uuid,ceph.osd_id=0,ceph.osd_fsid=abcd,ceph.cluster_name=ceph,{encrypted},ceph.cephx_lockbox_secret=abcd',
                      lv_uuid='fake-wal-uuid')]
        self.lvm._activate(lvs)
        if encrypted == "ceph.encrypted=0":
            assert fake_run.calls == [{'args': (['ceph-bluestore-tool', '--cluster=ceph',
                                                 'prime-osd-dir', '--dev', '/fake-block-path',
                                                 '--path', '/var/lib/ceph/osd/ceph-0', '--no-mon-config'],),
                                       'kwargs': {}},
                                      {'args': (['ln', '-snf', '/fake-block-path',
                                                 '/var/lib/ceph/osd/ceph-0/block'],),
                                       'kwargs': {}},
                                      {'args': (['ln', '-snf', '/fake-db-path',
                                                 '/var/lib/ceph/osd/ceph-0/block.db'],),
                                       'kwargs': {}},
                                      {'args': (['ln', '-snf', '/fake-wal-path',
                                                 '/var/lib/ceph/osd/ceph-0/block.wal'],),
                                       'kwargs': {}},
                                      {'args': (['systemctl', 'enable',
                                                 'ceph-volume@lvm-0-abcd'],),
                                       'kwargs': {}},
                                      {'args': (['systemctl', 'enable', '--runtime', 'ceph-osd@0'],),
                                       'kwargs': {}},
                                      {'args': (['systemctl', 'start', 'ceph-osd@0'],),
                                       'kwargs': {}}]
        else:
            assert fake_run.calls == [{'args': (['ceph-bluestore-tool', '--cluster=ceph',
                                                'prime-osd-dir', '--dev', '/dev/mapper/fake-block-uuid',
                                                '--path', '/var/lib/ceph/osd/ceph-0', '--no-mon-config'],),
                                      'kwargs': {}},
                                      {'args': (['ln', '-snf', '/dev/mapper/fake-block-uuid',
                                                  '/var/lib/ceph/osd/ceph-0/block'],),
                                      'kwargs': {}},
                                      {'args': (['ln', '-snf', '/dev/mapper/fake-db-uuid',
                                                  '/var/lib/ceph/osd/ceph-0/block.db'],),
                                      'kwargs': {}},
                                      {'args': (['ln', '-snf', '/dev/mapper/fake-wal-uuid',
                                                  '/var/lib/ceph/osd/ceph-0/block.wal'],),
                                      'kwargs': {}},
                                      {'args': (['systemctl', 'enable', 'ceph-volume@lvm-0-abcd'],),
                                      'kwargs': {}},
                                      {'args': (['systemctl', 'enable', '--runtime', 'ceph-osd@0'],),
                                      'kwargs': {}},
                                      {'args': (['systemctl', 'start', 'ceph-osd@0'],),
                                      'kwargs': {}}]
        assert m_success.mock_calls == [call('ceph-volume lvm activate successful for osd ID: 0')]

    @patch('ceph_volume.systemd.systemctl.osd_is_active', return_value=False)
    def test_activate_all(self,
                          m_create_key,
                          mock_lvm_direct_report,
                          is_root,
                          factory,
                          fake_run):
        args = factory(no_systemd=True)
        self.lvm.args = args
        self.lvm.activate = MagicMock()
        self.lvm.activate_all()
        assert self.lvm.activate.mock_calls == [call(args,
                                                        osd_id='1',
                                                        osd_fsid='824f7edf-371f-4b75-9231-4ab62a32d5c0'),
                                                   call(args,
                                                        osd_id='0',
                                                        osd_fsid='a0e07c5b-bee1-4ea2-ae07-cb89deda9b27')]

    @patch('ceph_volume.systemd.systemctl.osd_is_active', return_value=False)
    def test_activate_all_no_osd_found(self,
                                       m_create_key,
                                       is_root,
                                       factory,
                                       fake_run,
                                       monkeypatch,
                                       capsys):
        monkeypatch.setattr('ceph_volume.objectstore.lvm.direct_report', lambda: {})
        args = factory(no_systemd=True)
        self.lvm.args = args
        self.lvm.activate_all()
        stdout, stderr = capsys.readouterr()
        assert "Was unable to find any OSDs to activate" in stderr
        assert "Verify OSDs are present with" in stderr

    @patch('ceph_volume.api.lvm.process.call', Mock(return_value=('', '', 0)))
    @patch('ceph_volume.systemd.systemctl.osd_is_active', return_value=True)
    def test_activate_all_osd_is_active(self,
                                        mock_lvm_direct_report,
                                        is_root,
                                        factory,
                                        fake_run):
        args = factory(no_systemd=False)
        self.lvm.args = args
        self.lvm.activate = MagicMock()
        self.lvm.activate_all()
        assert self.lvm.activate.mock_calls == []

    @patch('ceph_volume.api.lvm.get_lvs')
    def test_activate_osd_id_and_fsid(self,
                                      m_get_lvs,
                                      is_root,
                                      factory):
        args = factory(osd_id='1',
                       osd_fsid='824f7edf',
                       no_systemd=True)
        lvs = [Volume(lv_name='lv_foo',
                      lv_path='/fake-path',
                      vg_name='vg_foo',
                      lv_tags=f'ceph.osd_id={args.osd_id},ceph.osd_fsid={args.osd_fsid}',
                      lv_uuid='fake-uuid')]
        m_get_lvs.return_value = lvs
        self.lvm.args = args
        self.lvm._activate = MagicMock()
        self.lvm.activate()
        assert self.lvm._activate.mock_calls == [call(lvs, True, False)]
        assert m_get_lvs.mock_calls == [call(tags={'ceph.osd_id': '1',
                                                   'ceph.osd_fsid': '824f7edf'})]

    @patch('ceph_volume.api.lvm.get_lvs')
    def test_activate_not_osd_id_and_fsid(self,
                                          m_get_lvs,
                                          is_root,
                                          factory):
        args = factory(no_systemd=True,
                       osd_id=None,
                       osd_fsid='824f7edf')
        lvs = [Volume(lv_name='lv_foo',
                      lv_path='/fake-path',
                      vg_name='vg_foo',
                      lv_tags='',
                      lv_uuid='fake-uuid')]
        m_get_lvs.return_value = lvs
        self.lvm.args = args
        self.lvm._activate = MagicMock()
        self.lvm.activate()
        assert self.lvm._activate.mock_calls == [call(lvs, True, False)]
        assert m_get_lvs.mock_calls == [call(tags={'ceph.osd_fsid': '824f7edf'})]

    def test_activate_osd_id_and_not_fsid(self,
                                          is_root,
                                          factory):
        args = factory(no_systemd=True,
                       osd_id='1',
                       osd_fsid=None)
        self.lvm.args = args
        self.lvm._activate = MagicMock()
        with pytest.raises(RuntimeError) as error:
            self.lvm.activate()
        assert str(error.value) == 'could not activate osd.1, please provide the osd_fsid too'

    def test_activate_not_osd_id_and_not_fsid(self,
                                              is_root,
                                              factory):
        args = factory(no_systemd=True,
                       osd_id=None,
                       osd_fsid=None)
        self.lvm.args = args
        self.lvm._activate = MagicMock()
        with pytest.raises(RuntimeError) as error:
            self.lvm.activate()
        assert str(error.value) == 'Please provide both osd_id and osd_fsid'

    @patch('ceph_volume.api.lvm.get_lvs')
    def test_activate_couldnt_find_osd(self,
                                       m_get_lvs,
                                       is_root,
                                       factory):
        args = factory(osd_id='1',
                       osd_fsid='824f7edf',
                       no_systemd=True)
        lvs = []
        m_get_lvs.return_value = lvs
        self.lvm.args = args
        self.lvm._activate = MagicMock()
        with pytest.raises(RuntimeError) as error:
            self.lvm.activate()
        assert str(error.value) == 'could not find osd.1 with osd_fsid 824f7edf'