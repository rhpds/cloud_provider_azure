# installer_platform

Render Azure-specific YAML fragments for the shared self-managed OpenShift IPI
installer. This keeps Azure platform fields in the cloud collection while v2
retains the common `install-config.yaml` template. Rendering is local and makes
no Azure calls.

## Inputs

Include this role in the same task context as the shared installer, after
`initialize` has run on localhost. [defaults/main.yml](defaults/main.yml)
documents the normalized controller `azure_settings`, `project_tag`, optional
`cloud_tags_final`, and the IPI VM size inputs:

```yaml
control_plane_instance_type: Standard_D8s_v5
worker_instance_type: Standard_D4s_v5
```

These size inputs apply to IPI. ARO reads `aro_master_vm_size` and
`aro_worker_vm_size` instead. The common v2 config owns node counts, installer
version, pull secret, SSH key, cluster name and base DNS domain.

The platform fragment reads region, existing DNS-zone resource group and owned
cluster resource group from `hostvars.localhost.azure_settings`. Optional
`cloud_tags_final` is also read from localhost. The ownership tag
`agnosticd_project: <project_tag>` is always applied after user tags and therefore
cannot be overridden through that mapping.

## Outputs and use

```yaml
- name: Render provider-specific IPI platform fragments
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.installer_platform
```

The role sets three YAML **string** facts, not parsed mappings:

| Fact | Included YAML |
| --- | --- |
| `host_ocp4_installer_platform_fragment` | `azure` region, sandbox DNS group, cluster group and userTags |
| `host_ocp4_installer_control_plane_platform_fragment` | `azure.type` for control-plane nodes |
| `host_ocp4_installer_worker_platform_fragment` | `azure.type` for workers |

The shared installer inserts these into their corresponding `platform` sections.
They are recomputed on every render, and no installer secrets are embedded here.
Call `installer_prepare` separately before installation to establish the owned
cluster group and `installer_credentials` on the bastion to write its secret
credential file. The shared installer is responsible for final configuration
validation and IPI creation/destruction.
