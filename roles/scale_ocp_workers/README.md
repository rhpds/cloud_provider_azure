# scale_ocp_workers

Add or scale worker nodes of an existing self-managed OpenShift IPI cluster on
Azure (not ARO or HCP) by managing MachineSets. It runs against the cluster API
with `kubernetes.core` and takes the same variables as the AWS
`scale_ocp_workers` role. Run it on the controller after the cluster is
installed with zero workers.

## How it works

The IPI installer creates one worker MachineSet per zone even with zero compute
replicas. The role:

1. Scales those MachineSets round-robin (5 workers over 3 zones = 2, 2, 1) when
   `worker_instance_type` is empty or equals their `vmSize`.
2. Otherwise copies each worker MachineSet as `<name>-<worker_machineset_suffix>`
   and changes only the name, `vmSize` and replicas, so image, subnet, resource
   group and managed identity stay as the installer set them. Re-running reuses
   the copies.
3. If the cluster has no worker MachineSet, builds one per control-plane zone
   from a control-plane Machine providerSpec (worker user data, no internal load
   balancer; `publicLoadBalancer` is kept for outbound access). This requires
   `worker_instance_type`.
4. Waits until `worker_instance_count` worker nodes are Ready.
5. With `worker_only: true`, removes the worker label from control-plane nodes,
   sets `mastersSchedulable: false` and drains them.

## Usage

```yaml
- name: Scale OCP workers
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.scale_ocp_workers
  vars:
    worker_instance_count: 3
    worker_instance_type: Standard_D8as_v5
    worker_machineset_suffix: compute
```

Variables and defaults are documented in [defaults/main.yml](defaults/main.yml).
Scaling down is done by setting a lower `worker_instance_count`; MachineSets
created for another VM size are not removed.
