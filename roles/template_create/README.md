# template_create

Deploy or reuse the owned Azure bastion ARM deployment and reconcile its owned
`bastion` DNS A record. This role provisions the bastion infrastructure; it does
not install OpenShift or discover Ansible inventory.

## Prerequisites and variables

Run on the controller after `initialize` and `template_generate`. Azure CLI,
subscription permissions, the existing sandbox DNS zone and the provision public
key must already be available. [defaults/main.yml](defaults/main.yml) documents
the generated controller facts `azure_auth`, `azure_settings`,
`azure_cli_config_dir`, `azure_arm_template` and the required `project_tag`.
Choose bastion options through `template_generate` rather than editing the ARM
mapping or normalized settings directly.

```yaml
- name: Deploy the rendered bastion infrastructure
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.template_create
```

## Behavior on retries

The provider creates the infrastructure group only when absent, and requires
the exact `agnosticd_project` tag when it already exists. ARM deployment name
`bastion` is created when absent or previously Failed/Canceled, waited on when
in progress, and reused when Succeeded. A successful deployment is not updated
merely because template variables changed. Reuse is followed by inventory
validation: exactly one owned bastion with a public IP must be found.

The `bastion` A record is written in the existing sandbox zone with ownership
metadata and IP in one conditional ARM request. An unchanged owned record is a
no-op. Existing records without the correct ownership metadata are refused;
they are not adopted even if the address matches. Concurrent modification fails
instead of silently overwriting another writer's record.

The registered `azure_result` contains the module's `changed` flag and bastion
`vms` list. Run `create_inventory` next, followed by the shared SSH configuration
role. See [the ownership guide](../../README.md#ownership-and-cleanup) before
repairing a record created by an earlier collection version.
