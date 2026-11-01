# host_ocp4_aro_destroy

Delete the standard ARO cluster through the Azure ARO API, then remove its owned
cluster resource group containing the network and identities. Run before
`template_destroy` removes the bastion and its DNS record.

## Inputs and run location

[defaults/main.yml](defaults/main.yml) documents the required `project_tag` and
the `azure_auth`, `azure_settings`, `azure_cli_config_dir` facts produced on
localhost by `initialize`. Restore the original deployment variables and output
archive before initialization; do not guess resource names or change the
subscription to reach a different cluster.

All tasks delegate to localhost even when the role is included from a bastion
play. The controller must have Azure CLI and permission to delete ARO and the
owned group. This role needs neither SSH to the bastion nor a working OpenShift
API/kubeconfig, allowing cleanup after incomplete installation.

```yaml
- name: Delete the managed cluster before bastion infrastructure
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.host_ocp4_aro_destroy
```

## Ordering, ownership and retries

The cluster group must carry the exact `agnosticd_project` tag. If ARO exists,
the role calls synchronous `az aro delete` before attempting to delete the
cluster group. Azure handles the ARO-managed resource group through that API;
the provider does not power or individually delete its managed VMs.

If ARO deletion fails, the cluster group's network/identities remain available
for diagnosis or another delete attempt. If ARO is already absent, the owned
leftover group is still removed; this covers failed preflight runs that created
the group before cluster creation. An already absent cluster group is a no-op.

The supplied sandbox group, zone and service principal are preserved. Bastion
infrastructure is intentionally left for `template_destroy` after cluster cleanup.
There are no public access facts or kubeconfig outputs from this role.
