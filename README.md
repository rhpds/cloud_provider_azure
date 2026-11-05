# AgnosticD Azure provider

Collection namespace: `agnosticd.cloud_provider_azure`. This collection supports
new v2 deployments: a Linux bastion, self-managed Azure OpenShift IPI and standard
architecture ARO, with opt-in ARO Hosted Control Planes (HCP) preview support.

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
| [host_ocp4_aro_hcp_credentials](roles/host_ocp4_aro_hcp_credentials/README.md) | Bastion: issue/refresh temporary HCP administrative kubeconfig |
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
Only the six documented credential fields enter the normalized auth mapping;
extra sandbox metadata is ignored. Module redaction protects secret fields rather
than the entire auth/settings dictionaries, keeping public API URLs and opaque
returned credentials intact. Task-level `no_log` protects ARO access operations.

CLI resource operations and access-token requests select the supplied subscription
explicitly. `az account list-locations` uses the current account and rejects the
`--subscription` option, so the wrapper first runs `az account set` with the supplied
subscription inside its private `AZURE_CONFIG_DIR`. This affects only the
deployment's isolated CLI session. A selection failure stops the location lookup.

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
ARO installs only `oc` and `kubectl`, using the exact `aro_version` client archive.
It does not call the shared self-managed installer role or download
`openshift-install`; `host_ocp4_installer_version` has no effect on this client.
See the ARO role's commented defaults for `aro_client_download_url` and
`aro_client_install_dir`. Failed logins report a safe error category while
credentials and raw output stay hidden; TLS verification remains enabled.
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

After destroy or an initialization that created no resources, provision may
replace archived cluster settings, including classic ARO versus HCP, region,
worker count, version and network CIDRs. Read-only checks must confirm that all
archived infrastructure, cluster and ARO-managed resource groups are absent.
Subscription, project tag, sandbox group, deployment group names, cluster name
and DNS zone remain fixed. Existing resources or authorization errors block the
change and preserve the archive. Lifecycle/destroy jobs require the original
cluster settings and region. Errors identify changed public input names and
archived/requested values. Sandbox validation runs before writing the archive,
so failed initialization does not record invalid settings. Keep the archive
rather than deleting it to bypass a mismatch.

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

## Hosted control planes (preview)

### Live validation status

Self-managed Azure IPI and standard ARO have completed live provisioning tests.
HCP remains experimental. The latest HCP test created a ready control plane in
`westeurope`, but the worker pool failed with Azure `DeadlineExceeded` and
`hypershift node pool not cached yet`; no worker VMs appeared. The underlying
cause remains under engineering investigation. Offline checks cover the provider
logic but do not establish successful HCP provisioning. Leave `aro_deploy_hcp`
false to use the validated standard ARO path.

The failure is a terminal Azure deployment state, so increasing the Ansible
timeout cannot recover that operation. Preserve the failed deployment's ARM
operation details and correlation ID for the HCP engineering investigation.
The collection does not automatically delete and recreate failed resources.

### Configuration and lifecycle

Set `host_ocp4_deploy_installation_method: aro` and `aro_deploy_hcp: true` for a
NEW deployment. Default false preserves standard ARO. Missing flags in existing
archives mean standard ARO. Lifecycle/destroy jobs require the archived
architecture. A provision run can replace archived cluster settings only after
verifying that all deployment groups are absent. Keep the ownership archive and
continue using the existing ARO install/destroy roles.

HCP defaults to two Standard_D4s_v5 workers in one node pool/zone, public API and
ingress, managed identities, OVN-Kubernetes, and a public owned encryption vault.
`aro_version` must be an exact advertised HCP release: its minor selects the
control plane, and its full version selects the node pool and client. Standard
master size/subnet inputs are unused. See initialize/defaults for the pool,
logical zone and integration CIDR options. Initialization queries Azure provider
metadata for HCP-specific regions immediately after login, before reading ownership
archives or querying sandbox resources. Unsupported regions fail with the supported
codes returned by Azure; the collection has no hardcoded region allowlist. The
sandbox group's metadata location may differ from the deployment region; its
existing group and DNS zone are retained. HCP quota preflight requires
at least 20 available regional/worker-family cores, or workers plus surge when
larger. No control-plane/bootstrap cores are allocated in this subscription.

Inspect available HCP locations with this read-only command:

```sh
az provider show --namespace Microsoft.RedHatOpenShift \
  --query "resourceTypes[?resourceType=='HCPOpenShiftClusters'].locations | [0]" -o json
```

Azure returns display names (for example, `East US 2`); use their location codes
(`eastus2`) for `azure_region`. The provider matches resource type names without
case sensitivity and normalizes the returned locations. Missing HCP metadata or
access failures abort validation without falling back to classic ARO locations.

Discover regional HCP releases with this read-only command, replacing both
placeholders (classic `az aro get-versions` queries a different resource type):

```sh
az rest --method get --url \
  'https://management.azure.com/subscriptions/<subscription-id>/providers/Microsoft.RedHatOpenShift/locations/<region>/hcpOpenShiftVersions?api-version=2026-09-01-preview'
```

Select an exact advertised release for `aro_version`. See Microsoft's
[HCP configuration guide](https://learn.microsoft.com/azure/openshift/howto-choose-cluster-configuration)
for current preview regions and permanent cluster settings, and the
[cluster creation guide](https://learn.microsoft.com/azure/openshift/howto-create-custom-hosted-cluster)
for subscription permissions and resource prerequisites.

HCP uses pinned ARM REST API 2026-09-01-preview and the checked-in ARM template;
there is no runtime Bicep compiler or preview CLI extension dependency. The
provider creates operator identities, scoped role assignments, dedicated VNet
integration, and an encryption key/vault before the cluster and worker pool.
Failed resource states require explicit destruction; ready resources are reused.
Destroy deletes the parent cluster, which cascades to its node pools, and waits
for cluster and managed-group removal before deleting prerequisites. Azure rejects
deleting the last node pool independently, including a failed pool. On failure
prerequisites remain. The vault's seven-day soft-delete recovery window is
retained; it is never purged automatically. A matching trusted archive permits
recovery of that exact deterministic vault on a later retry.

The vault template omits `enablePurgeProtection` because the Key Vault API rejects
an explicit `false` even for a new vault. New vaults use Azure's default; existing
or recovered vaults retain their protection setting. A vault-creation failure
before cluster creation can be retried using the same owned prerequisites after
correcting the template.

HCP has no built-in kubeadmin login/password. The
[host_ocp4_aro_hcp_credentials role](roles/host_ocp4_aro_hcp_credentials/README.md)
issues temporary admin access, exports the standard kubeconfig and records its
expiry. Credentials expire after 24 hours; rerun that role on the bastion after
initializing the original controller context to refresh all copies. Regular
OIDC users, console authentication, private endpoints, multiple node pools and
automated upgrades are outside this initial implementation. Never use the
standard password-based info role for HCP.

The v2 sample `openshift-cluster-aro-hcp-azure.yml` documents required sandbox
inputs and the preview defaults. Live HCP provisioning is not covered by the
offline test suite.
