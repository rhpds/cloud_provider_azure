"""Exercise lifecycle action precedence through Ansible and the offline Azure CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

COLLECTION = Path(__file__).resolve().parents[2]


class LifecycleTests(unittest.TestCase):
    def run_lifecycle(self, action, override=None, extra_action=False):
        with tempfile.TemporaryDirectory(prefix='azure-lifecycle-') as tmp:
            root = Path(tmp)
            collections = root / 'ansible_collections/agnosticd'
            collections.mkdir(parents=True)
            (collections / 'cloud_provider_azure').symlink_to(COLLECTION, target_is_directory=True)
            variables = dict(
                config='openshift-cluster', guid='test', project_tag='openshift-cluster-test',
                host_ocp4_deploy_installation_method='aro', output_dir=str(root),
                ansible_python_interpreter=sys.executable,
                azure_subscription='synthetic-subscription', azure_service_principal_id='synthetic-app',
                azure_service_principal_password='synthetic-password', azure_tenant='synthetic-tenant',
                azure_resource_group='sandbox', azure_region='eastus')
            state = dict(groups={
                name: {'tags': {'agnosticd_project': 'openshift-cluster-test'}}
                for name in ('openshift-cluster-test-infra', 'openshift-cluster-test-cluster')},
                cluster={'provisioningState': 'Succeeded'}, record=None)
            state_file = root / 'state.json'
            state_file.write_text(json.dumps(state))
            include = {'name': 'Run the real lifecycle role', 'ansible.builtin.include_role': {
                'name': 'agnosticd.cloud_provider_azure.lifecycle'}}
            if override:
                include['vars'] = {'azure_lifecycle_action': override}
            playbook = root / 'lifecycle.yml'
            playbook.write_text(yaml.safe_dump([dict(
                name='Exercise lifecycle action precedence without Azure access', hosts='localhost',
                connection='local', gather_facts=False, vars=variables, tasks=[
                    {'name': 'Set the global run action as destroy.yml does',
                     'ansible.builtin.set_fact': {'ACTION': action}}, include,
                    {'name': 'Verify the global action was preserved',
                     'ansible.builtin.assert': {'that': f"ACTION == '{action}'"}}])], sort_keys=False))
            config = root / 'ansible.cfg'
            config.write_text('[defaults]\nretry_files_enabled = false\n')
            env = dict(os.environ, ANSIBLE_CONFIG=str(config), ANSIBLE_COLLECTIONS_PATH=str(root),
                       ANSIBLE_LOCAL_TEMP=str(root / 'local'), ANSIBLE_REMOTE_TEMP=str(root / 'remote'),
                       AZURE_TEST_STATE=str(state_file),
                       PATH=str(COLLECTION / 'tests/integration') + os.pathsep + os.environ.get('PATH', ''))
            command = [sys.executable, '-m', 'ansible.cli.playbook', str(playbook)]
            if extra_action:
                command += ['-e', json.dumps({'ACTION': action})]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)
            return result, json.loads(state_file.read_text()), state

    def test_destroy_preparation_starts_only_bastion_and_preserves_cluster(self):
        for extra_action in (False, True):
            with self.subTest(extra_action=extra_action):
                result, state, original = self.run_lifecycle('destroy', 'start', extra_action)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(state['groups'], original['groups'])
                self.assertEqual(state['cluster'], original['cluster'])
                self.assertEqual(state['power_actions'], [{'action': 'start', 'id': '/bastion'}])

    def test_lifecycle_defaults_to_global_stop_and_status_actions(self):
        for action in ('stop', 'status'):
            with self.subTest(action=action):
                result, state, original = self.run_lifecycle(action)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(state['groups'], original['groups'])
                self.assertEqual(state['cluster'], original['cluster'])
                self.assertEqual(state.get('power_actions', []),
                                 [{'action': 'deallocate', 'id': '/bastion'}] if action == 'stop' else [])

    def test_lifecycle_refuses_destroy_without_an_explicit_start_override(self):
        result, state, original = self.run_lifecycle('destroy')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Azure lifecycle actions must be start, stop or status', result.stdout)
        self.assertEqual(state, original)


if __name__ == '__main__':
    unittest.main()
