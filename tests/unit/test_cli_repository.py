"""Check the installer against Microsoft's published RPM repository locations."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


class CliRepositoryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.tasks = yaml.safe_load((root / 'roles/install_cli/tasks/main.yml').read_text())
        self.defaults = yaml.safe_load((root / 'roles/install_cli/defaults/main.yml').read_text())
        self.jinja = Environment(undefined=StrictUndefined)

    def module_args(self, module):
        return next(task[module] for task in self.tasks if module in task)

    def test_repository_and_signing_key_match_the_os_major(self):
        repository = self.module_args('ansible.builtin.yum_repository')
        rpm_key = self.module_args('ansible.builtin.rpm_key')
        for major, signing_key in (('8', 'microsoft.asc'), ('9', 'microsoft.asc'), ('10', 'microsoft-2025.asc')):
            with self.subTest(major=major):
                context = {
                    'ansible_facts': {},
                    'install_cli_host_setup': {'ansible_facts': {'ansible_distribution_major_version': major}},
                }
                render = lambda value: self.jinja.from_string(value).render(context)
                self.assertEqual(render(repository['baseurl']), f'https://packages.microsoft.com/rhel/{major}/prod/')
                expected_key = 'https://packages.microsoft.com/keys/' + signing_key
                self.assertEqual(render(repository['gpgkey']), expected_key)
                self.assertEqual(render(rpm_key['key']), expected_key)

    def test_existing_legacy_repository_is_replaced_and_package_cache_refreshed(self):
        repository = self.module_args('ansible.builtin.yum_repository')
        self.assertEqual(repository['name'], 'azure-cli')
        self.assertTrue(repository['enabled'])
        self.assertTrue(repository['gpgcheck'])
        self.assertTrue(repository['repo_gpgcheck'])
        package = self.module_args('ansible.builtin.dnf')
        self.assertTrue(package['update_cache'])
        self.assertEqual(self.jinja.from_string(package['name']).render(self.defaults), 'azure-cli-2.90.0')

    def test_delegated_setup_works_without_controller_distribution_facts(self):
        # Execute only setup and validation from the real role. Never execute package/repository changes.
        tasks = [next(task for task in self.tasks if module in task)
                 for module in ('ansible.builtin.setup', 'ansible.builtin.assert')]
        with tempfile.TemporaryDirectory(prefix='azure-cli-facts-') as tmp:
            root = Path(tmp)
            role = root / 'roles/install_cli/tasks'
            role.mkdir(parents=True)
            (role / 'main.yml').write_text(yaml.safe_dump(tasks, sort_keys=False))
            playbook = root / 'facts.yml'
            playbook.write_text('''---
- name: Verify delegated CLI fact gathering without controller facts
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_facts: {}
  tasks:
  - name: Add a local stand-in for the bastion
    ansible.builtin.add_host:
      name: bastion-stub
      ansible_connection: local
  - name: Run the role fact-gathering and validation tasks
    delegate_to: bastion-stub
    block:
    - name: Include read-only tasks from installer
      ansible.builtin.include_role:
        name: install_cli
    rescue:
    - name: Accept only the documented unsupported distribution error on non-RHEL test hosts
      ansible.builtin.assert:
        that:
        - ansible_failed_result.msg == 'Azure CLI installation requires a RHEL-compatible 8, 9 or 10 host'
  - name: Verify the setup result is available independently of controller facts
    ansible.builtin.assert:
      that:
      - install_cli_host_setup.ansible_facts.ansible_os_family is defined
      - install_cli_host_setup.ansible_facts.ansible_distribution_major_version is defined
''')
            config = root / 'ansible.cfg'
            config.write_text('[defaults]\nretry_files_enabled = false\n')
            env = dict(os.environ)
            env.update(ANSIBLE_CONFIG=str(config), ANSIBLE_ROLES_PATH=str(root / 'roles'),
                       ANSIBLE_LOCAL_TEMP=str(root / 'local'), ANSIBLE_REMOTE_TEMP=str(root / 'remote'))
            result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(playbook)],
                                    cwd=root, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
