# Copyright 2019 Canonical Ltd.
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

"""Encapsulate policyd testing."""

import logging
import os
import shutil
import tempfile
import zipfile

import keystoneauth1

import zaza.model as zaza_model

import zaza.openstack.charm_tests.test_utils as test_utils
import zaza.openstack.utilities.openstack as openstack_utils
import zaza.openstack.charm_tests.keystone as ch_keystone
import zaza.openstack.utilities.exceptions as zaza_exceptions


class PolicydTest(object):
    """Charm operation tests.

    The policyd test needs some config from the tests.yaml in order to work
    properly.  A top level key of "tests_options".  Under that key is
    'policyd', and then the k:v of 'service': <name>.  e.g. for keystone

    tests_options:
      policyd:
        service: keystone
    """

    @classmethod
    def setUpClass(cls, application_name=None):
        super(PolicydTest, cls).setUpClass(application_name)
        if (openstack_utils.get_os_release() <
                openstack_utils.get_os_release('xenial_queens')):
            cls.SkipTest("Test not valid before xenial_queens")
        cls._tmp_dir = tempfile.mkdtemp()
        cls._service_name = \
            cls.test_config['tests_options']['policyd']['service']

    @classmethod
    def tearDownClass(cls):
        super(PolicydTest, cls).tearDownClass()
        try:
            shutil.rmtree(cls._tmp_dir, ignore_errors=True)
        except Exception as e:
            logging.error("Removing the policyd tempdir/files failed: {}"
                          .format(str(e)))

    def _set_config(self, state):
        s = "True" if state else "False"
        config = {"use-policyd-override": s}
        logging.info("Setting config to {}".format(config))
        zaza_model.set_application_config(self.application_name, config)

    def _make_zip_file_from(self, name, files):
        """Make a zip file from a dictionary of filename: string.

        :param name: the name of the zip file
        :type name: PathLike
        :param files: a dict of name: string to construct the files from.
        :type files: Dict[str, str]
        :returns: temp file that is the zip file.
        :rtype: PathLike
        """
        path = os.path.join(self._tmp_dir, name)
        with zipfile.ZipFile(path, "w") as zfp:
            for name, contents in files.items():
                zfp.writestr(name, contents)
        return path

    def _set_policy_with(self, rules):
        rules_zip_path = self._make_zip_file_from('rules.zip', rules)
        zaza_model.attach_resource(self.application_name,
                                   'policyd-override',
                                   rules_zip_path)
        self._set_config(True)
        zaza_model.block_until_wl_status_info_starts_with(
            self.application_name, "PO:", negate_match=True)

    def test_001_policyd_good_yaml(self):
        # Test that the policyd with a good zipped yaml file puts the yaml file
        # in the right directory
        good = {
            'file1.yaml': "{'rule1': '!'}"
        }
        good_zip_path = self._make_zip_file_from('good.zip', good)
        logging.info("Attaching a resource.")
        zaza_model.attach_resource(self.application_name,
                                   'policyd-override',
                                   good_zip_path)
        logging.debug("... waiting for idle")
        zaza_model.block_until_all_units_idle()
        logging.debug("Now setting config to true")
        self._set_config(True)
        # check that the file gets to the right location
        path = os.path.join(
            "/etc", self._service_name, "policy.d", 'file1.yaml')
        logging.info("Now checking for file contents: {}".format(path))
        zaza_model.block_until_file_has_contents(self.application_name,
                                                 path,
                                                 "rule1: '!'")
        # ensure that the workload status info line starts with PO:
        logging.info("Checking for workload status line starts with PO:")
        zaza_model.block_until_wl_status_info_starts_with(
            self.application_name, "PO:")
        logging.debug("App status is valid")

        # disable the policy override
        logging.info("Disabling policy override by setting config to false")
        self._set_config(False)
        # check that the status no longer has "PO:" on it.
        # we have to do it twice due to async races and that some info lines
        # erase the PO: bit prior to actuall getting back to idle.  The double
        # check verifies that the charms have started, the idle waits until it
        # is finiehed, and then the final check really makes sure they got
        # switched off.
        zaza_model.block_until_wl_status_info_starts_with(
            self.application_name, "PO:", negate_match=True)
        zaza_model.block_until_all_units_idle()
        zaza_model.block_until_wl_status_info_starts_with(
            self.application_name, "PO:", negate_match=True)

        # verify that the file no longer exists
        logging.info("Checking that {} has been removed".format(path))
        zaza_model.block_until_file_missing(self.application_name, path)

        logging.info("...done")


class KeystonePolicydTest(PolicydTest,
                          ch_keystone.BaseKeystoneTest,
                          test_utils.OpenStackBaseTest):

    @classmethod
    def setUpClass(cls, application_name=None):
        super(KeystonePolicydTest, cls).setUpClass(application_name)

    def test_disable_service(self):
        logging.info("Doing policyd override to disable listing domains")
        self._set_policy_with(
            {'rule.yaml': "{'identity:get_auth_domains': '!'}"})
        with self.config_change(
                {'preferred-api-version': self.default_api_version,
                 'use-policyd-override': 'False'},
                {'preferred-api-version': '3',
                 'use-policyd-override': 'True'},
                application_name="keystone"):
            for ip in self.keystone_ips:
                try:
                    logging.info('keystone IP {}'.format(ip))
                    ks_session = openstack_utils.get_keystone_session(
                        openstack_utils.get_overcloud_auth(address=ip))
                    ks_client = openstack_utils.get_keystone_session_client(
                        ks_session)
                    ks_client.domains.list()
                    raise zaza_exceptions.PolicydError(
                        'Retrieve domain list as admin with project scoped '
                        'token passed and should have failed. IP = {}'
                        .format(ip))
                except keystoneauth1.exceptions.http.Forbidden:
                    logging.info("keystone IP:{} policyd override working"
                                 .format(ip))

            logging.info('OK')


class GenericPolicydTest(PolicydTest, test_utils.OpenStackBaseTest):
    pass
