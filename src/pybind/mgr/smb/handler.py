from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

import contextlib
import logging
import time

from ceph.deployment.service_spec import SMBSpec

from . import config_store, external, resources
from .enums import (
    AuthMode,
    CephFSStorageProvider,
    JoinSourceType,
    LoginAccess,
    LoginCategory,
    State,
    UserGroupSourceType,
)
from .internal import (
    ClusterEntry,
    JoinAuthEntry,
    ShareEntry,
    UsersAndGroupsEntry,
)
from .proto import (
    AccessAuthorizer,
    ConfigEntry,
    ConfigStore,
    EarmarkResolver,
    OrchSubmitter,
    PathResolver,
    Simplified,
)
from .resources import SMBResource
from .results import ErrorResult, Result, ResultGroup
from .staging import Staging, auth_refs, cross_check_resource, ug_refs
from .utils import checked, ynbool

ClusterRef = Union[resources.Cluster, resources.RemovedCluster]
ShareRef = Union[resources.Share, resources.RemovedShare]

_DOMAIN = 'domain'
_CLUSTERED = 'clustered'
_CEPHFS_PROXY = 'cephfs-proxy'
log = logging.getLogger(__name__)


class InvalidResourceMatch(ValueError):
    pass


class ClusterChangeGroup:
    """A bag of holding for items being modified and thus needing synchronizing
    with the external stores & components.
    """

    def __init__(
        self,
        cluster: ClusterRef,
        shares: List[resources.Share],
        join_auths: List[resources.JoinAuth],
        users_and_groups: List[resources.UsersAndGroups],
    ):
        self.cluster = cluster
        self.shares = shares
        self.join_auths = join_auths
        self.users_and_groups = users_and_groups
        # a cache for modified entries
        self.cache = config_store.EntryCache()

    def cache_updated_entry(self, entry: ConfigEntry) -> None:
        self.cache[entry.full_key] = entry


class _FakePathResolver:
    """A stub PathResolver for unit testing."""

    def resolve(
        self, volume: str, subvolumegroup: str, subvolume: str, path: str
    ) -> str:
        path = path.lstrip('/')
        if subvolumegroup or subvolume:
            import uuid

            # mimic the uuid found in a real ceph subvolume path
            # by deriving a uuid from the existing values we have
            vid = str(
                uuid.uuid3(
                    uuid.NAMESPACE_URL,
                    f'cephfs+{volume}:{subvolumegroup}:{subvolume}',
                )
            )
            subvolumegroup = subvolumegroup or '_nogroup'
            return f'/volumes/{subvolumegroup}/{subvolume}/{vid}/{path}'
        return f'/{path}'

    resolve_exists = resolve


class _FakeEarmarkResolver:
    """A stub EarmarkResolver for unit testing."""

    def __init__(self) -> None:
        self._earmarks: Dict[Tuple[str, str], str] = {}

    def get_earmark(self, path: str, volume: str) -> Optional[str]:
        return None

    def set_earmark(self, path: str, volume: str, earmark: str) -> None:
        pass

    def check_earmark(self, earmark: str, top_level_scope: str) -> bool:
        return True


class _FakeAuthorizer:
    """A stub AccessAuthorizer for unit testing."""

    def authorize_entity(
        self, volume: str, entity: str, caps: str = ''
    ) -> None:
        pass


class _Matcher:
    def __init__(self) -> None:
        self._contents: Set[Any] = set()
        self._inputs: Set[str] = set()

    def __str__(self) -> str:
        if not self._contents:
            return 'match-all'
        return 'match-resources:' + ','.join(self._inputs)

    def __contains__(self, value: Any) -> bool:
        if not self._contents:
            return True
        if not isinstance(value, tuple):
            return value in self._contents
        assert len(value) > 1
        return (
            # match a specific resource id
            value in self._contents
            # match all ids of a given resource type
            or (value[0], None) in self._contents
            # match a all partial ids (shares only)
            or (
                len(value) == 3
                and (value[0], value[1], None) in self._contents
            )
        )

    def parse(self, txt: str) -> None:
        rtypes: Dict[str, Any] = {
            cast(Any, r).resource_type: r
            for r in (
                resources.Cluster,
                resources.Share,
                resources.JoinAuth,
                resources.UsersAndGroups,
            )
        }
        if txt in rtypes:
            resource_cls = rtypes[txt]
            self._contents.add(resource_cls)
            self._contents.add((resource_cls, None))
            self._inputs.add(txt)
            return
        try:
            prefix, id_a = txt.rsplit('.', 1)
            resource_cls = rtypes[prefix]
            self._contents.add(resource_cls)
            self._contents.add((resource_cls, id_a))
            self._contents.add((resource_cls, id_a, None))
            self._inputs.add(txt)
            return
        except (ValueError, KeyError):
            pass
        try:
            prefix, id_a, id_b = txt.rsplit('.', 2)
            resource_cls = rtypes[prefix]
            self._contents.add(resource_cls)
            self._contents.add((resource_cls, id_a, id_b))
            self._inputs.add(txt)
            return
        except (ValueError, KeyError):
            pass
        raise InvalidResourceMatch(
            f'{txt!r} does not match a valid resource type'
        )


