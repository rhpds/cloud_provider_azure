# template_destroy

Remove the deployment-owned bastion DNS A record and infrastructure resource
group after cluster destruction has completed. The sandbox resource group, DNS
zone, parent delegation, subscription and supplied service principal are retained.

## Prerequisites and variables

Run on localhost with the original variables and restored v2 output archive.
Run `initialize` first so archived subscription, settings and ownership are
validated. [defaults/main.yml](defaults/main.yml) declares the required generated
controller facts and original `project_tag`; it also describes the registered
`azure_result` output. Azure CLI and permissions to delete the owned group and
record are required. No bastion SSH connection is used by this role.

```yaml
- name: Remove infrastructure after cluster cleanup
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.template_destroy
```

## Ordering, ownership and retries

For ARO, call `host_ocp4_aro_destroy` first. For self-managed IPI, the shared
`host_ocp4_destroy` installer role must remove the cluster resource group first.
This role refuses to proceed while that group still exists, including an empty
leftover group. Complete the appropriate cluster cleanup rather than deleting
the ownership check.

Only the `bastion` record with the matching `agnosticd_project` metadata is
deleted. A resource group must have the matching tag before group deletion.
An ownership mismatch or Azure authorization/API failure stops cleanup. The
provider does not delete arbitrary resources by name or infer ownership from
the DNS IP address.

Absent groups and records are accepted. A second completed destruction returns
`azure_result.changed: false`. Group deletion is synchronous, so a later job
does not assume completion merely because a delete request was accepted.
