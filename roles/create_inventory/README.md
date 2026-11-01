# create_inventory

Discover the deployment-owned bastion and add it to Ansible's dynamic `bastions`
group. Run on the controller after `initialize` and usually after `template_create`.
The role queries Azure but does not create or power resources, configure the
guest, or generate an SSH config.

## Variables and prerequisites

[defaults/main.yml](defaults/main.yml) documents `project_tag`,
`azure_bastion_user` and the controller facts read from `hostvars.localhost`.
`azure_auth`, `azure_settings` and `azure_cli_config_dir` must have been generated
by `initialize` on localhost. `azure_bastion_user` defaults to `azure` and must
match the user chosen when the VM was created.

```yaml
- name: Discover the Azure bastion
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.create_inventory
```

## Inventory contract

Only VMs in the owned infrastructure resource group whose `agnosticd_project`
tag matches `project_tag` are included. An absent owned group yields an empty
list; an unowned existing group fails. For every discovered VM the role sets:

| Inventory field | Value / consumer |
| --- | --- |
| inventory hostname and `ansible_host` | Azure VM name, matching the shared SSH config's `Host` entry |
| `public_ip_address` / `private_ip_address` | Azure public/private addresses |
| `public_dns_name` | `bastion.<azure_settings.dns_zone>` |
| `ansible_user` / `cloud_user` | `azure_bastion_user` |
| `instance_canonical_name` / `shortname` | `bastion` |
| `isolated` | `false` |

The registered `azure_result.vms` contains the queried VM records. Dynamic
inventory exists only for this playbook execution; restore it in a later job by
running the role again. Follow it with `infra_common_ssh_config_generate` from
v2 and use that generated SSH config before running bastion tasks. The VM-name
connection target is deliberate: it selects the correct provision key from the
SSH `Host` entry even though the entry resolves to a DNS hostname.

`hostvars`, `groups`, `item` and Ansible connection variables are supplied by
Ansible. The loop's `item` is a discovered VM, not a configurable role input.
