# initialize

Normalize the sandbox credentials and deployment settings on the controller,
validate the existing sandbox group and DNS zone, and record the ownership
boundaries used by later provisioning and cleanup jobs. This is the first Azure
role in provision, lifecycle and destroy runs.

## Run location and prerequisites

Run on `localhost` with a local connection. Azure CLI must already be available
in the controller execution environment. Supply an existing writable `output_dir`,
a unique `guid`, `config`, and the original `project_tag` ownership value.
The sandbox must already own the subscription, resource group and delegated DNS
zone. This role does not allocate a sandbox, create the DNS zone, or install CLI.

## Variables

Every input and generated variable is described beside its declaration in
[defaults/main.yml](defaults/main.yml). Required inputs use `undef(hint=...)`:
they are declared but intentionally stay undefined until the caller supplies them.
This preserves conditional checks and compatibility aliases.

Provide one of these complete credential forms through Vault or another secrets
source. The following examples contain placeholders only:

```yaml
azure_subscription: <subscription UUID>
azure_service_principal_id: <application/client UUID>
azure_service_principal_password: <secret>
azure_tenant: <tenant UUID or domain>
azure_resource_group: <existing sandbox group>
azure_region: eastus
```

```yaml
azure_sandbox:
  credentials:
  - subscription_id: <subscription UUID>
    app_id: <application/client UUID>
    password: <secret>
    tenant: <tenant UUID or domain>
    resource_group: <existing sandbox group>
    azure_region: eastus
```

When `azure_sandbox` is defined it is authoritative, must contain exactly one
complete credential, and is never filled from flat variables. To use flat
credentials, omit `azure_sandbox` entirely; setting it to null or an empty mapping
is invalid. The flat `azure_subscription_id` and `azure_tenant_id` aliases apply
only when their corresponding canonical variables are undefined. Neither form
uses an ambient `az login` session as its authentication source.

Set `azure_dns_zone` to the existing delegated zone when it differs from
`<guid>.azure.redhatworkshops.io`. Current validation requires the sandbox resource
group's location to match `azure_region`. This is a collection validation rule;
setting a different region alone does not bypass it.

For ARO, set `host_ocp4_deploy_installation_method: aro` and an exact `aro_version`
returned by `az aro get-versions --location <region>`. The `aro_*` defaults specify
cluster identity, VM sizing, worker count and CIDRs. They are separate from IPI's
`control_plane_instance_type` and `worker_instance_type`.

## Outputs and ownership

The role sets `azure_auth` (secret), `azure_settings` (non-secret),
`azure_cli_config_dir`, normalized flat credential fields, and compatibility
aliases on localhost. Later roles read those facts; do not configure `azure_auth`
or `azure_settings` directly. See the defaults for the complete output inventory.
The CLI state directory contains authentication material and stays private.

The file `<output_dir>/<config>_<guid>_azure_ownership.json` records subscription,
project tag, exact group names/IDs, DNS record identity and non-secret settings.
It is written with mode `0600`. Keep the original variables and archive for
subsequent lifecycle/destroy runs. Changes to ownership boundaries are rejected.
Before a cluster exists, ARO master/worker sizes may change after a read-only
absence check. For an existing cluster, provision rejects changed sizes and
lifecycle/destroy retain the archived sizes. Other settings must still match.

Initialization verifies resources and writes the local ownership archive; it
does not provision the bastion or OpenShift. Authentication and authorization
errors remain failures rather than being interpreted as missing resources.

## Example

```yaml
- name: Initialize the Azure deployment
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
  - name: Normalize credentials and establish ownership
    ansible.builtin.include_role:
      name: agnosticd.cloud_provider_azure.initialize
```

Supply the deployment context and credentials through the play's existing v2
variables. Continue with `template_generate` / `template_create` for provision,
or the appropriate lifecycle/destruction role. See the
[collection guide](../../README.md) for the complete flow.
