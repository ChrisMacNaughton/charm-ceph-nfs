#!/usr/bin/env python3
# Copyright 2021 OpenStack Charmers
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from ops.framework import StoredState
from ops.main import main
# from ops.model import ActiveStatus

import charmhelpers.core.host as ch_host
import charmhelpers.core.templating as ch_templating
import interface_ceph_client.ceph_client as ceph_client
import interface_ceph_nfs_peer
# TODO: Add the below class functionaity to action / relations
from ganesha import GaneshaNfs

import ops_openstack.adapters
import ops_openstack.core
import ops_openstack.plugins.classes

logger = logging.getLogger(__name__)


class CephClientAdapter(ops_openstack.adapters.OpenStackOperRelationAdapter):
    """Adapter for ceph client interface."""

    @property
    def mon_hosts(self):
        """Sorted list of ceph mon addresses.

        :returns: Ceph MON addresses.
        :rtype: str
        """
        hosts = self.relation.get_relation_data()['mon_hosts']
        return ' '.join(sorted(hosts))

    @property
    def auth_supported(self):
        """Authentication type.

        :returns: Authentication type
        :rtype: str
        """
        return self.relation.get_relation_data()['auth']

    @property
    def key(self):
        """Key client should use when communicating with Ceph cluster.

        :returns: Key
        :rtype: str
        """
        return self.relation.get_relation_data()['key']


class CephNFSContext(object):
    """Adapter for ceph NFS config."""

    name = 'ceph_nfs'

    def __init__(self, charm_instance):
        self.charm_instance = charm_instance

    @property
    def pool_name(self):
        """The name of the default rbd data pool to be used for shares.

        :returns: Data pool name.
        :rtype: str
        """
        return self.charm_instance.config_get('rbd-pool-name', self.charm_instance.app.name)

    @property
    def client_name(self):
        return self.charm_instance.app.name

    @property
    def hostname(self):
        return socket.gethostname()


class CephNFSAdapters(
        ops_openstack.adapters.OpenStackRelationAdapters):
    """Collection of relation adapters."""

    relation_adapters = {
        'ceph-client': CephClientAdapter,
    }


