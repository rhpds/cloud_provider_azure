"""Exercise real kubeconfig assembly/export/refresh tasks with synthetic credentials."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class HCPCredentialTests(unittest.TestCase):
    def test_refresh_keeps_all_kubeconfig_copies_private_and_inserts_the_local_key(self):
        with tempfile.TemporaryDirectory(prefix='hcp-credentials-') as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            source = (ROOT / 'roles/host_ocp4_aro_hcp_credentials/tasks/main.yml').read_text()
            # Redirect bastion paths into an isolated workspace; run the actual copy/fetch templates.
            tasks = yaml.safe_load(source.replace('/home/{{ ansible_user }}', str(root / 'bastion')))[0]['block']
            names = {'Create administrative kubeconfig directory', 'Assemble kubeconfig with locally held private key',
                     'Create login user kubeconfig directory', 'Configure login user access',
                     'Export HCP access to controller', 'Record credential expiry on controller', 'Protect exported kubeconfig'}
            fragment = root / 'export.yml'
            fragment.write_text(yaml.safe_dump([task for task in tasks if task['name'] in names], sort_keys=False))
            config = {'apiVersion': 'v1', 'kind': 'Config',
                      'clusters': [{'name': 'hcp', 'cluster': {'server': 'https://api.example.test:6443',
                                                            'certificate-authority-data': 'synthetic-ca'}}],
                      'contexts': [{'name': 'admin', 'context': {'cluster': 'hcp', 'user': 'admin'}}],
                      'current-context': 'admin',
                      'users': [{'name': 'admin', 'user': {'client-certificate-data': 'synthetic-certificate'}}]}
            private_keys = [base64.b64encode(('private-key-' + str(i)).encode()).decode() for i in (1, 2)]
            playbook = root / 'refresh.yml'
            playbook.write_text(yaml.safe_dump([dict(
                name='Refresh HCP access without Azure', hosts='localhost', connection='local', gather_facts=False,
                vars=dict(ansible_python_interpreter=sys.executable, output_dir=str(output),
                          config='openshift-cluster', guid='test', cluster_name='ocp'),
                tasks=[{'name': 'Issue synthetic credential and refresh every exported copy',
                        'ansible.builtin.include_tasks': str(fragment),
                        'loop': [1, 2],
                        'vars': {'hcp_credential': {'kubeconfig': yaml.safe_dump(config),
                                                    'expirationTimestamp': 'tomorrow-{{ item }}'},
                                 'hcp_private_key': {'content': '{{ synthetic_keys[item - 1] }}'},
                                 'synthetic_keys': private_keys}}])], sort_keys=False))
            ansible_config = root / 'ansible.cfg'
            ansible_config.write_text('[defaults]\nretry_files_enabled = false\n')
            env = dict(os.environ, ANSIBLE_CONFIG=str(ansible_config), ANSIBLE_LOCAL_TEMP=str(root / 'local'),
                       ANSIBLE_REMOTE_TEMP=str(root / 'remote'))
            result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(playbook),
                                     '-e', 'output_dir=' + str(output)],
                                    env=env, cwd=root, text=True, capture_output=True, timeout=90, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for path in (root / 'bastion/ocp/auth/kubeconfig', root / 'bastion/.kube/config',
                         output / 'openshift-cluster_test_kubeconfig'):
                saved = yaml.safe_load(path.read_text())
                self.assertEqual(saved['users'][0]['user']['client-key-data'], private_keys[-1])
                self.assertEqual(saved['users'][0]['user']['client-certificate-data'], 'synthetic-certificate')
                self.assertEqual(saved['current-context'], 'admin')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            expiry = output / 'openshift-cluster_test_hcp_credentials.json'
            self.assertEqual(json.loads(expiry.read_text())['expirationTimestamp'], 'tomorrow-2')
            self.assertEqual(expiry.stat().st_mode & 0o777, 0o600)
            for secret in private_keys + ['synthetic-certificate']:
                self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
