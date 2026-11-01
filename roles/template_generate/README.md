# template_generate

Render the ARM template for a Linux bastion and its dedicated network resources.
Rendering happens on the controller and makes no Azure API calls. Deployment is
performed separately by `template_create`.

## Inputs and defaults

[defaults/main.yml](defaults/main.yml) documents every variable, including the
required `guid`, `azure_region`, `project_tag` and actual public-key text in
`ssh_provision_pubkey_content`. Run `initialize` and the v2 provision-key and
authorized-key roles first. Never put a private key or a key filename in the
public-key input.

The default bastion uses `azure_bastion_user: azure`, `Standard_D2s_v5`, and the
PAYG RHEL 9 generation-2 LVM image. The rendered template also defines:

- The owned `bastion-<guid>` VM and its `agnosticd_project` tag.
- A 64 GiB `Premium_LRS` OS disk.
- A `10.240.0.0/24` VNet/subnet, NIC and Standard static public IP.
- An NSG permitting inbound TCP/22 from `azure_bastion_ssh_source` (default `*`).
- Public-key SSH with password authentication disabled.

VM disk size and bastion subnet are currently template constants. They are not
configurable through the ARO worker-disk or cluster-network variables.

Override the complete image mapping for a new RHEL 10 deployment:

```yaml
azure_bastion_image:
  publisher: RedHat
  offer: RHEL
  sku: 10-lvm-gen2
  version: latest
```

`latest` is resolved at VM creation. Marketplace image/SKU availability and
subscription policies must permit the selected region and image. Changing the
rendered image does not cause `template_create` to reimage a successful bastion.
Use a new deployment when a different image is required.

## Output and example

The generated `azure_arm_template` fact is a mapping consumed by `template_create`.
It contains the SSH public key, not private credentials.

```yaml
- name: Render the bastion template after provider and SSH-key initialization
  ansible.builtin.include_role:
    name: agnosticd.cloud_provider_azure.template_generate
```

Repeated rendering recomputes the fact without changing Azure. A standalone
caller can now use the documented defaults instead of relying on bastion values
being loaded by the v2 cloud-provider playbook.
