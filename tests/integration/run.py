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
        flat = dict(azure_subscription='synthetic-subscription', azure_service_principal_id='synthetic-app',
                    azure_service_principal_password='synthetic-password', azure_tenant='synthetic-tenant',
                    azure_resource_group='sandbox', azure_region='eastus',
                    azure_dns_zone='test.azure.redhatworkshops.io')
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
                        "'Standard_D8s_v3' in ansible_failed_result.msg",
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
