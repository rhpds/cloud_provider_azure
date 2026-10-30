"""Check the installer against Microsoft's published RPM repository locations."""
from pathlib import Path
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
                context = {'ansible_facts': {'distribution_major_version': major}}
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


if __name__ == '__main__':
    unittest.main()
