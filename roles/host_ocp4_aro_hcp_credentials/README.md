# host_ocp4_aro_hcp_credentials

Issue or renew the HCP cluster's temporary administrative kubeconfig. Run on the
existing bastion, after `initialize` has run on localhost with the original
variables and output archive. Azure CLI and the matching OpenShift client must
already be installed. The role installs the RHEL `python3-cryptography` package
with privilege escalation for certificate generation. This role does not create
a cluster, node pool or user. The controller needs the Kubernetes Python client
for metadata queries, as with classic ARO's shared information role.

All inputs, internal values and outputs are documented in
[defaults/main.yml](defaults/main.yml). The installation role invokes this role
automatically for HCP. To refresh an existing deployment, initialize the provider,
discover its bastion inventory and restore SSH configuration, then include:

```yaml
- name: Renew temporary administrative access
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.host_ocp4_aro_hcp_credentials
```

The role generates an RSA-2048 private key and CSR locally on the bastion and
requests a certificate using Azure's pinned HCP API. Azure supplies a kubeconfig
and expiration timestamp; the private key is inserted locally and never sent to
Azure. The certificate normally expires after 24 hours. This is administrative
CLI access, not an OIDC identity or a kubeadmin password.

The kubeconfig is saved to `/home/<ansible_user>/<cluster_name>/auth/kubeconfig`
for shared v2 workloads and the login user's `.kube/config`, optionally copied
to the existing student account, and exported to
`<output_dir>/<config>_<guid>_kubeconfig`. Files have mode 0600, directories 0700.
Expiry is recorded in `<config>_<guid>_hcp_credentials.json` and user data.
Refresh replaces all these copies. Treat the kubeconfig as a secret and refresh
it before running later workloads that require access. No automatic background
renewal or regular user/console OIDC authentication is configured.

TLS verification remains enabled and API readiness is checked. Shared controller
API, console and ingress facts are published without inventing a password.
Temporary key/CSR files and Azure CLI session state are removed even on failure.
