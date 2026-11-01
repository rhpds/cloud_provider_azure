# AgnosticD Azure provider

Collection namespace: `agnosticd.cloud_provider_azure`. This collection supports
new v2 deployments: a Linux bastion, self-managed Azure OpenShift IPI and standard
architecture ARO.

## Roles

Each role has a README describing prerequisites, execution location, inputs,
outputs, retry/cleanup behavior and an invocation example. Every project variable
directly used by its tasks/templates is declared and documented in that role's
`defaults/main.yml`, including generated results. Required values use
`undef(hint=...)` so declaring them does not fabricate credentials or disable
undefined-variable fallbacks. Ansible magic variables (`hostvars`, `groups`, loop
items) are supplied by Ansible and are not replaced with defaults. Connection
context comes from inventory. Options of included shared v2 roles are documented
by those roles; the collection READMEs identify those dependencies.

| Role | Purpose and execution location |
| --- | --- |
| [initialize](roles/initialize/README.md) | Controller: normalize credentials, validate sandbox and archive ownership |
| [template_generate](roles/template_generate/README.md) | Controller: render Linux bastion ARM template |
| [template_create](roles/template_create/README.md) | Controller: create/reuse bastion infrastructure and owned DNS record |
| [create_inventory](roles/create_inventory/README.md) | Controller: discover owned bastion and populate `bastions` inventory |
| [configure_linux_hosts](roles/configure_linux_hosts/README.md) | Bastion: wait for SSH and optionally add authorized keys |
| [install_cli](roles/install_cli/README.md) | RHEL target: install pinned Azure CLI RPM and repository |
| [installer_prepare](roles/installer_prepare/README.md) | Controller: create/verify the owned self-managed IPI group |
| [installer_credentials](roles/installer_credentials/README.md) | Bastion: write the secret IPI service-principal file |
| [installer_platform](roles/installer_platform/README.md) | Render Azure YAML fragments for the shared IPI installer |
| [host_ocp4_aro_install](roles/host_ocp4_aro_install/README.md) | Delegated bastion: provision/reconnect ARO and export access files |
| [host_ocp4_aro_destroy](roles/host_ocp4_aro_destroy/README.md) | Controller: delete ARO before its owned network/identity group |
| [lifecycle](roles/lifecycle/README.md) | Controller: start, stop or report status using a separate lifecycle action |
| [template_destroy](roles/template_destroy/README.md) | Controller: remove owned bastion/DNS after cluster cleanup |

For a normal provision run, initialize on localhost, prepare SSH keys, render and
deploy the bastion template, discover inventory, generate SSH config, configure
the guest, then invoke the selected OpenShift method. Destruction reverses the
dependency order: workload cleanup, cluster cleanup, then bastion/DNS cleanup.
Use the original settings and output archive in subsequent jobs. The v2 checkout
provides extensively commented ARO/IPI samples and `docs/azure.md` with entrypoints.

## Sandbox contract

The sandbox owns subscription allocation/release, the existing resource group,
and the delegated DNS zone. Supply either the flattened sandbox variables:

```yaml
azure_subscription: <subscription UUID>
azure_service_principal_id: <service principal application UUID>
azure_service_principal_password: <secret>
azure_tenant: redhat0.onmicrosoft.com
azure_resource_group: openenv-example
azure_region: eastus
azure_dns_zone: example.azure.redhatworkshops.io
```

`azure_subscription_id` and `azure_tenant_id` are accepted aliases when
`azure_subscription` and `azure_tenant`, respectively, are undefined.
`azure_application` and `sandbox_name` are metadata, not authentication inputs.

Alternatively, supply exactly one nested credential entry:

```yaml
azure_sandbox:
  credentials:
  - subscription_id: <subscription UUID>
    app_id: <service principal application UUID>
    password: <secret>
    tenant: redhat0.onmicrosoft.com
    resource_group: openenv-example
    azure_region: eastus
    # pool_subscription, azure_account and sandbox id are optional metadata.
```

If `azure_sandbox` is supplied, that payload is authoritative and must be
complete. Missing fields are not filled from flattened credentials or ambient
sessions. Without `azure_sandbox`, the flattened variables are normalized to the
same internal authentication/settings contract for provision, destroy and lifecycle.

The existing zone defaults to `<guid>.azure.redhatworkshops.io` in the sandbox
resource group. Override `azure_root_dns_zone` or `azure_dns_zone` as appropriate.
The sandbox must delegate this zone from its parent. No parent-zone credentials
are needed. Credentials are never obtained from ambient CLI sessions.

Use subscription Owner or equivalent permissions for resource/provider creation,
resource-group deletion and role assignments. ARO service-principal mode also
needs directory read access to resolve the supplied principal and ARO resource
provider principal. This provider never allocates subscriptions or creates tenant
applications. Managed identities are created by Azure CLI in the owned cluster
resource group. An existing ARO resource-provider identity is a tenant prerequisite.

## Installation

Use `config: openshift-cluster` and `cloud_provider: azure` in v2.

```yaml
host_ocp4_deploy_installation_method: openshift_install
ocp4_pull_secret: <Red Hat pull secret>
host_ocp4_installer_version: '4.20'
```

Or:

```yaml
host_ocp4_deploy_installation_method: aro
aro_version: <exact version from az aro get-versions --location eastus>
aro_identity_mode: managed_identity  # alternatively service_principal
```

