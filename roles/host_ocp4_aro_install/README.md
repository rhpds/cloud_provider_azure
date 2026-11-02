# host_ocp4_aro_install

Create or reconnect to a standard Azure Red Hat OpenShift cluster, configure
bastion access, and export the cluster credentials to the controller. The v2
`host_ocp4_deploy` dispatcher selects this role when
`host_ocp4_deploy_installation_method: aro`. Set `aro_deploy_hcp: true` to select the HCP preview branch; see the collection guide.

## Prerequisites and execution

Run `initialize` on localhost first and provision/configure the bastion. The
dispatcher includes this role inside a block delegated to the first bastion;
direct callers must provide equivalent delegation or run on the bastion. The
target must support the RHEL CLI installer and `become` for package operations.
The shared v2 `host_ocp4_installer_info` role must be available for publishing
access metadata. ARO does not invoke `host_ocp4_installer` or download/run
`openshift-install`. The selected ARO version, region, SKU availability, Azure permissions
and regional/family core quotas must permit deployment.

## Variables

[defaults/main.yml](defaults/main.yml) documents every directly used input,
generated result and task-local variable. Configure cluster infrastructure with
the `aro_*` inputs of [initialize](../initialize/defaults/main.yml). Its normalized
`azure_settings.version` must be non-empty and worker count must be at least three.

The principal in `azure_auth` always authenticates provisioning. `aro_identity_mode`
chooses the cluster identity (`managed_identity` or `service_principal`), not the
authentication method for this Ansible run. `ocp4_pull_secret` is optional and
defaults to an empty string; an explicit secret may be JSON text or a mapping.

`ansible_user` identifies the bastion login account. `cluster_name` names the
local authentication directory `/home/<ansible_user>/<cluster_name>/auth`; the
Azure resource itself is named by `aro_cluster_name`. `config`, `guid` and
`output_dir` supply the standard controller archive filenames. If
`bastion_setup_student_user` is true, `bastion_student_user_name` must refer to an
already-created user. The role copies its kubeconfig but does not create users.

## Provisioning and retries

The module validates IPv4 network containment/non-overlap and group ownership.
For a new cluster it checks provider registrations, regionally available ARO
versions, selected VM SKU restrictions and core quotas (including an additional
control-plane-sized bootstrap VM). It then creates the network/subnets and calls
the ARO API. Preflight may register an Azure resource provider if required.

Creation runs asynchronously with a 7,500-second task limit and 30-second poll
interval. The provider waits for a ready cluster; an existing ready cluster is
reused. Failed/non-progressing cluster states are reported instead of silently
recreating the cluster. Explicit destruction is required before recreating a
failed cluster. Initialization separately prevents changing the recorded VM sizes
of an existing cluster during a provision run.

The module result and credential-bearing tasks use `no_log`. Failure handling
publishes only selected region/sizes and structured non-secret SKU/quota details.
Do not debug `azure_aro_result` or the login result to bypass that protection.

## Access artifacts and cleanup

The collection's `install_client.yml` tasks download only `oc` and `kubectl` for
the exact ARO version, independently of `host_ocp4_installer_version` and other
self-managed installer options. `aro_client_download_url` selects the official
HTTPS archive for the bastion's architecture/OS by default; an explicit override
can point to a trusted mirror or an existing archive on that bastion. The default
`aro_client_install_dir` is `/usr/local/bin`. Login invokes that exact `oc` path
so a different client in `PATH` cannot take precedence.

After successful
`oc login`, the role stores kubeconfig and kubeadmin password under the bastion
authentication directory, exports them to these controller files and protects
them with mode `0600`:

- `<output_dir>/<config>_<guid>_kubeconfig`
- `<output_dir>/<config>_<guid>_kubeadmin-password`

It also writes the bastion login user's `.kube/config`, optionally copies it to
the student user, and invokes the shared role that publishes standard OpenShift
access information. Preserve the output archive for workloads and later jobs.

The temporary bastion Azure CLI state at `/home/<ansible_user>/.azure-agnosticd`
is removed in an `always` block, on success or failure. Persistent kubeconfigs and
the exported password are intentionally retained. Destruction later runs
`host_ocp4_aro_destroy` on the controller before bastion infrastructure cleanup.

The provider module protects the principal password and optional pull secret as
individual argument fields. Whole settings mappings must not be marked secret:
Ansible also removes those values from returned URLs and passwords, which makes
connection data unusable. ARO provisioning/login tasks retain task-level `no_log`
protection for returned kubeadmin credentials. On login failure, the role reports
a safe category (TLS, DNS, rejected credentials, connection or corrupted URL)
without printing the raw command output or credentials. TLS verification stays
enabled.

```yaml
- name: Install through the normal v2 dispatcher
  ansible.builtin.include_role:
    name: host_ocp4_deploy
  vars:
    host_ocp4_deploy_installation_method: aro
```

The dispatcher supplies bastion delegation; do not put `delegate_to` directly on
an `include_role` task. See the v2 Azure sample for credential and version inputs.

## HCP branch

With `aro_deploy_hcp: true`, the provider uses the HCP resource type and owned
ARM prerequisites instead of the classic API. It waits for cluster and node pool,
installs the exact selected client, then invokes `host_ocp4_aro_hcp_credentials`.
The classic password login/access task file is skipped. HCP uses two workers by
default and requires managed identity. No openshift-install program is used in
either ARO architecture. See the collection's HCP section and initialize/defaults
for region, quota, version, subnet and recovery requirements.
