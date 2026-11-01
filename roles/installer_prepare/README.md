# installer_prepare

Create or verify the owned resource group that the self-managed OpenShift IPI
installer will use. Run on the controller after `initialize`, before invoking
`openshift-install`. This role is for `openshift_install`; ARO prepares its own
cluster group through the ARO install operation.

## Inputs

[defaults/main.yml](defaults/main.yml) documents the normalized `azure_auth`,
`azure_settings`, `azure_cli_config_dir` facts and `project_tag`. Configure the
group through `azure_cluster_resource_group` on `initialize`, not by changing the
generated settings mapping. The group must differ from the sandbox, infrastructure
and ARO-managed group names. The controller needs Azure CLI and permission to
read/create the group.

```yaml
- name: Prepare the IPI installer resource group
  when: host_ocp4_deploy_installation_method == 'openshift_install'
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.installer_prepare
```

The absent group is created in the normalized region and tagged with
`agnosticd_project: <project_tag>`. A matching existing group is reused. An existing
unowned group is refused; the role never adopts the sandbox group.
Use this group only for the deployment. This role checks group ownership, not
whether someone has subsequently added unrelated contents to an owned group.

The shared installer later takes responsibility for the cluster resources and
their destruction. `installer_platform` passes the exact resource group name to
`install-config.yaml`. `installer_credentials` supplies the required service
principal file on the bastion. This role itself generates no credentials,
inventory, kubeconfig or installer configuration fragment.
