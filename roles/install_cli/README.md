# install_cli

Install a pinned Azure CLI RPM on a RHEL-compatible host. Run directly on the
bastion or inherit delegation from the ARO dispatcher. This role changes local
package/repository configuration but does not log into Azure or create resources.

## Variables and privileges

[defaults/main.yml](defaults/main.yml) documents `azure_cli_version` (default
`2.90.0`) and the internal registered setup result `install_cli_host_setup`.
The version is an RPM version without a release/architecture suffix. It must be
published for the selected OS in Microsoft's repository. The controller execution
environment has its own CLI installation; update that pin separately when needed.

The target must support `become: true` for signing-key import, repository changes
and DNF installation. Network access to Microsoft's package/key endpoints is
required. RedHat-family OS major versions 8, 9 and 10 are accepted; other systems
fail with a clear distribution requirement before repository changes.

## Repository selection and delegation

The role gathers minimal distribution facts on the actual target. Repository
selection reads the registered setup result, not `ansible_facts` on the controller,
so it also works when called through a delegated `include_role` block.

It configures repository `azure-cli` at
`https://packages.microsoft.com/rhel/<major>/prod/`. RHEL 8/9 use
`https://packages.microsoft.com/keys/microsoft.asc`; RHEL 10 uses
`https://packages.microsoft.com/keys/microsoft-2025.asc`. Package and repository
GPG checks remain enabled. Reusing the repository name replaces the old
`yumrepos/azure-cli` URL on reruns and DNF metadata is refreshed before install.

```yaml
- name: Install the Azure CLI on the current bastion
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.install_cli
```

For delegation, put `delegate_to` on an enclosing block rather than directly on
`include_role`. The package task uses `state: present` with the exact package
version; it is not an unconditional upgrade to whatever CLI is newest. The only
exported intermediate value is the setup result, not Azure credentials.
