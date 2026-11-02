"""Run early HCP region discovery with real Ansible and a read-only synthetic CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class HCPEarlyRegionTests(unittest.TestCase):
    def test_region_discovery_precedes_ownership_and_uses_authoritative_credentials(self):
        credentials = dict(subscription_id='synthetic-subscription', app_id='synthetic-app',
                           password='synthetic-password', tenant='synthetic-tenant', resource_group='sandbox')
        cases = [
            ('flat-unsupported', {'azure_region': 'eastus'}, True),
            ('flat-new-region', {'azure_region': 'newhcpregion'}, False),
            ('nested-unsupported', {'azure_region': 'newhcpregion', 'azure_sandbox': {
                'credentials': [dict(credentials, azure_region='eastus')]}}, True),
            ('nested-new-region', {'azure_region': 'eastus', 'azure_sandbox': {
                'credentials': [dict(credentials, azure_region='newhcpregion')]}}, False),
            ('classic-unaffected', {'azure_region': 'eastus', 'aro_deploy_hcp': False}, False),
        ]
        with tempfile.TemporaryDirectory(prefix='hcp-region-') as tmp:
            root = Path(tmp)
            collection = root / 'ansible_collections/agnosticd'
            collection.mkdir(parents=True)
            (collection / 'cloud_provider_azure').symlink_to(ROOT, target_is_directory=True)
            output = root / 'output'
            output.mkdir()
            archive = output / 'openshift-cluster_test_azure_ownership.json'
            original = 'Untouched archive: unsupported regions must fail before reading this file.'
            archive.write_text(original)
            trace = root / 'cli-calls.jsonl'
            config = root / 'ansible.cfg'
            config.write_text('[defaults]\nretry_files_enabled = false\n')
            env = dict(os.environ, ANSIBLE_CONFIG=str(config), ANSIBLE_COLLECTIONS_PATH=str(root),
                       ANSIBLE_LOCAL_TEMP=str(root / 'local'), ANSIBLE_REMOTE_TEMP=str(root / 'remote'),
                       PATH=str(ROOT / 'tests/integration') + os.pathsep + os.environ.get('PATH', ''),
                       AZURE_TEST_CALLS=str(trace), AZURE_TEST_STATE=str(root / 'state.json'))
            playbook = root / 'initialize.yml'
            playbook.write_text(yaml.safe_dump([dict(hosts='localhost', connection='local', gather_facts=False,
                tasks=[{'name': 'Initialize using synthetic Azure', 'ansible.builtin.include_role': {
                    'name': 'agnosticd.cloud_provider_azure.initialize'}}])], sort_keys=False))
            for name, overrides, rejected_region in cases:
                with self.subTest(name=name):
                    trace.write_text('')
                    variables = dict(config='openshift-cluster', guid='test', project_tag='openshift-cluster-test',
                                     output_dir=str(output), host_ocp4_deploy_installation_method='aro', aro_deploy_hcp=True,
                                     azure_subscription=credentials['subscription_id'],
                                     azure_service_principal_id=credentials['app_id'],
                                     azure_service_principal_password=credentials['password'], azure_tenant=credentials['tenant'],
                                     azure_resource_group=credentials['resource_group'], ansible_python_interpreter=sys.executable)
                    variables.update(overrides)
                    variables_file = root / 'variables.json'
                    variables_file.write_text(json.dumps(variables))
                    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(playbook),
                                             '-e', '@' + str(variables_file)], env=env, cwd=root, text=True,
                                            capture_output=True, timeout=60, check=False)
                    message = result.stdout + result.stderr
                    # Supported regions intentionally proceed to the malformed archive.
                    self.assertNotEqual(result.returncode, 0)
                    if rejected_region:
                        self.assertIn('ARO HCP is unavailable in region eastus', message)
                        self.assertIn('eastus2, newhcpregion, westeurope', message)
                        self.assertNotIn('Locate archived ownership record', message)
                    else:
                        self.assertNotIn('ARO HCP is unavailable', message)
                        self.assertIn('Load archived ownership record', message)
                    calls = [json.loads(line) for line in trace.read_text().splitlines()]
                    self.assertEqual(calls, [['login', '--service-principal'], ['provider', 'show']]
                                     if variables['aro_deploy_hcp'] else [])
                    self.assertEqual(archive.read_text(), original)
                    self.assertNotIn(credentials['password'], message)


if __name__ == '__main__':
    unittest.main()