ARO uses public API/ingress and Azure-managed endpoint DNS. Its pull secret is
optional. Defaults are three workers, D8s_v5 control plane, D4s_v5 workers and
128 GiB worker disks. Override `aro_master_vm_size`, `aro_worker_vm_size`,
`aro_worker_count`, `aro_worker_disk_size`, `aro_vnet_cidr`,
`aro_master_subnet_cidr`, `aro_worker_subnet_cidr`, `aro_pod_cidr`, and
`aro_service_cidr`. ARO deployment checks region/version, provider registrations,
VM SKU restrictions and regional/family core quotas, allowing an extra control
plane-sized bootstrap VM. Azure CLI validates ARO permissions during creation.
Failed clusters require diagnosis and explicit destruction before recreation.
Preflight errors report the selected region and node sizes, SKU restriction
reasons, and required/available core quota while credentials remain protected.
Changing VM sizes cannot bypass subscription restrictions or family quota limits.

The bastion defaults to Azure user `azure`, PAYG RHEL 9, Standard_D2s_v5 and a
64 GiB Premium disk. Configure `azure_bastion_user`, `azure_bastion_vm_size`, and
`azure_bastion_ssh_source` (default `*`). Its SSH key comes from v2's existing
provision-key role. A Standard static IP and SSH-only NSG supply access.
Marketplace availability and RHEL subscription/repository configuration remain
prerequisites, following the normal v2 bastion roles.

Override the complete `azure_bastion_image` Marketplace image reference to use
RHEL 10 for a new deployment:

```yaml
azure_bastion_image:
  publisher: RedHat
  offer: RHEL
  sku: 10-lvm-gen2
  version: latest
```

Successful infrastructure deployments are reused. Changing this variable does
not reimage an existing bastion; use a new deployment GUID to provision another
environment with the selected image.

## Ownership and cleanup

Default owned groups are `<config>-<guid>-infra` and `<config>-<guid>-cluster`;
override `azure_infra_resource_group` and `azure_cluster_resource_group` before
provisioning. They must differ from each other and from the sandbox group.
Existing groups must carry `agnosticd_project: <project_tag>`; unowned groups and
DNS records are refused. The DNS record `bastion` uses the same ownership metadata.
DNS addresses and ownership metadata are written together using a conditional ARM
request. Ready records are reused without another write. Earlier versions used
CLI `add-record`, which can discard metadata; those existing records require an
operator-approved metadata repair after verifying their IP and deployment ownership.
The provider does not automatically adopt records with missing ownership metadata.
IPI takes ownership of its initially empty cluster group and deletes it through
`openshift-install destroy cluster`. ARO has a separate Azure-managed resource
group, deleted through the ARO API; the provider never powers its VMs directly.

Destroy runs workload cleanup, deletes the cluster, then removes its owned
identities/network and bastion infrastructure. Failures stop cleanup before the
bastion is deleted. The sandbox group, zone, parent delegation and supplied
service principal are preserved. Already absent resources are handled idempotently.

Restore the original variables and v2 output archive for lifecycle/destroy jobs.
The archive contains SSH keys, installer artifacts and
`<config>_<guid>_azure_ownership.json` with subscription, exact group IDs and DNS
ownership. Changed ownership settings or subscription mismatches are refused. ARO control-plane
and worker VM sizes may change before cluster creation after a read-only absence
check; existing clusters keep their recorded sizes for lifecycle/destroy. Provision
runs reject attempts to change sizes of an existing cluster. If a bastion
and its IPI state have been lost, recover the installer state before destruction;
the provider intentionally refuses to delete a remaining IPI group blindly.
Do not put unrelated resources in deployment-owned groups.

`start`, `stop`, and `status` apply to the bastion and self-managed cluster VMs.
For ARO they report cluster status but only start/deallocate the bastion.
The lifecycle role defaults `azure_lifecycle_action` to the run's `ACTION`.
Destroy preparation sets `azure_lifecycle_action: start` to start the bastion
before cluster cleanup while preserving the run's `ACTION: destroy`.
Status is written to `output_dir/status.txt`. Kubeconfig and access facts follow
the standard v2 contract, including `<config>_<guid>_kubeconfig` on the controller.

## Runtime and verification

Controller and RHEL bastion use Azure CLI 2.90.0. The existing v2 public execution
environment Containerfile pins that version; rebuild the EE before use.
The bastion installer selects Microsoft's distribution-specific RHEL 8/9/10
repository and signing key, replaces the legacy `azure-cli` repository URL on
reruns, and refreshes DNF metadata before installing the pinned package.
No ARO preview extension, Bicep or Azure Python SDK is needed by this collection: its
module uses CLI JSON and Python's standard library. Collection dependencies are
declared in `galaxy.yml`; v2 supplies `agnosticd.core` and shared host roles.

Run `python -m unittest discover -s tests/unit -v`, `ansible-galaxy collection
build`, and `ansible-lint --offline` from this collection. The offline integration
playbook is `tests/integration/provider.yml`; run it with
`python tests/integration/run.py` using a Python environment with ansible-core.
The runner creates an isolated collection path and fresh state/output directories,
prepends the fake CLI to PATH, and also validates the v2 installer template when
this collection is next to the v2 checkout. It never calls Azure; all credentials
in the fixture are synthetic.

Before release, run live Azure IPI and both ARO identity-mode deployments,
reruns, workload access, lifecycle actions, destruction and repeated destruction.
Verify preservation of sandbox/unrelated resources and removal of owned groups,
identities and role assignments. Offline tests do not prove quota, marketplace,
Azure policy, ARM API or runtime-image compatibility.

References: [Microsoft ARO creation](https://learn.microsoft.com/en-us/azure/openshift/howto-create-openshift-cluster),
[ARO CLI](https://learn.microsoft.com/en-us/cli/azure/aro),
[Red Hat Azure installation](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html-single/installing_on_azure/installing_on_azure).
