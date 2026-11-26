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
                      identity_mode='managed_identity', version='5.0.1', worker_count=2, worker_size='Standard_D8s_v6',
                      worker_disk=128, hcp_nodepool='workers', hcp_zone='1', vnet_cidr='10.0.0.0/22',
                      worker_cidr='10.0.2.0/23', hcp_integration_cidr='10.0.0.0/28',
                      pod_cidr='10.128.0.0/14', service_cidr='172.30.0.0/16')
        self.h = hcp.HCP(self.az, self.s)

    def ready(self):
        return {'tags': {'agnosticd_project': 'project'}, 'properties': {
            'provisioningState': 'Succeeded', 'version': {'id': '5.0'}, 'platform': {'subnetId': '/test/worker-subnet'}}}

    def test_external_auth_registers_the_keycloak_console_client_idempotently(self):
        console = 'https://console.example.test'
        issuer = 'https://keycloak.example.test/realms/sso'
        self.s.update(hcp_auth_name='keycloak', hcp_auth_issuer=issuer,
                      hcp_auth_client_id='idp-4-ocp', hcp_auth_console_url=console)
        cluster = self.ready()
        cluster['properties']['console'] = {'url': console + '/'}
        self.h.show = Mock(side_effect=[cluster, None])
        self.h.rest = Mock()

        self.h.configure_external_auth()

        method, url, body = self.h.rest.call_args.args
        self.assertEqual((method, url), ('PUT', self.h.url + '/externalAuths/keycloak?api-version=' + hcp.API))
        self.assertEqual(body['properties'], {
            'issuer': {'url': issuer, 'audiences': ['idp-4-ocp']},
            'claim': {'mappings': {'username': {'claim': 'preferred_username', 'prefixPolicy': 'NoPrefix'}}},
            'clients': [{'clientId': 'idp-4-ocp', 'type': 'Confidential', 'extraScopes': ['profile'],
                         'component': {'name': 'console', 'authClientNamespace': 'openshift-console'}}],
        })
        self.assertTrue(self.az.changed)
        self.az.changed = False
        self.h.show.side_effect = [cluster, body]
        self.h.rest.reset_mock()

        self.h.configure_external_auth()

        self.assertFalse(self.az.changed)
        self.h.rest.assert_not_called()

    def test_external_auth_checks_cluster_ownership_and_console_before_mutating(self):
        self.s.update(hcp_auth_name='keycloak', hcp_auth_issuer='https://keycloak.example.test/realms/sso',
                      hcp_auth_client_id='idp-4-ocp', hcp_auth_console_url='https://console.example.test')
        cluster = self.ready()
        cluster['properties']['console'] = {'url': 'https://other.example.test'}
        self.h.show = Mock(side_effect=[None, cluster])
        self.h.rest = Mock()

        with self.assertRaisesRegex(provider.AzureError, 'missing or unowned'):
            self.h.configure_external_auth()
        with self.assertRaisesRegex(provider.AzureError, 'does not match'):
            self.h.configure_external_auth()

        self.h.rest.assert_not_called()

    def test_validate_rejects_identity_count_and_overlapping_networks(self):
        self.h.validate()
        for change in ({'identity_mode': 'service_principal'}, {'worker_count': 1},
                       {'hcp_integration_cidr': '10.0.2.0/28'}, {'version': '5.0'}, {'cluster_name': 'bad_name'},
                       {'hcp_nodepool': 'sixteenlettersss'}, {'hcp_zone': 'eastus2-az2'}):
            with self.subTest(change=change), self.assertRaises(provider.AzureError):
                hcp.HCP(self.az, dict(self.s, **change)).validate()
        self.az.call.assert_not_called()

    def test_install_rejects_preview_version_but_destroy_still_works(self):
        self.s['version'] = '4.21.22'
        with self.assertRaisesRegex(provider.AzureError, 'exact OpenShift 5 release'):
            self.h.install('/unused/template.json')
        self.az.group.assert_not_called()
        self.h.show = Mock(side_effect=[self.ready(), None])
        self.h.rest = Mock()
        self.az.call.return_value = None
        self.h.destroy()
        self.az.delete_group.assert_called_once_with('cluster', 'eastus2')

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
            [{'name': self.h.vault, 'properties': {'vaultId': '/subscriptions/subscription/resourceGroups/cluster/providers/Microsoft.KeyVault/vaults/' + self.h.vault}}]
            if args[:2] == ('keyvault', 'list-deleted') else None)
        with self.assertRaisesRegex(provider.AzureError, 'without matching archived ownership'):
            self.h.install('/unused/template.json')
        self.assertFalse(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

    def test_new_deployment_passes_explicit_parameters_and_waits_for_pool(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        operations = Mock()
        self.h.wait = operations.wait
        self.h.wait.return_value = self.ready()
        self.h.rest = operations.rest
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
                operations.deploy()
                captured.update(json.loads(Path(args[args.index('--parameters') + 1][1:]).read_text()))
                return {}
            raise AssertionError(args)
        self.az.call.side_effect = cli
        self.h.install('hcp.json')
        self.assertEqual(captured['clusterVersion']['value'], '5.0')
        self.assertNotIn('nodePoolVersion', captured)
        self.assertEqual(captured['customerKeyVaultName']['value'], self.h.vault)
        self.assertEqual([item[0] for item in operations.mock_calls], ['deploy', 'wait', 'rest', 'wait'])
        method, url, body = self.h.rest.call_args.args
        self.assertEqual((method, url), ('PUT', self.h.url + '/nodePools/workers?api-version=' + hcp.API))
        self.assertEqual(body['properties']['version'], {'id': '5.0.1', 'channelGroup': 'stable'})
        self.assertEqual(body['properties']['replicas'], 2)
        self.assertEqual(body['properties']['platform']['subnetId'], '/test/worker-subnet')
        self.assertEqual(body['properties']['platform']['availabilityZone'], '1')
        self.assertTrue(self.az.changed)

    def test_missing_pool_resumes_without_redeploying_cluster_or_pinning_zone(self):
        self.h.show = Mock(side_effect=[self.ready(), None])
        self.h.wait = Mock(return_value=self.ready())
        self.h.rest = Mock()
        self.h.preflight = Mock()
        self.s['hcp_zone'] = ''
        self.h.install('/unused/template.json')
        self.az.call.assert_not_called()
        self.h.preflight.assert_called_once_with()
        self.assertNotIn('availabilityZone', self.h.rest.call_args.args[2]['properties']['platform'])
        self.h.wait.assert_called_once_with('/nodePools/workers')

    def test_failed_or_unowned_pool_is_never_recreated(self):
        failed = self.ready()
        failed['properties']['provisioningState'] = 'Failed'
        for pool in (failed, {'tags': {}}):
            self.h.show = Mock(side_effect=[self.ready(), pool, pool])
            self.h.rest = Mock()
            with self.assertRaises(provider.AzureError):
                self.h.install('/unused/template.json')
            self.h.rest.assert_not_called()

    def test_matching_archive_recovers_only_its_owned_soft_deleted_vault(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.h.wait = Mock(return_value=self.ready())
        self.h.rest = Mock()
        self.s['hcp_recover_vault'] = True
        def cli(*args, **kwargs):
            if args[0] == 'provider':
                return {'registrationState': 'Registered'}
            if args[:2] == ('keyvault', 'list-deleted'):
                return [{'name': self.h.vault, 'properties': {
                    'vaultId': '/subscriptions/subscription/resourceGroups/cluster/providers/Microsoft.KeyVault/vaults/' + self.h.vault}}]
            return None
        self.az.call.side_effect = cli
        self.h.install('hcp.json')
        self.assertTrue(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

    def test_matching_archive_refuses_deleted_vault_from_another_resource_scope(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.s['hcp_recover_vault'] = True
        expected = '/subscriptions/subscription/resourceGroups/cluster/providers/Microsoft.KeyVault/vaults/' + self.h.vault
        for resource_id in (expected.replace('/subscription/', '/other-subscription/'),
                            expected.replace('/cluster/', '/other-group/'),
                            'https://management.azure.com' + expected):
            with self.subTest(resource_id=resource_id):
                self.az.call.reset_mock()
                self.az.call.side_effect = lambda *args, **kwargs: (
                    {'registrationState': 'Registered'} if args[0] == 'provider' else
                    [{'name': self.h.vault, 'properties': {'vaultId': resource_id}}]
                    if args[:2] == ('keyvault', 'list-deleted') else None)
                with self.assertRaisesRegex(provider.AzureError, 'without matching archived ownership'):
                    self.h.install('hcp.json')
                self.assertFalse(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

    def test_existing_vault_uses_resource_id_and_project_ownership(self):
        self.h.show = Mock(return_value=None)
        self.h.preflight = Mock()
        self.h.wait = Mock(return_value=self.ready())
        self.h.rest = Mock()
        expected = '/subscriptions/subscription/resourceGroups/cluster/providers/Microsoft.KeyVault/vaults/' + self.h.vault
        for resource_id, project, success in ((expected.upper(), 'project', True),
                                              (expected, 'other-project', False),
                                              (expected.replace('/cluster/', '/other-group/'), 'project', False)):
            with self.subTest(resource_id=resource_id, project=project):
                self.az.call.reset_mock()

                def cli(*args, **kwargs):
                    if args[0] == 'provider':
                        return {'registrationState': 'Registered'}
                    if args[:2] == ('keyvault', 'show'):
                        return {'id': resource_id, 'tags': {'agnosticd_project': project}}
                    if args[:2] == ('keyvault', 'list-deleted'):
                        return []
                    return None

                self.az.call.side_effect = cli
                if success:
                    self.h.install('hcp.json')
                    self.assertTrue(any(c.args[:3] == ('deployment', 'group', 'create')
                                        for c in self.az.call.call_args_list))
                else:
                    with self.assertRaisesRegex(provider.AzureError, 'unowned HCP encryption vault'):
                        self.h.install('hcp.json')
                    self.assertFalse(any(c.args[:3] == ('deployment', 'group', 'create')
                                         for c in self.az.call.call_args_list))
                self.assertFalse(any(c.args[:2] == ('keyvault', 'recover') for c in self.az.call.call_args_list))

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

    def test_generated_template_creates_only_the_hcp_control_plane_and_prerequisites(self):
        template = json.loads((ROOT / 'roles/host_ocp4_aro_install/templates/hcp.json').read_text())
        resources = template['resources']
        types = [r['type'] for r in resources]
        self.assertIn('Microsoft.RedHatOpenShift/hcpOpenShiftClusters', types)
        self.assertNotIn('Microsoft.RedHatOpenShift/hcpOpenShiftClusters/nodePools', types)
        cluster = next(r for r in resources if r['type'] == 'Microsoft.RedHatOpenShift/hcpOpenShiftClusters')
        self.assertEqual(cluster['apiVersion'], hcp.API)
        self.assertNotIn('Microsoft.Compute/virtualMachines', types)
        vault = next(r for r in resources if r['type'] == 'Microsoft.KeyVault/vaults')
        # Key Vault rejects explicit false, including on first creation. Omitting
        # the setting also avoids disabling protection on an existing vault.
        self.assertNotIn('enablePurgeProtection', vault['properties'])
        self.assertEqual(vault['properties']['softDeleteRetentionInDays'], 7)

    def test_preflight_uses_worker_quota_floor_and_exact_versions(self):
        enabled = {'name': '5.0.1', 'properties': {'channelGroup': 'stable', 'enabled': True}}
        self.h.rest = Mock(return_value={'value': [enabled]})
        def cli(*args):
            if args[:2] == ('provider', 'show'):
                return {'resourceTypes': [{'resourceType': 'HCPOpenShiftClusters', 'locations': ['East US 2']}]}
            if args[:2] == ('vm', 'list-skus'):
                return [{'name': self.s['worker_size'], 'family': 'StandardDSv5Family', 'restrictions': [],
                         'locationInfo': [{'zones': ['1']}], 'capabilities': [{'name': 'vCPUs', 'value': '4'}]}]
            if args[:2] == ('vm', 'list-usage'):
                return [{'name': {'value': n}, 'limit': 20, 'currentValue': 0} for n in ('cores', 'standardDSv5Family')]
            raise AssertionError(args)
        self.az.call.side_effect = cli
        self.h.preflight()
        self.s['hcp_zone'] = '2'
        with self.assertRaisesRegex(provider.AzureError, 'worker SKU or availability zone is unavailable'):
            self.h.preflight()
        self.s['hcp_zone'] = ''
        self.h.preflight()
        for item in (dict(enabled, name='5.0.10'),
                     dict(enabled, properties={'channelGroup': 'stable', 'enabled': False}),
                     dict(enabled, properties={'channelGroup': 'candidate', 'enabled': True})):
            with self.subTest(item=item), self.assertRaisesRegex(provider.AzureError, 'version is unavailable'):
                self.h.rest.return_value = {'value': [item]}
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

    def test_credential_request_uses_location_when_status_response_has_no_payload(self):
        self.az.call.return_value = {'accessToken': 'secret-token'}
        self.h.show = Mock(return_value=self.ready())
        status_url = self.h.root + '/operations/credential'
        result_url = self.h.root + '/operationResults/credential'
        credential = {'kubeconfig': 'apiVersion: v1\\nkind: Config', 'expirationTimestamp': '2026-11-26T10:00:00Z'}
        responses = [
            (202, {}, {'Azure-AsyncOperation': status_url, 'Location': result_url, 'Retry-After': '0'}),
            (200, {}, {}),
            (200, credential, {}),
        ]
        opener = Mock()
        mocked_responses = []
        for status, result, headers in responses:
            response = Mock(status=status, headers=headers)
            response.read.return_value = json.dumps(result).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock()
            mocked_responses.append(response)
        opener.open.side_effect = mocked_responses

        with patch.object(hcp.urllib.request, 'build_opener', return_value=opener), patch.object(hcp.time, 'sleep'):
            result = self.h.credentials('csr')

        self.assertEqual(result, dict(credential, kubeconfig='apiVersion: v1\nkind: Config'))
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([request.full_url for request in requests[1:]], [status_url, result_url])
        self.assertEqual([request.get_method() for request in requests], ['POST', 'GET', 'GET'])

    def test_credentials_retry_incomplete_results_and_reuse_the_csr(self):
        self.h.show = Mock(return_value=self.ready())
        expected = {'kubeconfig': 'private-config', 'expirationTimestamp': '2026-11-26T10:00:00Z'}
        self.h.rest = Mock(side_effect=[{}, {'kubeconfig': 'partial'}, expected])
        with patch.object(hcp.time, 'sleep') as sleep:
            self.assertEqual(self.h.credentials('csr'), expected)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.h.rest.call_count, 3)
        self.assertTrue(all(call.args[2] == {'certificateSigningRequest': 'csr'}
                            for call in self.h.rest.call_args_list))

    def test_credentials_report_incomplete_output_without_exposing_the_payload(self):
        self.h.show = Mock(return_value=self.ready())
        self.h.rest = Mock(return_value={'kubeconfig': 'partial-sensitive-value'})
        with patch.object(hcp.time, 'sleep'), self.assertRaises(provider.AzureError) as error:
            self.h.credentials('csr')
        self.assertIn('after 3 attempts. Missing or invalid fields include expirationTimestamp.', str(error.exception))
        self.assertNotIn('partial-sensitive-value', str(error.exception))
        self.assertEqual(self.h.rest.call_count, 3)

    def test_credentials_do_not_resubmit_after_an_explicit_azure_error(self):
        self.h.show = Mock(return_value=self.ready())
        self.h.rest = Mock(side_effect=provider.AzureError('HCP ARM request failed with HTTP 400'))
        with patch.object(hcp.time, 'sleep') as sleep, self.assertRaisesRegex(provider.AzureError, 'HTTP 400'):
            self.h.credentials('csr')
        self.h.rest.assert_called_once()
        sleep.assert_not_called()

    def test_pool_creation_follows_201_operation_until_azure_reports_failure(self):
        self.az.call.return_value = {'accessToken': 'secret-token'}
        initial, failed = Mock(), Mock()
        initial.status = 201
        operation = self.h.root + '/operation'
        initial.headers = {'Azure-AsyncOperation': operation, 'Retry-After': '0'}
        initial.read.return_value = b'{"properties": {"provisioningState": "Provisioning"}}'
        failed.status = 200
        failed.headers = {}
        failed.read.return_value = b'{"status": "Failed", "error": {"code": "DeadlineExceeded"}}'
        for response in (initial, failed):
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
        opener = Mock()
        opener.open.side_effect = [initial, failed]
        with patch.object(hcp.urllib.request, 'build_opener', return_value=opener), patch.object(hcp.time, 'sleep'):
            with self.assertRaisesRegex(provider.AzureError, 'DeadlineExceeded'):
                self.h.rest('PUT', self.h.url + '/nodePools/workers', {})
        poll_request = opener.open.call_args.args[0]
        self.assertEqual((poll_request.get_method(), poll_request.full_url), ('GET', operation))


if __name__ == '__main__':
    unittest.main()
