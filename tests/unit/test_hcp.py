"""Offline HCP API/ownership/template checks. Never accesses an Azure subscription."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('hcp_test_provider', ROOT / 'plugins/module_utils/provider.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)
spec = importlib.util.spec_from_file_location('hcp_test', ROOT / 'plugins/module_utils/hcp.py')
hcp = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.provider': provider}):
    spec.loader.exec_module(hcp)


class HCPTests(unittest.TestCase):
    def setUp(self):
        self.az = Mock()
        self.az.auth = {'subscription_id': 'subscription'}
        self.az.project = 'project'
        self.az.changed = False
        self.s = dict(cluster_group='cluster', cluster_name='aro-test', region='eastus2', method='aro',
                      identity_mode='managed_identity', version='4.21.22', worker_count=2, worker_size='Standard_D4s_v5',
                      worker_disk=128, hcp_nodepool='workers', hcp_zone='1', vnet_cidr='10.0.0.0/22',
                      worker_cidr='10.0.2.0/23', hcp_integration_cidr='10.0.0.0/28',
                      pod_cidr='10.128.0.0/14', service_cidr='172.30.0.0/16')
        self.h = hcp.HCP(self.az, self.s)

    def ready(self):
        return {'tags': {'agnosticd_project': 'project'}, 'properties': {'provisioningState': 'Succeeded'}}

    def test_validate_rejects_identity_count_and_overlapping_networks(self):
        self.h.validate()
        for change in ({'identity_mode': 'service_principal'}, {'worker_count': 1},
                       {'hcp_integration_cidr': '10.0.2.0/28'}, {'version': '4.21'}, {'cluster_name': 'bad_name'}):
            with self.subTest(change=change), self.assertRaises(provider.AzureError):
                hcp.HCP(self.az, dict(self.s, **change)).validate()
        self.az.call.assert_not_called()

    def test_ready_cluster_and_pool_are_reused_without_deployment(self):
        self.h.show = Mock(return_value=self.ready())
        self.h.install('/unused/template.json')
        self.az.call.assert_not_called()
        self.assertFalse(self.az.changed)

    def test_failed_and_unowned_clusters_are_not_recreated(self):
        for resource in ({'tags': {'agnosticd_project': 'other'}},
                         {'tags': {'agnosticd_project': 'project'}, 'properties': {'provisioningState': 'Failed'}}):
            self.h.show = Mock(return_value=resource)
            with self.assertRaises(provider.AzureError):
                self.h.install('/unused/template.json')
        self.az.call.assert_not_called()

    def test_deletion_failure_preserves_group_and_encryption_prerequisites(self):
        self.h.show = Mock(return_value=self.ready())
        self.h.rest = Mock(side_effect=provider.AzureError('Conflict'))
        with self.assertRaises(provider.AzureError):
            self.h.destroy()
        self.h.rest.assert_called_once_with('DELETE', self.h.url + '?api-version=' + hcp.API, missing=True)
        self.az.delete_group.assert_not_called()

    def test_delete_waits_for_cluster_and_managed_group_before_prerequisites(self):
        self.h.show = Mock(side_effect=[self.ready(), self.ready(), None])
        self.h.rest = Mock(return_value={})
        self.az.call.side_effect = [{'name': 'cluster-managed'}, None]
        def delete_prerequisites(*args):
            self.assertEqual(self.h.show.call_count, 3)
            self.assertEqual(self.az.call.call_count, 2)
        self.az.delete_group.side_effect = delete_prerequisites
        with patch.object(hcp.time, 'sleep'):
            self.h.destroy()
        self.h.rest.assert_called_once_with('DELETE', self.h.url + '?api-version=' + hcp.API, missing=True)
        self.az.delete_group.assert_called_once_with('cluster', 'eastus2')

    def test_destroy_cascades_through_cluster_when_last_pool_has_failed(self):
        self.h.show = Mock(side_effect=[self.ready(), None])
        self.az.call.return_value = None
        def api(method, url, **kwargs):
            if method == 'GET' and '/nodePools?' in url:
                return {'value': [{'name': 'workers', 'properties': {'provisioningState': 'Failed'}}]}
            if method == 'DELETE' and '/nodePools/' in url:
                raise provider.AzureError('Forbidden: The last node pool can not be deleted from a cluster')
            self.assertEqual((method, url), ('DELETE', self.h.url + '?api-version=' + hcp.API))
            return {}
        self.h.rest = Mock(side_effect=api)
        self.h.destroy()
        self.h.rest.assert_called_once_with('DELETE', self.h.url + '?api-version=' + hcp.API, missing=True)
        self.az.delete_group.assert_called_once_with('cluster', 'eastus2')

    def test_repeated_destroy_is_noop(self):
        self.az.group.return_value = None
        self.h.destroy()
        self.az.delete_group.assert_not_called()
        self.assertFalse(self.az.changed)

    def test_soft_deleted_vault_requires_trusted_archive(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.az.call.side_effect = lambda *args, **kwargs: (
            {'registrationState': 'Registered'} if args[0] == 'provider' else
            [{'name': self.h.vault, 'properties': {'vaultId': self.h.root + '/providers/Microsoft.KeyVault/vaults/' + self.h.vault}}]
            if args[:2] == ('keyvault', 'list-deleted') else None)
        with self.assertRaisesRegex(provider.AzureError, 'without matching archived ownership'):
            self.h.install('/unused/template.json')
        self.assertFalse(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

    def test_new_deployment_passes_explicit_parameters_and_waits_for_pool(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.h.physical_zone = 'eastus2-az2'
        self.h.wait = Mock(return_value=self.ready())
        captured = {}
        def cli(*args, **kwargs):
            if args[0] == 'provider':
                return {'registrationState': 'Registered'}
            if args[:2] == ('keyvault', 'list-deleted'):
                return []
            if args[:2] == ('keyvault', 'show'):
                return None
            if args[:2] == ('group', 'show'):
                return None
            if args[:3] == ('deployment', 'group', 'create'):
                captured.update(json.loads(Path(args[args.index('--parameters') + 1][1:]).read_text()))
                return {}
            raise AssertionError(args)
        self.az.call.side_effect = cli
        self.h.install('hcp.json')
        self.assertEqual(captured['workerCount']['value'], 2)
        self.assertEqual(captured['clusterVersion']['value'], '4.21')
        self.assertEqual(captured['nodePoolVersion']['value'], '4.21.22')
        self.assertEqual(captured['workerZone']['value'], 'eastus2-az2')
        self.assertEqual(captured['customerKeyVaultName']['value'], self.h.vault)
        self.assertEqual(self.h.wait.call_args_list[-1].args, ('/nodePools/workers',))
        self.assertTrue(self.az.changed)

    def test_matching_archive_recovers_only_its_owned_soft_deleted_vault(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.h.physical_zone = 'eastus2-az1'
        self.h.wait = Mock(return_value=self.ready())
        self.s['hcp_recover_vault'] = True
        def cli(*args, **kwargs):
            if args[0] == 'provider':
                return {'registrationState': 'Registered'}
            if args[:2] == ('keyvault', 'list-deleted'):
                return [{'name': self.h.vault, 'properties': {
                    'vaultId': self.h.root + '/providers/Microsoft.KeyVault/vaults/' + self.h.vault}}]
            return None
        self.az.call.side_effect = cli
        self.h.install('hcp.json')
        self.assertTrue(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

    def test_operation_url_cannot_forward_credentials_to_another_host(self):
        self.az.call.return_value = {'accessToken': 'secret-token'}
        response = Mock()
        response.status = 202
        response.headers = {'Location': 'https://untrusted.example/operation', 'Retry-After': '0'}
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(hcp.urllib.request, 'build_opener', return_value=opener), patch.object(hcp.time, 'sleep'):
            with self.assertRaisesRegex(provider.AzureError, 'outside management.azure.com'):
                self.h.rest('POST', self.h.url)
        self.assertEqual(opener.open.call_count, 1)

    def test_orphaned_managed_group_blocks_creation(self):
        self.h.show = Mock(return_value=None)
        self.az.call.return_value = {'id': 'existing-managed-group'}
        with self.assertRaisesRegex(provider.AzureError, 'managed resource group already exists'):
            self.h.install('hcp.json')
        self.az.group.assert_called_once_with('cluster', 'eastus2')

    def test_credentials_require_cluster_ownership(self):
        self.h.show = Mock(return_value={'tags': {'agnosticd_project': 'someone-else'}})
        self.h.rest = Mock()
        with self.assertRaisesRegex(provider.AzureError, 'unowned HCP cluster'):
            self.h.credentials('csr')
        self.h.rest.assert_not_called()

    def test_destroy_reconnects_to_in_progress_deletion(self):
        self.h.show = Mock(side_effect=[{'tags': {'agnosticd_project': 'project'},
                                        'properties': {'provisioningState': 'Deleting'}}, None])
        self.h.rest = Mock()
        self.az.call.return_value = None
        self.h.destroy()
        self.h.rest.assert_not_called()
        self.az.delete_group.assert_called_once_with('cluster', 'eastus2')

    def test_generated_template_has_hcp_pool_and_no_classic_control_plane(self):
        template = json.loads((ROOT / 'roles/host_ocp4_aro_install/templates/hcp.json').read_text())
        resources = template['resources']
        types = [r['type'] for r in resources]
        self.assertIn('Microsoft.RedHatOpenShift/hcpOpenShiftClusters', types)
        self.assertIn('Microsoft.RedHatOpenShift/hcpOpenShiftClusters/nodePools', types)
        self.assertNotIn('Microsoft.Compute/virtualMachines', types)
        vault = next(r for r in resources if r['type'] == 'Microsoft.KeyVault/vaults')
        # Key Vault rejects explicit false, including on first creation. Omitting
        # the setting also avoids disabling protection on an existing vault.
        self.assertNotIn('enablePurgeProtection', vault['properties'])
        self.assertEqual(vault['properties']['softDeleteRetentionInDays'], 7)
        pool = next(r for r in resources if r['type'].endswith('/nodePools'))
        self.assertEqual(pool['properties']['platform']['availabilityZone'], "[parameters('workerZone')]")

    def test_preflight_uses_worker_quota_floor_and_exact_versions(self):
        self.h.rest = Mock(return_value={'value': [{'name': '4.21.22'}]})
        def cli(*args):
            if args[:2] == ('provider', 'show'):
                return {'resourceTypes': [{'resourceType': 'HCPOpenShiftClusters', 'locations': ['East US 2']}]}
            if args[:2] == ('vm', 'list-skus'):
                return [{'name': self.s['worker_size'], 'family': 'StandardDSv5Family', 'restrictions': [],
                         'locationInfo': [{'zones': ['1']}], 'capabilities': [{'name': 'vCPUs', 'value': '4'}]}]
            if args[:2] == ('account', 'list-locations'):
                return [{'name': 'eastus2', 'availabilityZoneMappings': [{'logicalZone': '1', 'physicalZone': 'eastus2-az2'}]}]
            return [{'name': {'value': n}, 'limit': 20, 'currentValue': 0} for n in ('cores', 'standardDSv5Family')]
        self.az.call.side_effect = cli
        self.h.preflight()
        self.assertEqual(self.h.physical_zone, 'eastus2-az2')
        self.h.rest.return_value = {'value': [{'name': '4.21.220'}]}
        with self.assertRaisesRegex(provider.AzureError, 'version is unavailable'):
            self.h.preflight()

    def test_region_discovery_accepts_new_hcp_regions_and_ignores_classic_locations(self):
        self.az.call.return_value = {'resourceTypes': [
            {'resourceType': 'OpenShiftClusters', 'locations': ['East US']},
            {'resourceType': 'HCPOpenShiftClusters', 'locations': ['New HCP Region', 'East US 2']}]}
        self.assertEqual(hcp.check_region(self.az, 'newhcpregion'), ['eastus2', 'newhcpregion'])
        with self.assertRaisesRegex(provider.AzureError, 'Supported HCP regions reported by Azure: eastus2, newhcpregion'):
            hcp.check_region(self.az, 'eastus')
        self.assertTrue(all(c.args == ('provider', 'show', '--namespace', 'Microsoft.RedHatOpenShift')
                            for c in self.az.call.call_args_list))

    def test_missing_hcp_metadata_and_access_failures_do_not_use_a_fallback(self):
        for metadata in (None, {}, {'resourceTypes': None},
                         {'resourceTypes': [{'resourceType': 'OpenShiftClusters', 'locations': ['East US 2']}]},
                         {'resourceTypes': [{'resourceType': 'hcpOpenShiftClusters', 'locations': []}]}):
            self.az.call.return_value = metadata
            with self.subTest(metadata=metadata), self.assertRaisesRegex(provider.AzureError, 'did not advertise HCP regions'):
                hcp.check_region(self.az, 'eastus2')
        self.az.call.side_effect = provider.AzureError('AuthorizationFailed')
        with self.assertRaisesRegex(provider.AzureError, 'AuthorizationFailed'):
            hcp.check_region(self.az, 'eastus2')

    def test_asynchronous_credential_request_polls_location(self):
        class Response:
            def __init__(self, status, body, headers):
                self.status, self.body, self.headers = status, body, headers
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps(self.body).encode()
        self.az.call.return_value = {'accessToken': 'secret-token'}
        responses = [Response(202, {}, {'Location': self.h.root + '/operation', 'Retry-After': '0'}),
                     Response(200, {'kubeconfig': 'private-config', 'expirationTimestamp': 'tomorrow'}, {})]
        opener = Mock()
        opener.open.side_effect = responses
        with patch.object(hcp.urllib.request, 'build_opener', return_value=opener), patch.object(hcp.time, 'sleep'):
            result = self.h.rest('POST', self.h.url, {'certificateSigningRequest': 'csr'})
        self.assertEqual(result['kubeconfig'], 'private-config')


if __name__ == '__main__':
    unittest.main()
