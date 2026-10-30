#!/usr/bin/python
# SPDX-License-Identifier: GPL-3.0-or-later
DOCUMENTATION = r'''
---
module: azure_environment
short_description: Manage explicitly owned AgnosticD Azure environments
version_added: 1.0.0
description: Uses isolated Azure CLI credentials and exact resource group ownership.
options:
  auth:
    description: Sandbox credential dictionary.
    type: dict
    required: true
  settings:
    description: Validated provider settings from the initialize role.
    type: dict
    required: true
  config_dir:
    description: Private Azure CLI state directory.
    type: path
    required: true
  project:
    description: Ownership tag value.
    type: str
    required: true
  action:
    description: Provider operation.
    type: str
    required: true
    choices: [validate, installer_prepare, provision, inventory, destroy, aro_install, aro_destroy, start, stop, status]
  template:
    description: Rendered ARM deployment template.
    type: dict
    default: {}
author: AgnosticD contributors
'''
EXAMPLES = r'''
- name: Discover bastion
  agnosticd.cloud_provider_azure.azure_environment:
    action: inventory
    auth: "{{ azure_auth }}"
    settings: "{{ azure_settings }}"
    config_dir: "{{ azure_cli_config_dir }}"
    project: "{{ project_tag }}"
'''
RETURN = r'''
vms:
  description: Owned virtual machines with connection details.
  type: list
  returned: when inventory or lifecycle is requested
'''
from ansible.module_utils.basic import AnsibleModule
from ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.provider import Azure, AzureError, validate_names


def execute(az, action, s, template):
    if action == 'validate':
        group = az.call('group', 'show', '-n', s['sandbox_group'])
        if group['location'].lower() != s['region'].lower():
            raise AzureError('Sandbox resource group location does not match azure_region')
        zone = az.call('network', 'dns', 'zone', 'show', '-g', s['sandbox_group'], '-n', s['dns_zone'])
        return dict(zone=zone)
    if action == 'installer_prepare':
        az.group(s['cluster_group'], s['region'], create=True)
        return {}
    if action == 'provision':
        az.group(s['infra_group'], s['region'], create=True)
        deployment = az.call('deployment', 'group', 'show', '-g', s['infra_group'], '-n', 'bastion', missing=True)
        if not deployment or deployment['properties']['provisioningState'] in ('Failed', 'Canceled'):
            az.call('deployment', 'group', 'create', '-g', s['infra_group'], '-n', 'bastion',
                    '--template-file', template)
            az.changed = True
        elif deployment['properties']['provisioningState'] != 'Succeeded':
            az.call('deployment', 'group', 'wait', '-g', s['infra_group'], '-n', 'bastion', '--created', '--timeout', '3600')
        vms = az.inventory(s)
        if len(vms) != 1 or not vms[0].get('publicIps'):
            raise AzureError('Expected exactly one owned bastion with a public IP')
        record = az.call('network', 'dns', 'record-set', 'a', 'show', '-g', s['sandbox_group'],
                         '-z', s['dns_zone'], '-n', 'bastion', missing=True)
        if record and record.get('metadata', {}).get('agnosticd_project') != az.project:
            raise AzureError('Refusing unowned bastion DNS record')
        if not record:
            az.call('network', 'dns', 'record-set', 'a', 'create', '-g', s['sandbox_group'],
                    '-z', s['dns_zone'], '-n', 'bastion', '--metadata', 'agnosticd_project=' + az.project)
            az.changed = True
        az.call('network', 'dns', 'record-set', 'a', 'add-record', '-g', s['sandbox_group'],
                '-z', s['dns_zone'], '-n', 'bastion', '-a', vms[0]['publicIps'])
        return dict(vms=vms)
    if action == 'inventory':
        return dict(vms=az.inventory(s))
    if action == 'aro_install':
        return az.aro_install(s)
    if action == 'aro_destroy':
        az.aro_destroy(s)
        return {}
    if action == 'destroy':
        # Cluster cleanup must finish first; never delete the bastion underneath a live cluster.
        if az.group(s['cluster_group'], s['region']):
            raise AzureError('Cluster resource group still exists; complete cluster destruction first')
        record = az.call('network', 'dns', 'record-set', 'a', 'show', '-g', s['sandbox_group'],
                         '-z', s['dns_zone'], '-n', 'bastion', missing=True)
        if record:
            if record.get('metadata', {}).get('agnosticd_project') != az.project:
                raise AzureError('Refusing to delete unowned bastion DNS record')
            az.call('network', 'dns', 'record-set', 'a', 'delete', '-g', s['sandbox_group'],
                    '-z', s['dns_zone'], '-n', 'bastion', '--yes')
            az.changed = True
        az.delete_group(s['infra_group'], s['region'])
        return {}
    return az.lifecycle(s, action)


def main():
    module = AnsibleModule(argument_spec=dict(
        auth=dict(type='dict', required=True, no_log=True), settings=dict(type='dict', required=True, no_log=True),
        config_dir=dict(type='path', required=True), project=dict(type='str', required=True),
        action=dict(type='str', required=True, choices=['validate', 'installer_prepare', 'provision', 'inventory', 'destroy',
                    'aro_install', 'aro_destroy', 'start', 'stop', 'status']),
        template=dict(type='dict', default={})), supports_check_mode=False)
    p = module.params
    try:
        validate_names(p['settings'], p['settings']['sandbox_group'])
        az = Azure(p['auth'], p['config_dir'], p['project'])
        az.login()
        # ARM templates are passed in a private temporary file, never command line JSON.
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'template.json')
            with open(path, 'w') as stream:
                json.dump(p['template'], stream)
            os.chmod(path, 0o600)
            result = execute(az, p['action'], p['settings'], path)
        module.exit_json(changed=az.changed, **result)
    except (AzureError, OSError, ValueError, KeyError, StopIteration) as exc:
        module.fail_json(msg=str(exc))


if __name__ == '__main__':
    main()
