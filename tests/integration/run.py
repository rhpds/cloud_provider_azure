#!/usr/bin/env python3
"""Run real Ansible modules against the synthetic Azure CLI; never contacts Azure."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml


def main():
    collection = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='agnosticd-azure-test-') as tmp:
        root = Path(tmp)
        collections = root / 'ansible_collections' / 'agnosticd'
        collections.mkdir(parents=True)
        (collections / 'cloud_provider_azure').symlink_to(collection, target_is_directory=True)
        output = root / 'output'
        output.mkdir()
        env = dict(os.environ)
        env.update(
            PATH=str(collection / 'tests/integration') + os.pathsep + env.get('PATH', ''),
            AZURE_TEST_STATE=str(root / 'state.json'),
            ANSIBLE_LOCAL_TEMP=str(root / 'local'),
            ANSIBLE_REMOTE_TEMP=str(root / 'remote'),
            ANSIBLE_COLLECTIONS_PATH=str(root) + os.pathsep + env.get('ANSIBLE_COLLECTIONS_PATH', ''),
        )
        command = [sys.executable, '-m', 'ansible.cli.playbook',
                   str(collection / 'tests/integration/provider.yml'),
                   '-e', 'output_dir=' + str(output),
                   '-e', 'ansible_python_interpreter=' + sys.executable]
        template = collection.parents[2] / 'agnosticd-v2/ansible/roles/host_ocp4_installer/templates/install-config.yaml.j2'
        if template.is_file():
            command += ['-e', 'v2_install_config_template=' + str(template)]
            env['ANSIBLE_ROLES_PATH'] = str(template.parents[2]) + os.pathsep + env.get('ANSIBLE_ROLES_PATH', '')
        result = subprocess.run(command, env=env, cwd=collection, check=False)
        if result.returncode:
            return result.returncode
        destroy_playbook = template.parents[3] / 'configs/openshift-cluster/azure/destroy_env.yml'
        if destroy_playbook.is_file():
            # Exercise the real v2 preparation play with ACTION supplied as an extra var.
            # This has higher precedence than the former per-role ACTION: start override.
            state_file = Path(env['AZURE_TEST_STATE'])
            previous_state = state_file.read_text()
            owned = {'tags': {'agnosticd_project': 'openshift-cluster-test'}}
            state_file.write_text(json.dumps(dict(
                groups={'openshift-cluster-test-infra': owned, 'openshift-cluster-test-cluster': owned},
                cluster={'provisioningState': 'Succeeded'}, record={
                    'metadata': {'agnosticd_project': 'openshift-cluster-test'},
                    'ARecords': [{'ipv4Address': '192.0.2.1'}]})))
            prepare = yaml.safe_load(destroy_playbook.read_text())[0]
            prepare['vars'] = dict(yaml.safe_load((collection / 'tests/integration/provider.yml').read_text())[0]['vars'],
                                   output_dir=str(output), cloud_provider='azure', ansible_user='azure',
                                   ssh_provision_key_name='synthetic-ssh-key',
                                   ssh_provision_key_path=str(output / 'synthetic-ssh-key'),
                                   ansible_python_interpreter=sys.executable)
            prepare['tasks'] += [
                {'name': 'Require ARO to survive destroy preparation', 'ansible.builtin.assert': {
                    'that': ["azure_lifecycle_result.cluster.provisioningState == 'Succeeded'", "ACTION == 'destroy'"]}},
                {'name': 'Delete ARO before bastion infrastructure', 'ansible.builtin.include_role': {
                    'name': 'agnosticd.cloud_provider_azure.host_ocp4_aro_destroy'}},
                {'name': 'Delete bastion infrastructure after ARO', 'ansible.builtin.include_role': {
                    'name': 'agnosticd.cloud_provider_azure.template_destroy'}},
                {'name': 'Repeat infrastructure destruction', 'ansible.builtin.include_role': {
                    'name': 'agnosticd.cloud_provider_azure.template_destroy'}},
                {'name': 'Require repeated destruction to be a no-op', 'ansible.builtin.assert': {
                    'that': 'not azure_result.changed'}}]
            prepare_file = root / 'destroy-preparation.yml'
            prepare_file.write_text(yaml.safe_dump([prepare], sort_keys=False))
            prepare_result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(prepare_file),
                                             '-e', 'ACTION=destroy'],
                                            cwd=collection, env=env, text=True, capture_output=True, check=False)
            state = json.loads(state_file.read_text())
            if (prepare_result.returncode or state['groups'] or state['cluster'] or state['record']
                    or state.get('power_actions') != [{'action': 'start', 'id': '/bastion'}]):
                print('FAIL: v2 destroy preparation and ordered cleanup')
                print(prepare_result.stdout + prepare_result.stderr)
                return 1
            state_file.write_text(previous_state)
            print('PASS: v2 destroy preparation starts only the bastion, then ordered cleanup is idempotent')
        flat = dict(azure_subscription='synthetic-subscription', azure_service_principal_id='synthetic-app',
                    azure_service_principal_password='synthetic-password', azure_tenant='synthetic-tenant',
                    azure_resource_group='sandbox', azure_region='eastus',
                    azure_dns_zone='test.azure.redhatworkshops.io')
        ownership_file = output / 'openshift-cluster_test_azure_ownership.json'
        legacy = json.loads(ownership_file.read_text())
        legacy['settings'].update(master_size='Standard_D8s_v6', worker_size='Standard_D4s_v6')
        state_file = Path(env['AZURE_TEST_STATE'])
        original_state = state_file.read_text()
        size_cases = [
            ('before-cluster', {}, False, True),
            ('existing-cluster', {}, True, False),
            ('status-preserves-sizes', dict(ACTION='status', expected_master_size='Standard_D8s_v6',
                                            expected_worker_size='Standard_D4s_v6'), True, True),
            ('ownership-boundary', dict(azure_infra_resource_group='different-infra'), False, False),
        ]
        for name, overrides, cluster_exists, expected_success in size_cases:
            ownership_file.write_text(json.dumps(legacy))
            state = json.loads(original_state)
            if cluster_exists:
                group = legacy['settings']['cluster_group']
                state['groups'][group] = {'tags': {'agnosticd_project': 'openshift-cluster-test'}}
                state['cluster'] = {'provisioningState': 'Succeeded'}
            state_file.write_text(json.dumps(state))
            variables = dict(flat, output_dir=str(output), ansible_python_interpreter=sys.executable, **overrides)
            variables_file = root / ('sizing-' + name + '.json')
            variables_file.write_text(json.dumps(variables))
            size_result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook',
                                          str(collection / 'tests/integration/initialize.yml'),
                                          '-e', '@' + str(variables_file)],
                                         cwd=collection, env=env, text=True, capture_output=True, check=False)
            saved = json.loads(ownership_file.read_text())
            if ((size_result.returncode == 0) != expected_success
                    or saved['resource_group_ids'] != legacy['resource_group_ids']
                    or (not expected_success and saved != legacy)):
                print('FAIL: ARO sizing case ' + name)
                print(size_result.stdout + size_result.stderr)
                return 1
            print('PASS: ARO sizing case ' + name)
        state_file.write_text(original_state)
        # Exercise the actual protected asynchronous task and its public error handler.
        install_tasks = yaml.safe_load((collection / 'roles/host_ocp4_aro_install/tasks/main.yml').read_text())
        create_task = next(task for task in install_tasks[0]['block'] if task['name'] == 'Create or reconnect to ARO cluster')
        create_task['block'][0]['poll'] = 1
        create_task['block'][0]['agnosticd.cloud_provider_azure.azure_environment']['config_dir'] = str(root / 'fake-cli')
        fragment = root / 'protected-create.yml'
        fragment.write_text(yaml.safe_dump([create_task], sort_keys=False))
        failure_output = root / 'failure-output'
        failure_output.mkdir()
        failure_vars = dict(flat, config='openshift-cluster', guid='test', project_tag='openshift-cluster-test',
                            aro_version='4.20.1', host_ocp4_deploy_installation_method='aro',
                            output_dir=str(failure_output), ansible_user='azure',
                            ansible_python_interpreter=sys.executable)
        failure_playbook = root / 'failure.yml'
        failure_playbook.write_text(yaml.safe_dump([dict(
            name='Verify public diagnostics from protected ARO provisioning', hosts='localhost',
            connection='local', gather_facts=False, vars=failure_vars,
            tasks=[{'name': 'Initialize synthetic Azure settings', 'ansible.builtin.include_role':
                    {'name': 'agnosticd.cloud_provider_azure.initialize'}},
                   {'name': 'Capture the expected SKU rejection',
                    'block': [{'name': 'Run protected task', 'ansible.builtin.include_tasks': str(fragment)}],
                    'rescue': [{'name': 'Verify public failure details', 'ansible.builtin.assert': {'that': [
                        "'Standard_D8s_v5' in ansible_failed_result.msg",
                        "'NotAvailableForSubscription' in ansible_failed_result.msg",
                        "'synthetic-password' not in ansible_failed_result.msg"]}},
                        {'name': 'Record expected failure', 'ansible.builtin.set_fact': {'failure_observed': True}}]},
                   {'name': 'Require the SKU failure to have been handled',
                    'ansible.builtin.assert': {'that': 'failure_observed | default(false)'}}])], sort_keys=False))
        failure_env = dict(env, AZURE_TEST_RESTRICT_SKU='true', AZURE_TEST_STATE=str(root / 'failure-state.json'))
        failure_result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', str(failure_playbook)],
                                        env=failure_env, cwd=collection, text=True, capture_output=True, check=False)
        if failure_result.returncode or 'synthetic-password' in failure_result.stdout:
            print(failure_result.stdout + failure_result.stderr)
            return 1
        print('PASS: protected asynchronous SKU failure reports public diagnostics')
        aliases = dict(flat)
        aliases['azure_subscription_id'] = aliases.pop('azure_subscription')
        aliases['azure_tenant_id'] = aliases.pop('azure_tenant')
        missing = {k: v for k, v in flat.items() if k != 'azure_service_principal_password'}
        incomplete_nested = dict(flat, azure_sandbox={'credentials': [{'subscription_id': 'synthetic-subscription'}]})
        null_password = dict(flat, azure_service_principal_password=None)
        nested = {'credentials': [dict(subscription_id='synthetic-subscription', app_id='synthetic-app',
                                      password='synthetic-password', tenant='synthetic-tenant',
                                      resource_group='sandbox', azure_region='eastus')]}
        authoritative = dict(flat, azure_sandbox=nested, azure_service_principal_id='unused-flat-app')
        cases = [('flattened', flat, True), ('aliases', aliases, True),
                 ('nested-authoritative', authoritative, True), ('missing-secret', missing, False),
                 ('null-secret', null_password, False), ('no-mixing', incomplete_nested, False),
                 ('malformed-nested', dict(flat, azure_sandbox=None), False)]
        for name, variables, expected_success in cases:
            case_output = root / name
            case_output.mkdir()
            variables['output_dir'] = str(case_output)
            variables['ansible_python_interpreter'] = sys.executable
            variables_file = root / (name + '.json')
            variables_file.write_text(json.dumps(variables))
            case_command = [sys.executable, '-m', 'ansible.cli.playbook',
                            str(collection / 'tests/integration/initialize.yml'),
                            '-e', '@' + str(variables_file)]
            case_result = subprocess.run(case_command, env=env, cwd=collection, text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            if (case_result.returncode == 0) != expected_success or 'synthetic-password' in case_result.stdout:
                print('FAIL: credential normalization case ' + name)
                print(case_result.stdout)
                return 1
            print('PASS: credential normalization case ' + name)
        return 0


if __name__ == '__main__':
    sys.exit(main())
