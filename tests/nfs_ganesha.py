# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Encapsulate ``Ceph NFS`` testing."""

import logging
import subprocess
import tenacity
from typing import Dict
import unittest
import yaml
import zaza
import zaza.utilities.installers


class NfsGaneshaTest(unittest.TestCase):
    mount_dir = '/mnt/test'
    share_protocol = 'nfs'
    mounts_share = False
    created_share = None

    def tearDown(self):
        if self.mounts_share:
            try:
                zaza.utilities.generic.run_via_ssh(
                    unit_name='ubuntu/0',
                    cmd='sudo umount /mnt/test && sudo rmdir /mnt/test')
                zaza.utilities.generic.run_via_ssh(
                    unit_name='ubuntu/1',
                    cmd='sudo umount /mnt/test && sudo rmdir /mnt/test')
            except subprocess.CalledProcessError:
                logging.warning("Failed to cleanup mounts")
        if self.created_share:
            zaza.model.run_action_on_leader(
                'ceph-nfs',
                'delete-share',
                action_params={
                    'name': self.created_share,
                })

    def _create_share(self, name: str, size: int = 10) -> Dict[str, str]:
        action = zaza.model.run_action_on_leader(
            'ceph-nfs',
            'create-share',
            action_params={
                'name': name,
                'size': size,
            })
        self.assertEqual(action.status, 'completed')
        self.created_share = name
        results = action.results
        logging.debug("Action results: {}".format(results))
        return results

    def _mount_share(self, unit_name: str, share_ip: str, export_path: str):
        ssh_cmd = (
            'sudo mkdir -p {0} && '
            'sudo mount -t {1} -o nfsvers=4.1,proto=tcp {2}:{3} {0}'.format(
                self.mount_dir,
                self.share_protocol,
                share_ip,
                export_path))

        for attempt in tenacity.Retrying(
                stop=tenacity.stop_after_attempt(5),
                wait=tenacity.wait_exponential(multiplier=3, min=2, max=10)):
            with attempt:
                zaza.utilities.generic.run_via_ssh(
                    unit_name=unit_name,
                    cmd=ssh_cmd)
        self.mounts_share = True

    def _install_dependencies(self, unit: str):
        logging.debug("About to install nfs-common on {}".format(unit))
        zaza.utilities.generic.run_via_ssh(
            unit_name=unit,
            cmd='sudo apt-get install -yq nfs-common')

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=3, min=2, max=10))
    def _write_testing_file_on_instance(self, instance_name: str):
        zaza.utilities.generic.run_via_ssh(
            unit_name=instance_name,
            cmd='echo "test" | sudo tee {}/test'.format(self.mount_dir))

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=3, min=2, max=10))
    def _verify_testing_file_on_instance(self, instance_name: str):
        run_with_juju_ssh = zaza.utilities.installers.make_juju_ssh_fn('ubuntu/1', sudo=True)
        output = run_with_juju_ssh(
            'sudo cat {}/test'.format(self.mount_dir))
        logging.info("Verification output: {}".format(output))
        self.assertEqual('test\r\n', output)

    def test_create_share(self):
        for unit in ['0', '1']:
            self._install_dependencies('ubuntu/{}'.format(unit))
        logging.info("Creating a share")
        share = self._create_share('test_ganesha_share')
        export_path = share['path']
        ip = share['ip']
        logging.info("Mounting share on ubuntu units")
        self._mount_share('ubuntu/0', ip, export_path)
        self._mount_share('ubuntu/1', ip, export_path)
        logging.info("writing to the share on ubuntu/0")
        self._write_testing_file_on_instance('ubuntu/0')
        logging.info("reading from the share on ubuntu/1")
        self._verify_testing_file_on_instance('ubuntu/1')

    def test_list_shares(self):
        self._create_share('test_ganesha_list_share')
        action = zaza.model.run_action_on_leader(
            'ceph-nfs',
            'list-shares',
            action_params={})
        self.assertEqual(action.status, 'completed')
        results = action.results
        logging.debug("Action results: {}".format(results))
        logging.debug("exports: {}".format(results['exports']))
        exports = yaml.safe_load(results['exports'])
        self.assertIn('test_ganesha_list_share', [export['name'] for export in exports])
