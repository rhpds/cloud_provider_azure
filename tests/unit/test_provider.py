"""Offline behavioral checks; no real Azure credentials or network access."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('provider', ROOT / 'plugins/module_utils/provider.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.responses = []
        self.auth = dict(subscription_id='subscription', app_id='application', password='SECRET', tenant='tenant')
        self.az = provider.Azure(self.auth, self.tmp.name, 'project', runner=self.run_cli)
        self.s = dict(infra_group='infra', cluster_group='cluster', cluster_name='aro-test',
                      region='eastus', method='aro', identity_mode='managed_identity')

    def run_cli(self, command, **kwargs):
        self.calls.append(command)
        code, value = self.responses.pop(0)
        return SimpleNamespace(returncode=code, stdout=json.dumps(value) if code == 0 else '',
                               stderr=value if code else '')

    def test_subscription_explicit_and_cli_isolated(self):
        self.responses = [(0, {})]
        self.az.call('vm', 'list')
        self.assertIn('--subscription', self.calls[0])
        self.assertEqual(self.az.env['AZURE_CONFIG_DIR'], self.tmp.name)
        self.assertEqual(self.az.env['AZURE_EXTENSION_USE_DYNAMIC_INSTALL'], 'no')

    def test_login_uses_supplied_credentials(self):
        self.responses = [(0, [])]
        self.az.login()
        self.assertIn('--password=SECRET', self.calls[0])
        self.assertNotIn('--subscription', self.calls[0])

    def test_auth_failure_never_means_missing(self):
        self.responses = [(1, 'Forbidden SECRET')]
        with self.assertRaisesRegex(provider.AzureError, 'Forbidden <redacted>'):
            self.az.call('group', 'show', missing=True)

    def test_not_found_is_idempotent(self):
        self.responses = [(1, 'ResourceGroupNotFound')]
        self.assertIsNone(self.az.group('infra', 'eastus'))

    def test_missing_arm_deployment_allows_initial_creation(self):
        self.responses = [(1, "ERROR: (DeploymentNotFound) Deployment 'bastion' could not be found.\n"
                              "Code: DeploymentNotFound\n"
                              "Message: Deployment 'bastion' could not be found.\n")]
        self.assertIsNone(self.az.call('deployment', 'group', 'show', '-g', 'infra', '-n', 'bastion', missing=True))

    def test_missing_arm_deployment_is_fatal_unless_explicitly_allowed(self):
        self.responses = [(1, 'ERROR: (DeploymentNotFound) Deployment could not be found.')]
        with self.assertRaisesRegex(provider.AzureError, 'DeploymentNotFound'):
            self.az.call('deployment', 'group', 'show', '-g', 'infra', '-n', 'bastion')

    def test_unowned_group_cannot_be_deleted(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'someone-else'}})]
        with self.assertRaisesRegex(provider.AzureError, 'unowned'):
            self.az.delete_group('infra', 'eastus')
        self.assertEqual(len(self.calls), 1)

    def test_owned_group_can_be_deleted(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}), (0, None)]
        self.az.delete_group('infra', 'eastus')
        self.assertTrue(self.az.changed)
        self.assertEqual(self.calls[1][1:3], ['group', 'delete'])

    def test_sandbox_group_cannot_be_targeted(self):
        with self.assertRaisesRegex(provider.AzureError, 'distinct'):
            provider.validate_names(self.s, 'INFRA')

    def test_aro_lifecycle_does_not_power_managed_vms(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}),
                          (0, [{'id': '/bastion', 'tags': {'agnosticd_project': 'project'}}]),
                          (0, None), (0, {'tags': {'agnosticd_project': 'project'}}),
                          (0, {'provisioningState': 'Succeeded'})]
        self.az.lifecycle(self.s, 'stop')
        power = [x for x in self.calls if x[1:3] == ['vm', 'deallocate']]
        self.assertEqual(len(power), 1)
        self.assertIn('/bastion', power[0])

    def test_failed_aro_does_not_get_recreated(self):
        self.responses = [(0, {'provisioningState': 'Failed'})]
        with self.assertRaisesRegex(provider.AzureError, 'Failed'):
            self.az.wait_cluster(self.s)
        self.assertEqual(len(self.calls), 1)

    def test_aro_deletion_failure_preserves_identity_group(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}),
                          (0, {'provisioningState': 'Succeeded'}), (1, 'Conflict')]
        with self.assertRaisesRegex(provider.AzureError, 'Conflict'):
            self.az.aro_destroy(self.s)
        self.assertFalse(any(x[1:3] == ['group', 'delete'] for x in self.calls))

    def test_aro_already_absent_removes_leftover_identity_group(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}),
                          (1, 'ResourceNotFound'),
                          (0, {'tags': {'agnosticd_project': 'project'}}), (0, None)]
        self.az.aro_destroy(self.s)
        self.assertTrue(self.az.changed)

    def test_repeated_aro_destroy_is_noop(self):
        self.responses = [(1, 'ResourceGroupNotFound')]
        self.az.aro_destroy(self.s)
        self.assertFalse(self.az.changed)

    def test_overlapping_aro_subnets_fail_before_resource_creation(self):
        self.s.update(vnet_cidr='10.0.0.0/22', master_cidr='10.0.0.0/23',
                      worker_cidr='10.0.0.0/23', pod_cidr='10.128.0.0/14', service_cidr='172.30.0.0/16')
        with self.assertRaisesRegex(provider.AzureError, 'overlap'):
            self.az.aro_install(self.s)
        self.assertFalse(self.calls)

    def test_managed_group_cannot_overlap_sandbox(self):
        with self.assertRaisesRegex(provider.AzureError, 'distinct'):
            provider.validate_names(self.s, 'cluster-managed')

    def test_incomplete_quota_fails_before_creation(self):
        self.s.update(version='4.20.1', master_size='master', worker_size='worker', worker_count=3)
        skus = [dict(name=size, family='family', restrictions=[], capabilities=[dict(name='vCPUs', value='8')])
                for size in ('master', 'worker')]
        self.responses = [(0, {'registrationState': 'Registered'})] * 6 + [
            (0, ['4.20.1']), (0, skus),
            (0, [dict(name=dict(value='family'), limit=10, currentValue=0)])]
        with self.assertRaisesRegex(provider.AzureError, 'quota'):
            self.az.preflight(self.s)
        self.assertFalse(any('create' in x for x in self.calls))


if __name__ == '__main__':
    unittest.main()