class ClusterConfigHandler:
    """The central class for ingesting and handling smb configuration change
    requests.

    The ClusterConfigHandler works in roughly three phases:
    1. Validation - for the resources being updated makes sure they're valid
                    internally and also performs basic consistency checks.
    2. Update     - updates the internal configuration store to persist the
                    new resource objects
    3. Sync'ing   - convert internal resources to externally usable data and
                    update external components as needed.
                    (see also "reconciliation")

    It makes use of three data stores.
    * internal_store: items that belong to the smb module. Generally, our
      own saved resource types.
    * public_store: A public store that is meant for sharing configuration data
      with other processes. It is intended for non-sensitive general
      configuration data
    * priv_store: A priv(ate/ileged) store that is also meant for sharing data
      with other processes. But unlike public store this data might be
      sensitive.

    Note that these stores are permitted to overlap. A public_store and
    priv_store could use the exact same store object if the caller configures
    the ClusterConfigHandler that way. This is very much expected when
    executed in unit/other tests. Do NOT assume the keys in stores are mutually
    exclusive!

    This class also exposes some extra functionality for reading/iterating
    the internal store so that the mgr module can be largely encapsulated
    away from the store(s).
    """

    def __init__(
        self,
        *,
        internal_store: ConfigStore,
        public_store: ConfigStore,
        priv_store: ConfigStore,
        path_resolver: Optional[PathResolver] = None,
        authorizer: Optional[AccessAuthorizer] = None,
        orch: Optional[OrchSubmitter] = None,
        earmark_resolver: Optional[EarmarkResolver] = None,
    ) -> None:
        self.internal_store = internal_store
        self.public_store = public_store
        self.priv_store = priv_store
        if path_resolver is None:
            path_resolver = _FakePathResolver()
        self._path_resolver: PathResolver = path_resolver
        if authorizer is None:
            authorizer = _FakeAuthorizer()
        self._authorizer: AccessAuthorizer = authorizer
        self._orch = orch  # if None, disables updating the spec via orch
        if earmark_resolver is None:
            earmark_resolver = cast(EarmarkResolver, _FakeEarmarkResolver())
        self._earmark_resolver = earmark_resolver
        log.info(
            'Initialized new ClusterConfigHandler with'
            f' internal store {self.internal_store!r},'
            f' public store {self.public_store!r},'
            f' priv store {self.priv_store!r},'
            f' path resolver {self._path_resolver!r},'
            f' authorizer {self._authorizer!r},'
            f' orch {self._orch!r},'
            f' earmark resolver {self._earmark_resolver!r}'
        )

    def apply(
        self, inputs: Iterable[SMBResource], *, create_only: bool = False
    ) -> ResultGroup:
        """Apply resource configuration changes.
        Set `create_only` to disable changing existing resource values.
        """
        log.debug('applying changes to internal data store')
        results = ResultGroup()
        staging = Staging(self.internal_store)
        try:
            incoming = order_resources(inputs)
            for resource in incoming:
                staging.stage(resource)
            with _store_transaction(staging.destination_store):
                for resource in incoming:
                    results.append(
                        self._check(
                            resource, staging, create_only=create_only
                        )
                    )
        except ErrorResult as err:
            results.append(err)
        except Exception as err:
            log.exception("error updating resource")
            msg = str(err)
            if not msg:
                # handle the case where the exception has no text
                msg = f"error updating resource: {type(err)} (see logs for details)"
            result = ErrorResult(resource, msg=msg)
            results.append(result)
        if results.success:
            log.debug(
                'successfully updated %s resources. syncing changes to public stores',
                len(list(results)),
            )
            with _store_transaction(staging.destination_store):
                results = staging.save()
                staging.prune_linked_entries()
            with _store_transaction(staging.destination_store):
                self._sync_modified(results)
        return results

    def cluster_ids(self) -> List[str]:
        return list(ClusterEntry.ids(self.internal_store))

    def share_ids(self) -> List[Tuple[str, str]]:
        return list(ShareEntry.ids(self.internal_store))

    def share_ids_by_cluster(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for cluster_id, share_id in ShareEntry.ids(self.internal_store):
            out.setdefault(cluster_id, []).append(share_id)
        return out

    def join_auth_ids(self) -> List[str]:
        return list(JoinAuthEntry.ids(self.internal_store))

    def user_and_group_ids(self) -> List[str]:
        return list(UsersAndGroupsEntry.ids(self.internal_store))

    def all_resources(self) -> List[SMBResource]:
        with _store_transaction(self.internal_store):
            return self._search_resources(_Matcher())

    def matching_resources(self, names: List[str]) -> List[SMBResource]:
        matcher = _Matcher()
        for name in names:
            matcher.parse(name)
        with _store_transaction(self.internal_store):
            return self._search_resources(matcher)

    def _search_resources(self, matcher: _Matcher) -> List[SMBResource]:
        log.debug("performing search with matcher: %s", matcher)
        out: List[SMBResource] = []
        if resources.Cluster in matcher or resources.Share in matcher:
            log.debug("searching for clusters and/or shares")
            cluster_shares = self.share_ids_by_cluster()
            for cluster_id in self.cluster_ids():
                if (resources.Cluster, cluster_id) in matcher:
                    out.append(self._cluster_entry(cluster_id).get_cluster())
                for share_id in cluster_shares.get(cluster_id, []):
                    if (resources.Share, cluster_id, share_id) in matcher:
                        out.append(
                            self._share_entry(
                                cluster_id, share_id
                            ).get_share()
                        )
        if resources.JoinAuth in matcher:
            log.debug("searching for join auths")
            for auth_id in self.join_auth_ids():
                if (resources.JoinAuth, auth_id) in matcher:
                    out.append(self._join_auth_entry(auth_id).get_join_auth())
        if resources.UsersAndGroups in matcher:
            log.debug("searching for users and groups")
            for ug_id in self.user_and_group_ids():
                if (resources.UsersAndGroups, ug_id) in matcher:
                    out.append(
                        self._users_and_groups_entry(
                            ug_id
                        ).get_users_and_groups()
                    )
        log.debug("search found %d resources", len(out))
        return out

    def _check(
        self,
        resource: SMBResource,
        staging: Staging,
        *,
        create_only: bool = False,
    ) -> Result:
        """Check/validate a staged resource."""
        log.debug('staging resource: %r', resource)
        if create_only:
            if not staging.is_new(resource):
                return Result(
                    resource,
                    success=False,
                    msg='a resource with the same ID already exists',
                )
        try:
            cross_check_resource(
                resource,
                staging,
                path_resolver=self._path_resolver,
                earmark_resolver=self._earmark_resolver,
            )
        except ErrorResult as err:
            log.debug('rejected resource: %r', resource)
            return err
        log.debug('checked resource: %r', resource)
        result = Result(resource, success=True, status={'checked': True})
        return result

    def _sync_clusters(
        self, modified_cluster_ids: Optional[Collection[str]] = None
    ) -> None:
        """Trigger synchronization for all the clusters listed in
        `modified_cluster_ids` or all clusters if None.
        """
        share_ids = self.share_ids()
        present_cluster_ids = set()
        removed_cluster_ids = set()
        change_groups = []
        cluster_ids = modified_cluster_ids or ClusterEntry.ids(
            self.internal_store
        )
        log.debug(
            'syncing %s clusters: %s',
            'all' if not modified_cluster_ids else 'selected',
            ' '.join(cluster_ids),
        )
        for cluster_id in cluster_ids:
            entry = self._cluster_entry(cluster_id)
            try:
                cluster = entry.get_cluster()
            except KeyError:
                removed_cluster_ids.add(cluster_id)
                continue
            present_cluster_ids.add(cluster_id)
            change_group = ClusterChangeGroup(
                cluster,
                [
                    self._share_entry(cid, shid).get_share()
                    for cid, shid in share_ids
                    if cid == cluster_id
                ],
                [
                    self._join_auth_entry(_id).get_join_auth()
                    for _id in auth_refs(cluster)
                ],
                [
                    self._users_and_groups_entry(_id).get_users_and_groups()
                    for _id in ug_refs(cluster)
                ],
            )
            change_groups.append(change_group)
        for change_group in change_groups:
            self._save_cluster_settings(change_group)

        # if there are clusters in the public store, that don't exist
        # in the internal store, we need to clean them up.
        if not modified_cluster_ids:
            ext_ids = set(
                external.stored_cluster_ids(
                    self.public_store, self.priv_store
                )
            )
            removed_cluster_ids = ext_ids - set(cluster_ids)
        for cluster_id in removed_cluster_ids:
            self._remove_cluster(cluster_id)

    def _sync_modified(self, updated: ResultGroup) -> None:
        cluster_ids = self._find_modifications(updated)
        self._sync_clusters(cluster_ids)

    def _find_modifications(self, updated: ResultGroup) -> Collection[str]:
        """Given a ResultGroup tracking what was recently updated in the
        internal store, return all cluster_ids that may need external syncing.
        """
        # this initial version is going to take a simplistic approach and try
        # to broadly collect anything that could be a change.
        # Later, this function can be refined to trigger fewer changes by looking
        # at the objects in more detail any only producing a change group for
        # something that really has been modified.
        chg_cluster_ids: Set[str] = set()
        chg_join_ids: Set[str] = set()
        chg_ug_ids: Set[str] = set()
        for result in updated:
            state = (result.status or {}).get('state', None)
            if state in (State.PRESENT, State.NOT_PRESENT):
                # these are the no-change states. we can ignore them
                continue
            if isinstance(
                result.src, (resources.Cluster, resources.RemovedCluster)
            ):
                chg_cluster_ids.add(result.src.cluster_id)
            elif isinstance(
                result.src, (resources.Share, resources.RemovedShare)
            ):
                # shares always belong to one cluster
                chg_cluster_ids.add(result.src.cluster_id)
            elif isinstance(result.src, resources.JoinAuth):
                chg_join_ids.add(result.src.auth_id)
            elif isinstance(result.src, resources.UsersAndGroups):
                chg_ug_ids.add(result.src.users_groups_id)

        # TODO: here's a lazy bit. if any join auths or users/groups changed we
        # will regen all clusters because these can be shared by >1 cluster.
        # In future, make this only pick clusters using the named resources.
        if chg_join_ids or chg_ug_ids:
            chg_cluster_ids.update(ClusterEntry.ids(self.internal_store))
        return chg_cluster_ids

    def _save_cluster_settings(
        self, change_group: ClusterChangeGroup
    ) -> None:
        """Save the external facing objects. Tickle the external components."""
        log.debug(
            'saving external store for cluster: %s',
            change_group.cluster.cluster_id,
        )
        # vols: hold the cephfs volumes our shares touch. some operations are
        # disabled/skipped unless we touch volumes.
        vols = {share.checked_cephfs.volume for share in change_group.shares}
        data_entity = _cephx_data_entity(change_group.cluster.cluster_id)
        # save the various object types
        previous_info = _swap_pending_cluster_info(
            self.public_store,
            change_group,
            orch_needed=bool(vols and self._orch),
        )
        _save_pending_join_auths(self.priv_store, change_group)
        _save_pending_users_and_groups(self.priv_store, change_group)
        _save_pending_config(
            self.public_store,
            change_group,
            self._path_resolver,
            data_entity,
        )
        # remove any stray objects
        external.rm_other_in_ns(
            self.priv_store,
            change_group.cluster.cluster_id,
            set(change_group.cache),
        )

        # ensure a entity exists with access to the volumes
        for volume in vols:
            self._authorizer.authorize_entity(volume, data_entity)
        if not vols:
            # there were no volumes, and thus nothing to authorize. set data_entity
            # to an empty string to avoid adding it to the svc spec later.
            data_entity = ''

        # build a service spec for smb cluster
        cluster = change_group.cluster
        assert isinstance(cluster, resources.Cluster)
        config_entries = [
            change_group.cache[external.config_key(cluster.cluster_id)],
            self.public_store[
                external.config_key(cluster.cluster_id, override=True)
            ],
        ]
        join_source_entries = [
            change_group.cache[(cluster.cluster_id, key)]
            for key in external.stored_join_source_keys(
                change_group.cache, cluster.cluster_id
            )
        ]
        user_source_entries = [
            change_group.cache[(cluster.cluster_id, key)]
            for key in external.stored_usergroup_source_keys(
                change_group.cache, cluster.cluster_id
            )
        ]
        smb_spec = _generate_smb_service_spec(
            cluster,
            config_entries=config_entries,
            join_source_entries=join_source_entries,
            user_source_entries=user_source_entries,
            data_entity=data_entity,
            needs_proxy=_has_proxied_vfs(change_group),
        )
        _save_pending_spec_backup(self.public_store, change_group, smb_spec)
        # if orch was ever needed in the past we must "re-orch", but if we have
        # no volumes and never orch'ed before wait until we have something to
        # share before orchestrating the smb cluster. This is done because we
        # need volumes in order to have cephx keys that we pass to the services
        # via orch.  This differs from NFS because ganesha embeds the cephx
        # keys directly in each export definition block while samba needs the
        # ceph keyring to load keys.
        previous_orch = previous_info.get('orch_needed', False)
        if self._orch and (vols or previous_orch):
            self._orch.submit_smb_spec(smb_spec)

    def _remove_cluster(self, cluster_id: str) -> None:
        log.info('Removing cluster: %s', cluster_id)
        spec_key = external.spec_backup_key(cluster_id)
        if self.public_store[spec_key].exists() and self._orch:
            service_name = f'smb.{cluster_id}'
            log.debug('Removing smb orch service: %r', service_name)
            self._orch.remove_smb_service(service_name)
        external.rm_cluster(self.priv_store, cluster_id)
        external.rm_cluster(self.public_store, cluster_id)

    def _cluster_entry(self, cluster_id: str) -> ClusterEntry:
        return ClusterEntry.from_store(self.internal_store, cluster_id)

    def _share_entry(self, cluster_id: str, share_id: str) -> ShareEntry:
        return ShareEntry.from_store(
            self.internal_store, cluster_id, share_id
        )

    def _join_auth_entry(self, auth_id: str) -> JoinAuthEntry:
        return JoinAuthEntry.from_store(self.internal_store, auth_id)

    def _users_and_groups_entry(self, ug_id: str) -> UsersAndGroupsEntry:
        return UsersAndGroupsEntry.from_store(self.internal_store, ug_id)

    def generate_config(self, cluster_id: str) -> Dict[str, Any]:
        """Demo function that generates a config on demand."""
        cluster = self._cluster_entry(cluster_id).get_cluster()
        shares = [
            self._share_entry(cluster_id, shid).get_share()
            for shid in self.share_ids_by_cluster()[cluster_id]
        ]
        return _generate_config(
            cluster,
            shares,
            self._path_resolver,
            _cephx_data_entity(cluster_id),
        )

    def generate_smb_service_spec(self, cluster_id: str) -> SMBSpec:
        """Demo function that generates a smb service spec on demand."""
        cluster = self._cluster_entry(cluster_id).get_cluster()
        # if the user manually puts custom configurations (aka "override"
        # configs) in the store, use that in favor of the generated config.
        # this is mainly intended for development/test
        config_entries = [
            self.public_store[external.config_key(cluster_id)],
            self.public_store[external.config_key(cluster_id, override=True)],
        ]
        join_source_entries = [
            self.priv_store[(cluster_id, key)]
            for key in external.stored_join_source_keys(
                self.priv_store, cluster_id
            )
        ]
        user_source_entries = [
            self.priv_store[(cluster_id, key)]
            for key in external.stored_usergroup_source_keys(
                self.priv_store, cluster_id
            )
        ]
        return _generate_smb_service_spec(
            cluster,
            config_entries=config_entries,
            join_source_entries=join_source_entries,
            user_source_entries=user_source_entries,
        )


def order_resources(
    resource_objs: Iterable[SMBResource],
) -> List[SMBResource]:
    """Sort resource objects by type so that the user can largely input
    objects freely but that references map out cleanly.
    """

    def _keyfunc(r: SMBResource) -> int:
        if isinstance(r, resources.RemovedShare):
            return -2
        if isinstance(r, resources.RemovedCluster):
            return -1
        if isinstance(r, resources.Share):
            return 2
        if isinstance(r, resources.Cluster):
            return 1
        return 0

    return sorted(resource_objs, key=_keyfunc)


def _generate_share(
    share: resources.Share, resolver: PathResolver, cephx_entity: str
) -> Dict[str, Dict[str, str]]:
    cephfs = share.checked_cephfs
    assert cephfs.provider.is_vfs(), "not a vfs provider"
    assert cephx_entity, "cephx entity name missing"
    # very annoyingly, samba's ceph module absolutely must NOT have the
    # "client." bit in front. JJM has been tripped up by this multiple times -
    # seemingly every time this module is touched.
    _prefix = 'client.'
    plen = len(_prefix)
    if cephx_entity.startswith(_prefix):
        cephx_entity = cephx_entity[plen:]
    path = resolver.resolve(
        cephfs.volume,
        cephfs.subvolumegroup,
        cephfs.subvolume,
        cephfs.path,
    )
    try:
        ceph_vfs, proxy_val = {
            CephFSStorageProvider.SAMBA_VFS_CLASSIC: ('ceph', ''),
            CephFSStorageProvider.SAMBA_VFS_NEW: ('ceph_new', 'no'),
            CephFSStorageProvider.SAMBA_VFS_PROXIED: ('ceph_new', 'yes'),
        }[cephfs.provider.expand()]
    except KeyError:
        raise ValueError(f'unsupported provider: {cephfs.provider}')
    cfg = {
        # smb.conf options
        'options': {
            'path': path,
            "vfs objects": f"acl_xattr ceph_snapshots {ceph_vfs}",
            'acl_xattr:security_acl_name': 'user.NTACL',
            f'{ceph_vfs}:config_file': '/etc/ceph/ceph.conf',
            f'{ceph_vfs}:filesystem': cephfs.volume,
            f'{ceph_vfs}:user_id': cephx_entity,
            'read only': ynbool(share.readonly),
            'browseable': ynbool(share.browseable),
            'kernel share modes': 'no',
            'x:ceph:id': f'{share.cluster_id}.{share.share_id}',
            'smbd profiling share': 'yes',
        }
    }
    if share.comment is not None:
        cfg['options']['comment'] = share.comment

    if share.max_connections is not None:
        cfg['options']['max connections'] = str(share.max_connections)

    if proxy_val:
        cfg['options'][f'{ceph_vfs}:proxy'] = proxy_val
    # extend share with user+group login access lists
    _generate_share_login_control(share, cfg)
    # extend share with custom options
    custom_opts = share.cleaned_custom_smb_share_options
    if custom_opts:
        cfg['options'].update(custom_opts)
        cfg['options']['x:ceph:has_custom_options'] = 'yes'
    return cfg


def _generate_share_login_control(
    share: resources.Share, cfg: Simplified
) -> None:
    valid_users: List[str] = []
    invalid_users: List[str] = []
    read_list: List[str] = []
    write_list: List[str] = []
    admin_users: List[str] = []
    for entry in share.login_control or []:
        if entry.category == LoginCategory.GROUP:
            name = f'@{entry.name}'
        else:
            name = entry.name
        if entry.access == LoginAccess.NONE:
            invalid_users.append(name)
            continue
        elif entry.access == LoginAccess.ADMIN:
            admin_users.append(name)
        elif entry.access == LoginAccess.READ_ONLY:
            read_list.append(name)
        elif entry.access == LoginAccess.READ_WRITE:
            write_list.append(name)
        if share.restrict_access:
            valid_users.append(name)
    if valid_users:
        cfg['options']['valid users'] = ' '.join(valid_users)
    if invalid_users:
        cfg['options']['invalid users'] = ' '.join(invalid_users)
    if read_list:
        cfg['options']['read list'] = ' '.join(read_list)
    if write_list:
        cfg['options']['write list'] = ' '.join(write_list)
    if admin_users:
        cfg['options']['admin users'] = ' '.join(admin_users)


def _generate_config(
    cluster: resources.Cluster,
    shares: Iterable[resources.Share],
    resolver: PathResolver,
    cephx_entity: str = "",
) -> Dict[str, Any]:
    cluster_global_opts = {}
    if cluster.auth_mode == AuthMode.ACTIVE_DIRECTORY:
        assert cluster.domain_settings is not None
        cluster_global_opts['security'] = 'ads'
        cluster_global_opts['realm'] = cluster.domain_settings.realm
        # TODO: support alt. workgroup values
        wg = cluster.domain_settings.realm.upper().split('.')[0]
        cluster_global_opts['workgroup'] = wg
        cluster_global_opts['idmap config * : backend'] = 'autorid'
        cluster_global_opts['idmap config * : range'] = '2000-9999999'
    if cluster.is_clustered() and cluster.custom_ports:
        # a ctdb enabled cluster (w/ host networking) with custom ports needs
        # to change the port at the smbd level
        cluster_global_opts['smb ports'] = str(_smb_port(cluster))

    share_configs = {
        share.name: _generate_share(share, resolver, cephx_entity)
        for share in shares
    }

    instance_features = []
    if cluster.is_clustered():
        instance_features.append('ctdb')
    cfg: Dict[str, Any] = {
        'samba-container-config': 'v0',
        'configs': {
            cluster.cluster_id: {
                'instance_name': cluster.cluster_id,
                'instance_features': instance_features,
                'globals': ['default', cluster.cluster_id],
                'shares': list(share_configs.keys()),
            },
        },
        'globals': {
            'default': {
                'options': {
                    'load printers': 'No',
                    'printing': 'bsd',
                    'printcap name': '/dev/null',
                    'disable spoolss': 'Yes',
                    'smbd profiling level': 'on',
                }
            },
            cluster.cluster_id: {
                'options': cluster_global_opts,
            },
        },
        'shares': share_configs,
    }
    # insert global custom options
    custom_opts = cluster.cleaned_custom_smb_global_options
    if custom_opts:
        # isolate custom config opts into a section for cleanliness
        gname = f'{cluster.cluster_id}_custom'
        cfg['configs'][cluster.cluster_id]['globals'].append(gname)
        cfg['globals'][gname] = {'options': dict(custom_opts)}
    return cfg


def _generate_smb_service_spec(
    cluster: resources.Cluster,
    *,
    config_entries: List[ConfigEntry],
    join_source_entries: List[ConfigEntry],
    user_source_entries: List[ConfigEntry],
    data_entity: str = '',
    needs_proxy: bool = False,
) -> SMBSpec:
    features = []
    if cluster.auth_mode == AuthMode.ACTIVE_DIRECTORY:
        features.append(_DOMAIN)
    if cluster.is_clustered():
        features.append(_CLUSTERED)
    if needs_proxy:
        features.append(_CEPHFS_PROXY)
    # only one config uri can be used, the input list should be
    # ordered from lowest to highest priority and the highest priority
    # item that exists in the store will be used.
    config_uri = ''
    for entry in config_entries:
        if entry.exists():
            config_uri = entry.uri
    if not config_uri:
        raise ValueError('no samba container configuration available')
    # collect the the uris for the join sources
    join_sources: List[str] = []
    for entry in join_source_entries:
        # if entry.exists():
        join_sources.append(entry.uri)
    # collect the uris for the user sources
    user_sources: List[str] = []
    for entry in user_source_entries:
        user_sources.append(entry.uri)
    user_entities: Optional[List[str]] = None
    if data_entity:
        user_entities = [data_entity]
    return SMBSpec(
        service_id=cluster.cluster_id,
        placement=cluster.placement,
        cluster_id=cluster.cluster_id,
        features=features,
        config_uri=config_uri,
        join_sources=join_sources,
        user_sources=user_sources,
        custom_dns=cluster.custom_dns,
        include_ceph_users=user_entities,
        cluster_public_addrs=cluster.service_spec_public_addrs(),
        custom_ports=cluster.custom_ports,
        bind_addrs=cluster.service_spec_bind_addrs(),
    )


def _swap_pending_cluster_info(
    store: ConfigStore,
    change_group: ClusterChangeGroup,
    orch_needed: bool,
) -> Simplified:
    # TODO: its not just a  placeholder any more. rename the key func!
    pentry = store[
        external.cluster_placeholder_key(change_group.cluster.cluster_id)
    ]
    try:
        existing = pentry.get()
    except KeyError:
        existing = {}
    pentry.set(
        {
            'cluster_id': change_group.cluster.cluster_id,
            'timestamp': int(time.time()),
            'orch_needed': orch_needed,
        }
    )
    change_group.cache_updated_entry(pentry)
    return existing


def _save_pending_join_auths(
    store: ConfigStore,
    change_group: ClusterChangeGroup,
) -> None:
    cluster = change_group.cluster
    assert isinstance(cluster, resources.Cluster)
    # save each join auth source in the priv store
    if cluster.auth_mode != AuthMode.ACTIVE_DIRECTORY:
        return
    arefs = {j.auth_id: j for j in change_group.join_auths}
    for idx, src in enumerate(checked(cluster.domain_settings).join_sources):
        if src.source_type == JoinSourceType.RESOURCE:
            javalues = checked(arefs[src.ref].auth)
        else:
            raise ValueError(
                f'unsupported join source type: {src.source_type}'
            )
        jentry = store[external.join_source_key(cluster.cluster_id, str(idx))]
        jentry.set(javalues.to_simplified())
        change_group.cache_updated_entry(jentry)


def _save_pending_users_and_groups(
    store: ConfigStore,
    change_group: ClusterChangeGroup,
) -> None:
    cluster = change_group.cluster
    assert isinstance(cluster, resources.Cluster)
    # save each users-and-groups settings in the priv store
    if cluster.auth_mode != AuthMode.USER:
        return
    augs = {ug.users_groups_id: ug for ug in change_group.users_and_groups}
    for idx, ugsv in enumerate(checked(cluster.user_group_settings)):
        if ugsv.source_type == UserGroupSourceType.RESOURCE:
            ugvalues = augs[ugsv.ref].values
            assert ugvalues
        elif ugsv.source_type == UserGroupSourceType.EMPTY:
            continue
        else:
            raise ValueError(
                f'unsupported users/groups source type: {ugsv.source_type}'
            )
        ugentry = store[
            external.users_and_groups_key(cluster.cluster_id, str(idx))
        ]
        ugsimple = ugvalues.to_simplified()
        ug_config: Simplified = {'samba-container-config': 'v0'}
        if 'users' in ugsimple:
            ug_config['users'] = {'all_entries': ugsimple['users']}
        if 'groups' in ugsimple:
            ug_config['groups'] = {'all_entries': ugsimple['groups']}
        ugentry.set(ug_config)
        change_group.cache_updated_entry(ugentry)


def _save_pending_config(
    store: ConfigStore,
    change_group: ClusterChangeGroup,
    resolver: PathResolver,
    cephx_entity: str = "",
) -> None:
    assert isinstance(change_group.cluster, resources.Cluster)
    # generate the cluster configuration and save it in the public store
    cconfig = _generate_config(
        change_group.cluster, change_group.shares, resolver, cephx_entity
    )
    centry = store[external.config_key(change_group.cluster.cluster_id)]
    centry.set(cconfig)
    change_group.cache_updated_entry(centry)


def _save_pending_spec_backup(
    store: ConfigStore, change_group: ClusterChangeGroup, smb_spec: SMBSpec
) -> None:
    ssentry = store[external.spec_backup_key(change_group.cluster.cluster_id)]
    ssentry.set(smb_spec.to_json())
    change_group.cache_updated_entry(ssentry)


def _cephx_data_entity(cluster_id: str) -> str:
    """Generate a name for the (default?) cephx key that a cluster (smbd) will
    use for data access.
    """
    return f'client.smb.fs.cluster.{cluster_id}'


@contextlib.contextmanager
def _store_transaction(store: ConfigStore) -> Iterator[None]:
    transaction = getattr(store, 'transaction', None)
    if not transaction:
        log.debug("No transaction support for store")
        yield None
        return
    log.debug("Using store transaction")
    with transaction():
        yield None


def _has_proxied_vfs(change_group: ClusterChangeGroup) -> bool:
    """Return true if any shares in the change group use the new vfs module
    with the proxied cephfs library.
    """
    return any(
        s.checked_cephfs.provider.expand()
        == CephFSStorageProvider.SAMBA_VFS_PROXIED
        for s in change_group.shares
    )


def _smb_port(cluster: resources.Cluster, default: int = 445) -> int:
    return (cluster.custom_ports or {}).get("smb", default)
