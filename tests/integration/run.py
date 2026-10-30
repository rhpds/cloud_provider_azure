#!/usr/bin/env python3
"""Run real Ansible modules against the synthetic Azure CLI; never contacts Azure."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile


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
        result = subprocess.run(command, env=env, cwd=collection, check=False)
        if result.returncode:
            return result.returncode
        flat = dict(azure_subscription='synthetic-subscription', azure_service_principal_id='synthetic-app',
                    azure_service_principal_password='synthetic-password', azure_tenant='synthetic-tenant',
                    azure_resource_group='sandbox', azure_region='eastus',
                    azure_dns_zone='test.azure.redhatworkshops.io')
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
