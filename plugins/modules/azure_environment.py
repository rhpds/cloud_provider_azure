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
    description: Normalized sandbox credential dictionary. Only the password is secret.
    type: dict
    required: true
    suboptions:
      subscription_id:
        description: Azure subscription UUID.
        type: str
        required: true
      app_id:
        description: Service principal application/client UUID.
        type: str
        required: true
      password:
        description: Service principal secret, protected with no_log.
        type: str
        required: true
      tenant:
        description: Tenant UUID or domain.
        type: str
        required: true
      resource_group:
        description: Existing sandbox resource group, retained for the normalized role contract.
        type: str
      azure_region:
        description: Deployment location code supplied with sandbox credentials, retained for the normalized role contract.
        type: str
  settings:
    description:
    - Validated non-secret provider settings from initialize, plus an optional protected pull secret.
    - Public settings must not be marked no_log as a group; Ansible also replaces their values inside returned URLs and credentials.
    type: dict
    required: true
    suboptions:
      sandbox_group:
        description: Existing sandbox resource group.
        type: str
      region:
        description: Azure deployment location code.
        type: str
      infra_group:
        description: Owned bastion resource group.
        type: str
      cluster_group:
        description: Owned cluster resource group.
        type: str
      cluster_name:
        description: ARO resource name.
        type: str
      dns_zone:
        description: Existing delegated bastion DNS zone.
        type: str
      method:
        description: OpenShift installation method.
        type: str
      identity_mode:
        description: ARO cluster identity mode.
        type: str
      version:
        description: Exact selected ARO release.
        type: str
      master_size:
        description: Control-plane Azure VM SKU.
        type: str
      worker_size:
        description: Worker Azure VM SKU.
        type: str
      worker_count:
        description: Number of ARO workers.
        type: int
      worker_disk:
        description: Worker OS disk capacity in GiB.
        type: int
      vnet_cidr:
        description: ARO machine VNet IPv4 CIDR.
        type: str
      master_cidr:
        description: Control-plane subnet IPv4 CIDR.
        type: str
      worker_cidr:
        description: Worker subnet IPv4 CIDR.
        type: str
      pod_cidr:
        description: Pod network IPv4 CIDR.
        type: str
      service_cidr:
        description: Service network IPv4 CIDR.
        type: str
      hcp:
        description: Select hosted control planes instead of classic ARO.
        type: bool
        default: false
      hcp_recover_vault:
        description: Internal permission derived from a validated existing ownership archive.
        type: bool
        default: false
      hcp_csr:
        description: PEM certificate request used for HCP temporary admin access.
        type: str
      hcp_nodepool:
        description: Single HCP worker node pool name.
        type: str
      hcp_vault_name:
        description: Deterministic owned Key Vault name archived for HCP recovery.
        type: str
      hcp_zone:
        description: Logical Azure availability zone for the HCP worker pool.
        type: str
      hcp_integration_cidr:
        description: Dedicated delegated integration subnet IPv4 CIDR.
        type: str
      pull_secret:
        description: Optional Red Hat pull secret as JSON text or a mapping, protected with no_log.
        type: raw
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
    choices: [validate, installer_prepare, provision, inventory, destroy, check_region_change, check_deployment_change, aro_check_sizes, aro_install, aro_destroy, hcp_check_region, hcp_credentials, start, stop, status]
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
credentials:
  description: Secret ARO kubeadmin credentials. Call aro_install with task-level no_log enabled.
  type: dict
  returned: when classic aro_install is requested
kubeconfig:
  description: Secret HCP certificate kubeconfig, without the locally held private key. Use task-level no_log.
  type: str
  returned: when hcp_credentials is requested
expirationTimestamp:
  description: Expiry of the temporary HCP administrative certificate.
  type: str
  returned: when hcp_credentials is requested
supported_regions:
  description: HCP region codes advertised by the subscription's Azure provider metadata.
  type: list
  elements: str
  returned: when hcp_check_region is requested
