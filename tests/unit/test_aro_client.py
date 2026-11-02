"""Exercise client installation, protected login and password publication offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[2]


class AroClientTests(unittest.TestCase):
    def test_default_download_matches_aro_release_and_target_os(self):
        defaults = yaml.safe_load((ROOT / 'roles/host_ocp4_aro_install/defaults/main.yml').read_text())
        template = Environment(undefined=StrictUndefined).from_string(defaults['aro_client_download_url'])
        for major, architecture, archive in (
                ('9', 'x86_64', 'openshift-client-linux-amd64-rhel9.tar.gz'),
                ('10', 'aarch64', 'openshift-client-linux-arm64-rhel9.tar.gz'),
                ('8', 'x86_64', 'openshift-client-linux.tar.gz')):
            with self.subTest(major=major, architecture=architecture):
                url = template.render(
                    hostvars={'localhost': {'azure_settings': {'version': '4.21.22'}}},
                    host_ocp4_installer_version='4.22',
                    install_cli_host_setup={'ansible_facts': {'ansible_architecture': architecture,
                                                            'ansible_distribution_major_version': major}})
                self.assertEqual(url, 'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/4.21.22/' + archive)

    def run_client_and_login(self, fail_login=False):
        with tempfile.TemporaryDirectory(prefix='aro-client-') as tmp:
            root = Path(tmp)
            collection = root / 'ansible_collections/agnosticd'
            collection.mkdir(parents=True)
            role = collection / 'cloud_provider_azure/roles/host_ocp4_aro_install'
            (role / 'tasks').mkdir(parents=True)
            (role / 'defaults').mkdir()
            (role / 'defaults/main.yml').write_text((ROOT / 'roles/host_ocp4_aro_install/defaults/main.yml').read_text())
            client_tasks = yaml.safe_load((ROOT / 'roles/host_ocp4_aro_install/tasks/install_client.yml').read_text())
            # Preserve the actual install tasks, with immediate retry failure in tests.
            client_tasks[-1].update(retries=1, delay=0)
            (role / 'tasks/install_client.yml').write_text(yaml.safe_dump(client_tasks, sort_keys=False))
            binary = '''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args == ['version', '--client']:
    print('Client Version: 4.21.22')
    raise SystemExit(0)
if args[:2] == ['login', 'https://api.test.eastus.aroapp.io:6443/']:
    if '--password=opaque-aro-eastus-128-3-password' in args and '--username' in args:
        print('Login successful')
        raise SystemExit(0)
print('Unable to connect to the server: no such host', file=sys.stderr)
raise SystemExit(1)
'''
            archive = root / 'client.zip'
            # ZIP exercises the real unarchive task on macOS as well as Linux;
            # the production tar.gz extraction requires GNU tar on its RHEL host.
            with zipfile.ZipFile(archive, 'w') as client_zip:
                for name in ('oc', 'kubectl', 'openshift-install'):
                    member = zipfile.ZipInfo(name)
                    member.external_attr = 0o100755 << 16
                    client_zip.writestr(member, binary)
            tasks = yaml.safe_load((ROOT / 'roles/host_ocp4_aro_install/tasks/classic_access.yml').read_text())
            login = next(task for task in tasks if task['name'] == 'Log into ARO')
            login['block'][0].update(retries=1, delay=0)
            save_password = next(task for task in tasks if task['name'] == 'Save ARO kubeadmin password in user data')
            # Capture the user-info action's rendered arguments without requiring
            # the external core collection or writing a real deployment archive.
            action_dir = collection / 'core/plugins/action'
            action_dir.mkdir(parents=True)
            (action_dir / 'agnosticd_user_info.py').write_text('''from ansible.plugins.action import ActionBase
from pathlib import Path
import json
class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        data = self._task.args['data']
        Path(task_vars['test_user_data_path']).write_text(json.dumps(data))
        return dict(changed=False, data=data)
''')
            fragment = root / 'login.yml'
            fragment.write_text(yaml.safe_dump([login, save_password], sort_keys=False))
            playbook = root / 'client.yml'
            playbook.write_text(yaml.safe_dump([dict(
                name='Exercise client-only installation and protected login without Azure',
                hosts='localhost', connection='local', gather_facts=False,
                vars=dict(ansible_become=False, ansible_user='synthetic-user', cluster_name='ocp',
                          aro_client_download_url=str(archive), aro_client_install_dir=str(root / 'bin'),
                          test_user_data_path=str(root / 'user-data.json'),
                          ansible_python_interpreter=sys.executable,
                          install_cli_host_setup={'ansible_facts': {'ansible_architecture': 'x86_64'}},
                          azure_aro_result={'cluster': {'apiserverProfile': {
                              'url': 'https://wrong.example.test:6443/' if fail_login
                              else 'https://api.test.eastus.aroapp.io:6443/'}},
                              'credentials': {'kubeadminUsername': 'kubeadmin',
                                              'kubeadminPassword': 'opaque-aro-eastus-128-3-password'}}),
                tasks=[
                    {'name': 'Run real client-only tasks', 'ansible.builtin.include_role': {
                        'name': 'agnosticd.cloud_provider_azure.host_ocp4_aro_install', 'tasks_from': 'install_client.yml'}},
                    {'name': 'Run the real protected login block', 'ansible.builtin.include_tasks': str(fragment)},
                    {'name': 'Check installer was not extracted', 'ansible.builtin.stat': {
                        'path': str(root / 'bin/openshift-install')}, 'register': 'installer'},
                    {'name': 'Check matching client and login', 'ansible.builtin.assert': {
                        'that': ['not installer.stat.exists', 'azure_aro_login.rc == 0']}}
                ])], sort_keys=False))
            config = root / 'ansible.cfg'
            config.write_text('[defaults]\nretry_files_enabled = false\n')
            env = dict(os.environ, ANSIBLE_CONFIG=str(config), ANSIBLE_COLLECTIONS_PATH=str(root),
                       ANSIBLE_LOCAL_TEMP=str(root / 'local'), ANSIBLE_REMOTE_TEMP=str(root / 'remote'))
            result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(playbook),
                                   '-e', 'host_ocp4_installer_version=4.22'],
                                  cwd=root, env=env, text=True, capture_output=True, check=False, timeout=90)
            if result.returncode == 0:
                self.assertEqual(json.loads((root / 'user-data.json').read_text()), {
                    'openshift_kubeadmin_password': 'opaque-aro-eastus-128-3-password'})
            if fail_login:
                self.assertFalse((root / 'user-data.json').exists())
            return result

    def test_only_clients_are_installed_and_login_uses_unmodified_connection_data(self):
        result = self.run_client_and_login()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('opaque-aro-eastus-128-3-password', result.stdout + result.stderr)

    def test_login_failure_reports_dns_cause_without_printing_credentials(self):
        result = self.run_client_and_login(fail_login=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('endpoint DNS resolution failed', result.stdout)
        self.assertNotIn('opaque-aro-eastus-128-3-password', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
