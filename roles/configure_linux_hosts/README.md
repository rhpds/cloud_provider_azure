# configure_linux_hosts

Wait for SSH on an inventoried Linux bastion, then optionally install environment
authorized keys using the shared v2 `host_ssh_authorized_keys` role. This role
does not install packages, register RHEL, create a student user or deploy a cluster.

## Inputs and execution

Run against `hosts: bastions` after `create_inventory` and the shared SSH config
generator. Inventory must supply the bastion login account and SSH configuration
or provision private key. The connection wait has a 600-second timeout.

[defaults/main.yml](defaults/main.yml) documents the only role option,
`host_ssh_authorized_keys`. It defaults to an empty list, which skips the shared
key role. Its entries follow the shared role's schema:

```yaml
host_ssh_authorized_keys:
- key: 'ssh-rsa <public-key-data>'
  key_options: no-port-forwarding
```

These are public authorized keys. Preserve the provision key needed by the
controller and follow the shared role's options when choosing a target account.
The shared role and any privileges it requires must be supplied by v2.

```yaml
- name: Wait for and configure bastion access
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.configure_linux_hosts
```

Successful reruns wait for connectivity again and let the shared authorized-key
role reconcile keys idempotently. The Azure collection publishes no additional
facts or files here. Ansible's inventory/connection context is not redefined in
defaults.
