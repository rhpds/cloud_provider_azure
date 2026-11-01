# installer_credentials

Install Azure CLI on the bastion and write the service-principal credential file
expected by the self-managed OpenShift installer for Azure. The v2 Azure
prerequisite task files in `host_ocp4_provisioner` and `host_ocp4_destroy` call
this role for installation and cleanup respectively.

## Required context and variables

Run on the bastion after `initialize` has generated `azure_auth` on localhost.
The bastion must be reachable by SSH and support the `install_cli` role's
package/repository privileges. [defaults/main.yml](defaults/main.yml) documents
`ansible_user`, the controller's secret `azure_auth` fact and `azure_cli_version`.
`ansible_user` comes from inventory and must have a home under `/home/<user>`.

```yaml
- name: Prepare Azure IPI installer credentials on the bastion
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.installer_credentials
```

## Credential file and lifecycle

The role creates `/home/<ansible_user>/.azure` with mode `0700`, then writes
`osServicePrincipal.json` with mode `0600` and these exact installer keys:

| File key | Source in `hostvars.localhost.azure_auth` |
| --- | --- |
| `subscriptionId` | `subscription_id` |
| `clientId` | `app_id` |
| `clientSecret` | `password` |
| `tenantId` | `tenant` |

The write uses `no_log: true`. No new Azure principal is created, and no ambient
CLI session is used to discover a credential. The file is intentionally retained
for later installer/destroy operations. It contains a real principal secret;
limit access to the bastion and archive access accordingly. Repeated calls
reconcile the file content and permissions.

This file is an IPI installer requirement. ARO installation instead uses its own
temporary CLI session, which `host_ocp4_aro_install` removes in its `always` block.
See [install_cli](../install_cli/README.md) for supported OS releases and package
configuration.
