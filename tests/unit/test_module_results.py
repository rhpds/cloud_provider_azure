"""Check real Ansible redaction without damaging usable ARO connection data."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

from ansible.module_utils.common.arg_spec import ModuleArgumentSpecValidator
from ansible.module_utils.common.parameters import remove_values

ROOT = Path(__file__).resolve().parents[2]
provider_spec = importlib.util.spec_from_file_location('aro_provider', ROOT / 'plugins/module_utils/provider.py')
provider = importlib.util.module_from_spec(provider_spec)
provider_spec.loader.exec_module(provider)
module_spec = importlib.util.spec_from_file_location('azure_environment', ROOT / 'plugins/modules/azure_environment.py')
module = importlib.util.module_from_spec(module_spec)
with patch.dict(sys.modules, {
        'ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.provider': provider}):
    hcp_spec = importlib.util.spec_from_file_location('hcp_provider', ROOT / 'plugins/module_utils/hcp.py')
    hcp = importlib.util.module_from_spec(hcp_spec)
    hcp_spec.loader.exec_module(hcp)
    with patch.dict(sys.modules, {'ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.hcp': hcp}):
        module_spec.loader.exec_module(module)


class ModuleResultTests(unittest.TestCase):
    def test_sandbox_metadata_location_can_differ_while_group_and_dns_access_are_required(self):
        settings = dict(region='eastus2', sandbox_group='sandbox', dns_zone='test.example')
        zone = {'name': 'test.example'}
        az = Mock()
        az.call.side_effect = [{'location': 'eastus'}, zone]
        self.assertEqual(module.execute(az, 'validate', settings, None), {'zone': zone})
        self.assertEqual(az.call.call_args_list, [call('group', 'show', '-n', 'sandbox'),
            call('network', 'dns', 'zone', 'show', '-g', 'sandbox', '-n', 'test.example')])
        for results in ([provider.AzureError('ResourceGroupNotFound')],
                        [provider.AzureError('AuthorizationFailed')],
                        [{'location': 'eastus'}, provider.AzureError('ZoneNotFound')]):
            with self.subTest(results=results), self.assertRaises(provider.AzureError):
                az.call.side_effect = results
                module.execute(az, 'validate', settings, None)

    def test_public_settings_do_not_corrupt_urls_or_returned_credentials(self):
        for pull_secret in ('{"auths":{"registry.example":{"auth":"pull-secret-token"}}}',
                            {'auths': {'registry.example': {'auth': 'pull-secret-token'}}}):
            with self.subTest(pull_secret_type=type(pull_secret).__name__):
                auth = dict(subscription_id='subscription', app_id='application', password='principal-secret',
                            tenant='tenant', resource_group='sandbox', azure_region='eastus')
                settings = dict(sandbox_group='sandbox', region='eastus', infra_group='infra', cluster_group='cluster',
                                cluster_name='aro-test', method='aro', worker_count=3, worker_disk=128,
                                pull_secret=pull_secret)
                validation = ModuleArgumentSpecValidator(module.argument_spec()).validate(dict(
                    auth=auth, settings=settings, action='aro_install', project='project', config_dir='/tmp/test-cli'))
                self.assertEqual(validation.errors.errors, [])
                connection = dict(cluster={'apiserverProfile': {'url': 'https://api.test.eastus.aroapp.io:6443/'}},
                                  credentials={'kubeadminUsername': 'kubeadmin',
                                               'kubeadminPassword': 'opaque-aro-eastus-128-3-password'})
                # This is the same result sanitizer used by AnsibleModule.exit_json.
                self.assertEqual(remove_values(connection, validation._no_log_values), connection)
                invocation = remove_values(validation.validated_parameters, validation._no_log_values)
                self.assertNotIn('principal-secret', str(invocation))
                self.assertNotIn('pull-secret-token', str(invocation))
                self.assertEqual(invocation['settings']['region'], 'eastus')
                self.assertEqual(invocation['settings']['worker_count'], 3)


if __name__ == '__main__':
    unittest.main()
