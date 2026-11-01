# lifecycle

Start, stop or report status for an existing Azure deployment. Run on the
controller with the original deployment variables and restored output archive.
The role includes `initialize` itself, then performs the selected lifecycle action
and writes `<output_dir>/status.txt` with mode `0600`.

## Action selection and variables

All inputs and registered results are documented in
[defaults/main.yml](defaults/main.yml). The credential and ownership inputs of
[initialize](../initialize/README.md) are also required. Use the v2 run's `ACTION`
for ordinary lifecycle jobs:

```yaml
ACTION: status  # start, stop or status
```

`azure_lifecycle_action` defaults to `ACTION`. During destroy preparation use an
explicit override so the bastion starts without changing the global destroy action:

```yaml
- name: Start machines needed for cleanup
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.lifecycle
  vars:
    azure_lifecycle_action: start
```

The run remains `ACTION: destroy`. Do not set `ACTION: start` on the include:
facts or command-line extra variables may take precedence, causing the wrong
action. The role accepts only `start`, `stop` and `status`; resource deletion
belongs to the explicit cluster and infrastructure destruction roles.

## Resources affected

For ARO, only deployment-owned bastion VMs are started/deallocated. The ARO API
is queried for cluster status; Azure-managed cluster VMs are never powered by
this collection. For self-managed IPI, start/stop also operates on the VMs in
the owned cluster resource group. Keep those owned groups deployment-specific.

`status` performs no power operation. An absent owned group yields no VMs, and
an absent ARO resource yields a null cluster. Ownership/authentication failures
remain errors. Start/stop submit the requested operation to each VM and report
changed when submitted; they do not first compare each VM's current power state.

`azure_lifecycle_result` and `status.txt` contain the module's `changed`, `vms`
and `cluster` fields. The VM list is discovered before the power operation, so
it is not a post-operation power-state verification. This role does not collect
ARO kubeadmin credentials or export a new kubeconfig.