class CephNfsCharm(
        ops_openstack.plugins.classes.BaseCephClientCharm):
    """Ceph NFS Base Charm."""

    PACKAGES = ['nfs-ganesha-ceph', 'nfs-ganesha-rados-grace', 'ceph-common']

    CEPH_CAPABILITIES = [
        "mgr", "allow rw",
        "mds", "allow *",
        "osd", "allow rw",
        "mon", "allow r, "
        "allow command \"auth del\", "
        "allow command \"auth caps\", "
        "allow command \"auth get\", "
        "allow command \"auth get-or-create\""]

    REQUIRED_RELATIONS = ['ceph-client']

    CEPH_CONFIG_PATH = Path('/etc/ceph')
    GANESHA_CONFIG_PATH = Path('/etc/ganesha')

    CEPH_GANESHA_CONFIG_PATH = CEPH_CONFIG_PATH / 'ganesha'
    CEPH_CONF = CEPH_CONFIG_PATH / 'ceph.conf'
    GANESHA_KEYRING = CEPH_GANESHA_CONFIG_PATH / 'ceph.keyring'
    GANESHA_CONF = GANESHA_CONFIG_PATH / 'ganesha.conf'

    SERVICES = ['nfs-ganesha']

    RESTART_MAP = {
        str(GANESHA_CONF): SERVICES,
        str(CEPH_CONF): SERVICES,
        str(GANESHA_KEYRING): SERVICES}

    release = 'default'

    def __init__(self, framework):
        super().__init__(framework)
        # super().register_status_check(self.custom_status_check)
        logging.info("Using %s class", self.release)
        self._stored.set_default(
            is_started=False,
            is_cluster_setup=False
        )
        self.ceph_client = ceph_client.CephClientRequires(
            self,
            'ceph-client')
        self.peers = interface_ceph_nfs_peer.CephNfsPeers(
            self,
            'cluster')
        self.adapters = CephNFSAdapters(
            (self.ceph_client, self.peers),
            contexts=(CephNFSContext(self),),
            charm_instance=self)
        self.framework.observe(
            self.ceph_client.on.broker_available,
            self.request_ceph_pool)
        self.framework.observe(
            self.ceph_client.on.pools_available,
            self.render_config)
        self.framework.observe(
            self.on.config_changed,
            self.request_ceph_pool)
        self.framework.observe(
            self.on.upgrade_charm,
            self.render_config)
        self.framework.observe(
            self.ceph_client.on.pools_available,
            self.setup_ganesha),
        self.framework.observe(
            self.peers.on.pool_initialised,
            self.on_pool_initialised)
        self.framework.observe(
            self.peers.on.departing,
            self.on_departing)
        self.framework.observe(
            self.peers.on.reload_nonce,
            self.on_reload_nonce)
        # Actions
        self.framework.observe(
            self.on.create_share_action,
            self.create_share_action)

    def config_get(self, key, default=None):
        """Retrieve config option.

        :returns: Value of the corresponding config option or None.
        :rtype: Any
        """
        return self.model.config.get(key, default)

    @property
    def pool_name(self):
        """The name of the default rbd data pool to be used for shares.

        :returns: Data pool name.
        :rtype: str
        """
        return self.config_get('rbd-pool-name', self.app.name)

    @property
    def client_name(self):
        return self.app.name

    def request_ceph_pool(self, event):
        """Request pools from Ceph cluster."""
        if not self.ceph_client.broker_available:
            logging.info("Cannot request ceph setup at this time")
            return
        try:
            bcomp_kwargs = self.get_bluestore_compression()
        except ValueError as e:
            # The end user has most likely provided a invalid value for
            # a configuration option. Just log the traceback here, the
            # end user will be notified by assess_status() called at
            # the end of the hook execution.
            logging.warn('Caught ValueError, invalid value provided for '
                         'configuration?: "{}"'.format(str(e)))
            return
        weight = self.config_get('ceph-pool-weight')
        replicas = self.config_get('ceph-osd-replication-count')

        logging.info("Requesting replicated pool")
        self.ceph_client.create_replicated_pool(
            name=self.pool_name,
            app_name='ganesha',
            replicas=replicas,
            weight=weight,
            **bcomp_kwargs)
        logging.info("Requesting permissions")
        self.ceph_client.request_ceph_permissions(
            self.client_name,
            self.CEPH_CAPABILITIES)

    def refresh_request(self, event):
        """Re-request Ceph pools and render config."""
        self.render_config(event)
        self.request_ceph_pool(event)

    def render_config(self, event):
        """Render config and restart services if config files change."""
        if not self.ceph_client.pools_available:
            logging.info("Defering setup")
            event.defer()
            return

        self.CEPH_GANESHA_CONFIG_PATH.mkdir(
            exist_ok=True,
            mode=0o750)

        def daemon_reload_and_restart(service_name):
            subprocess.check_call(['systemctl', 'daemon-reload'])
            subprocess.check_call(['systemctl', 'restart', service_name])

        rfuncs = {}

        @ch_host.restart_on_change(self.RESTART_MAP, restart_functions=rfuncs)
        def _render_configs():
            for config_file in self.RESTART_MAP.keys():
                ch_templating.render(
                    os.path.basename(config_file),
                    config_file,
                    self.adapters)
        logging.info("Rendering config")
        _render_configs()
        logging.info("Setting started state")
        self._stored.is_started = True
        self.update_status()
        logging.info("on_pools_available: status updated")

    def on_departing(self, event):
        subprocess.check_call([
            'ganesha-rados-grace', '--userid', self.client_name,
            '--cephconf', self.CEPH_CONF, '--pool', self.pool_name,
            'remove', socket.gethostname()])
        self._stored.is_cluster_setup = False

    def setup_ganesha(self, event):
        if not self._stored.is_cluster_setup:
            subprocess.check_call([
                'ganesha-rados-grace', '--userid', self.client_name,
                '--cephconf', self.CEPH_CONF, '--pool', self.pool_name,
                'add', socket.gethostname()])
            self._stored.is_cluster_setup = True
        if not self.model.unit.is_leader():
            return
        cmd = [
            'rados', '-p', self.pool_name,
            '-c', self.CEPH_CONF,
            '--id', self.client_name,
            'put', 'ganesha-export-index', '/dev/null'
        ]
        try:
            subprocess.check_call(cmd)
            counter = tempfile.NamedTemporaryFile('w+')
            counter.write('1000')
            counter.seek(0)
            cmd = [
                'rados', '-p', self.pool_name,
                '-c', self.CEPH_CONF,
                '--id', self.client_name,
                'put', 'ganesha-export-counter', counter.name
            ]
            subprocess.check_call(cmd)
            self.peers.initialised_pool()
        except subprocess.CalledProcessError:
            logging.error("Failed to setup ganesha index object")
            event.defer()

    def on_pool_initialised(self, event):
        try:
            subprocess.check_call(['systemctl', 'restart', 'nfs-ganesha'])
        except subprocess.CalledProcessError:
            logging.error("Failed torestart nfs-ganesha")
            event.defer()

    def on_reload_nonce(self, _event):
        logging.info("Reloading Ganesha after nonce triggered reload")
        subprocess.call(['killall', '-HUP', 'ganesha.nfsd'])

    def access_address(self) -> str:
        """Return the IP to advertise Ganesha on"""
        binding = self.model.get_binding('public')
        if self.model.get_relation('hacluster'):
            return self.config_get('vip')
        else:
            return str(binding.network.ingress_address)

    def create_share_action(self, event):
        if not self.model.unit.is_leader():
            event.fail("Share creation needs to be run from the application leader")
            return
        share_size = event.params.get('size')
        client = GaneshaNfs(self.client_name, self.pool_name)
        export_path = client.create_share(size=share_size)
        self.peers.trigger_reload()
        event.set_results({"message": "Share created", "path": export_path, "ip": self.access_address()})

@ops_openstack.core.charm_class
class CephNFSCharmOcto(CephNfsCharm):
    """Ceph iSCSI Charm for Octopus."""

    _stored = StoredState()
    release = 'octopus'


if __name__ == '__main__':
    main(ops_openstack.core.get_charm_class_for_release())
