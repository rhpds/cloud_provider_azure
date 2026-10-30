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
        if '--body' in command:
            self.last_body = json.loads(Path(command[command.index('--body') + 1][1:]).read_text())
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

    def test_missing_dns_record_allows_initial_creation(self):
        self.responses = [(1, "ERROR: (NotFound) The resource record 'bastion' does not exist.\n"
                              "Code: NotFound\nMessage: The resource record 'bastion' does not exist.\n")]
        self.assertIsNone(self.az.call('network', 'dns', 'record-set', 'a', 'show', missing=True))

    def test_missing_dns_record_is_fatal_unless_explicitly_allowed(self):
        self.responses = [(1, 'ERROR: (NotFound) The resource record does not exist.')]
        with self.assertRaisesRegex(provider.AzureError, 'NotFound'):
            self.az.call('network', 'dns', 'record-set', 'a', 'show')

    def test_authorization_error_mentioning_absence_is_not_ignored(self):
        self.responses = [(1, 'ERROR: (AuthorizationFailed) Cannot determine whether ResourceNotFound applies.\n'
                              'Code: AuthorizationFailed\n')]
        with self.assertRaisesRegex(provider.AzureError, 'AuthorizationFailed'):
            self.az.call('network', 'dns', 'record-set', 'a', 'show', missing=True)

    def test_only_exact_absence_codes_are_ignored(self):
        for code in ('ResourceNotFound', 'ResourceGroupNotFound', 'ParentResourceNotFound',
                     'DeploymentNotFound', 'NotFound'):
            for error in (code, 'ERROR: (' + code + ') Missing resource.', 'Code: ' + code + '\n'):
                with self.subTest(error=error):
                    self.responses = [(1, error)]
                    self.assertIsNone(self.az.call('resource', 'show', missing=True))
        self.responses = [(1, 'ERROR: (NotFoundInCache) Network lookup failed.')]
        with self.assertRaisesRegex(provider.AzureError, 'NotFoundInCache'):
            self.az.call('resource', 'show', missing=True)

    def test_unowned_group_cannot_be_deleted(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'someone-else'}})]
        with self.assertRaisesRegex(provider.AzureError, 'unowned'):
            self.az.delete_group('infra', 'eastus')
        self.assertEqual(len(self.calls), 1)

    def test_dns_creation_writes_ip_and_ownership_atomically(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        self.responses = [(1, 'NotFound'), (0, {'properties': {'metadata': {'agnosticd_project': 'project'}}})]
        self.az.ensure_bastion_dns(self.s, '192.0.2.1')
        self.assertEqual(self.last_body['properties']['metadata'], {'agnosticd_project': 'project'})
        self.assertEqual(self.last_body['properties']['ARecords'], [{'ipv4Address': '192.0.2.1'}])
        self.assertIn('If-None-Match=*', self.calls[1])
        self.assertTrue(self.az.changed)

    def test_dns_retry_does_not_rewrite_owned_record(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        self.responses = [(0, {'metadata': {'agnosticd_project': 'project'},
                               'ARecords': [{'ipv4Address': '192.0.2.1'}]})]
        self.az.ensure_bastion_dns(self.s, '192.0.2.1')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.az.changed)

    def test_dns_update_preserves_metadata_and_checks_etag(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        metadata = {'agnosticd_project': 'project', 'other': 'retained'}
        self.responses = [(0, {'metadata': metadata, 'ARecords': [{'ipv4Address': '192.0.2.2'}],
                               'TTL': 60, 'etag': 'original-etag'}),
                          (0, {'properties': {'metadata': metadata}})]
        self.az.ensure_bastion_dns(self.s, '192.0.2.1')
        self.assertIn('If-Match=original-etag', self.calls[1])
        self.assertEqual(self.last_body['properties']['metadata'], metadata)
        self.assertEqual(self.last_body['properties']['TTL'], 60)

    def test_dns_without_ownership_is_never_adopted(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        for metadata in (None, {}, {'agnosticd_project': 'other-project'}):
            with self.subTest(metadata=metadata):
                self.calls.clear()
                self.responses = [(0, {'metadata': metadata, 'ARecords': [{'ipv4Address': '192.0.2.1'}]})]
                with self.assertRaisesRegex(provider.AzureError, 'unowned'):
                    self.az.ensure_bastion_dns(self.s, '192.0.2.1')
                self.assertEqual(len(self.calls), 1)

    def test_dns_conflict_is_not_retried_as_an_unconditional_write(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        self.responses = [(1, 'NotFound'), (1, 'ERROR: (PreconditionFailed) Record changed concurrently.')]
        with self.assertRaisesRegex(provider.AzureError, 'PreconditionFailed'):
            self.az.ensure_bastion_dns(self.s, '192.0.2.1')
        self.assertEqual(len(self.calls), 2)

    def test_dns_missing_ownership_in_response_is_reported(self):
        self.s.update(sandbox_group='sandbox', dns_zone='test.example.com')
        self.responses = [(1, 'NotFound'), (0, {'properties': {'ARecords': [{'ipv4Address': '192.0.2.1'}]}})]
        with self.assertRaisesRegex(provider.AzureError, 'ownership metadata'):
            self.az.ensure_bastion_dns(self.s, '192.0.2.1')

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

    def test_aro_size_change_is_allowed_before_resource_group_exists(self):
        self.responses = [(1, 'ResourceGroupNotFound')]
        self.az.check_aro_size_change(self.s)
        self.assertFalse(self.az.changed)

    def test_aro_size_change_is_allowed_when_only_owned_group_exists(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}), (1, 'ResourceNotFound')]
        self.az.check_aro_size_change(self.s)
        self.assertFalse(self.az.changed)
        self.assertFalse(any('create' in x for x in self.calls))

    def test_aro_size_change_is_refused_after_cluster_creation(self):
        self.responses = [(0, {'tags': {'agnosticd_project': 'project'}}), (0, {'provisioningState': 'Succeeded'})]
        with self.assertRaisesRegex(provider.AzureError, 'only before cluster creation'):
            self.az.check_aro_size_change(self.s)
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

    def test_sku_preflight_reports_public_restriction_details(self):
        self.s.update(version='4.20.1', master_size='Standard_D8s_v3', worker_size='Standard_D4s_v3', worker_count=3)
        restriction = dict(type='Zone', reasonCode='NotAvailableForSubscription', restrictionInfo={'zones': ['2', '3']})
        sku = dict(name='Standard_D8s_v3', restrictions=[restriction])
        self.responses = [(0, {'registrationState': 'Registered'})] * 6 + [(0, ['4.20.1']), (0, [sku])]
        with self.assertRaises(provider.AzureError) as error:
            self.az.preflight(self.s)
        self.assertEqual(error.exception.diagnostic,
                         {'kind': 'sku', 'setting': 'master_size',
                          'reasons': ['Zone: NotAvailableForSubscription (zones 2, 3)']})
        self.assertFalse(any('create' in x for x in self.calls))

    def test_quota_preflight_accepts_string_counts_and_family_case_differences(self):
        self.s.update(version='4.20.1', master_size='master', worker_size='worker', worker_count=3)
        skus = [dict(name=name, family='StandardDsv6Family', restrictions=[], capabilities=[dict(name='vCPUs', value=cores)])
                for name, cores in [('master', '8'), ('worker', '4')]]
        self.responses = [(0, {'registrationState': 'Registered'})] * 6 + [
            (0, ['4.20.1']), (0, skus),
            (0, [dict(name=dict(value='standardDSv6Family'), limit='44', currentValue='0'),
                 dict(name=dict(value='cores'), limit='268', currentValue='2')])]
        self.az.preflight(self.s)

    def test_quota_preflight_reports_available_and_required_cores(self):
        self.s.update(version='4.20.1', master_size='master', worker_size='worker', worker_count=3)
        skus = [dict(name=name, family='family', restrictions=[], capabilities=[dict(name='vCPUs', value=cores)])
                for name, cores in [('master', '8'), ('worker', '4')]]
        self.responses = [(0, {'registrationState': 'Registered'})] * 6 + [
            (0, ['4.20.1']), (0, skus), (0, [dict(name=dict(value='family'), limit='10', currentValue='0')])]
        with self.assertRaises(provider.AzureError) as error:
            self.az.preflight(self.s)
        self.assertEqual(error.exception.diagnostic,
                         {'kind': 'quota', 'family': 'family', 'required': 44, 'available': 10})


if __name__ == '__main__':
    unittest.main()