'''
from ansible.module_utils.basic import AnsibleModule
from ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.provider import Azure, AzureError, validate_names
from ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.hcp import HCP, check_region


def execute(az, action, s, template):
    if action == 'hcp_check_region':
        return dict(supported_regions=check_region(az, s['region']))
    if action == 'validate':
        if s.get('hcp'):
            HCP(az, s).validate()
        # The sandbox group's location stores its metadata; deployment resources
        # may use another region. Still require access to the supplied group/zone.
        az.call('group', 'show', '-n', s['sandbox_group'])
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
        az.ensure_bastion_dns(s, vms[0]['publicIps'])
        return dict(vms=vms)
    if action == 'inventory':
        return dict(vms=az.inventory(s))
    if action == 'aro_install':
        if s.get('hcp'):
            return HCP(az, s).install(template)
        return az.aro_install(s)
    if action == 'hcp_credentials':
        return HCP(az, s).credentials(s['hcp_csr'])
    if action == 'aro_check_sizes':
        az.check_aro_size_change(s)
        return {}
    if action == 'check_deployment_change':
        az.check_deployment_change(s)
        return {}
    if action == 'check_region_change':
        az.check_region_change(s)
        return {}
    if action == 'aro_destroy':
        if s.get('hcp'):
            HCP(az, s).destroy()
            return {}
        az.aro_destroy(s)
        return {}
    if action == 'destroy':
        # Cluster cleanup must finish first; never delete the bastion underneath a live cluster.
        if az.group(s['cluster_group'], s['region']):
            raise AzureError('Cluster resource group still exists; complete cluster destruction first')
        record = az.call('network', 'dns', 'record-set', 'a', 'show', '-g', s['sandbox_group'],
                         '-z', s['dns_zone'], '-n', 'bastion', missing=True)
        if record:
            if (record.get('metadata') or {}).get('agnosticd_project') != az.project:
                raise AzureError('Refusing to delete unowned bastion DNS record')
            az.call('network', 'dns', 'record-set', 'a', 'delete', '-g', s['sandbox_group'],
                    '-z', s['dns_zone'], '-n', 'bastion', '--yes')
            az.changed = True
        az.delete_group(s['infra_group'], s['region'])
        return {}
    return az.lifecycle(s, action)


def argument_spec():
    # no_log values are removed from the entire serialized result, not only
    # invocation arguments. Protect secrets individually so public region/method
    # names and integer counts cannot corrupt API URLs or returned passwords.
    settings_options = {key: dict(type='str') for key in (
        'sandbox_group', 'region', 'infra_group', 'cluster_group', 'cluster_name', 'dns_zone', 'method',
        'identity_mode', 'version', 'master_size', 'worker_size', 'vnet_cidr', 'master_cidr',
        'worker_cidr', 'pod_cidr', 'service_cidr', 'hcp_nodepool', 'hcp_zone', 'hcp_integration_cidr', 'hcp_vault_name')}
    settings_options.update(hcp=dict(type='bool', default=False), hcp_recover_vault=dict(type='bool', default=False),
                            hcp_csr=dict(type='str', no_log=True))
    settings_options.update(worker_count=dict(type='int'), worker_disk=dict(type='int'),
                            pull_secret=dict(type='raw', no_log=True))
    return dict(
        auth=dict(type='dict', required=True, options=dict(
            subscription_id=dict(type='str', required=True), app_id=dict(type='str', required=True),
            password=dict(type='str', required=True, no_log=True), tenant=dict(type='str', required=True),
            resource_group=dict(type='str'), azure_region=dict(type='str'))),
        settings=dict(type='dict', required=True, options=settings_options),
        config_dir=dict(type='path', required=True), project=dict(type='str', required=True),
        action=dict(type='str', required=True, choices=['validate', 'installer_prepare', 'provision', 'inventory', 'destroy',
                    'check_region_change', 'check_deployment_change', 'aro_check_sizes', 'aro_install', 'aro_destroy', 'hcp_check_region', 'hcp_credentials',
                    'start', 'stop', 'status']),
        template=dict(type='dict', default={}))


def main():
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=False)
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
        module.fail_json(msg=str(exc), diagnostic=getattr(exc, 'diagnostic', {}))


if __name__ == '__main__':
    main()
