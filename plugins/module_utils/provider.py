"""Azure CLI provider primitives. No SDK or ambient credential dependency."""
import json
import ipaddress
import os
import re
import subprocess
import tempfile
import time


class AzureError(Exception):
    pass


def owned(group, project):
    return group.get('tags', {}).get('agnosticd_project') == project


def validate_names(settings, sandbox_group):
    groups = [settings[k] for k in ('infra_group', 'cluster_group')]
    groups.append(settings['cluster_group'] + '-managed')
    if len(set(g.lower() for g in groups + [sandbox_group])) != 4:
        raise AzureError('Sandbox, infrastructure and cluster resource groups must be distinct')
    for name in groups + [settings['cluster_name']]:
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
            raise AzureError('Invalid deployment resource name')


class Azure:
    def __init__(self, auth, config_dir, project, runner=None):
        self.auth, self.project = auth, project
        self.env = dict(os.environ, AZURE_CONFIG_DIR=config_dir,
                        AZURE_EXTENSION_USE_DYNAMIC_INSTALL='no')
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        os.chmod(config_dir, 0o700)
        self.runner = runner or subprocess.run
        self.changed = False

    def call(self, *args, missing=False):
        cmd = ['az'] + [str(a) for a in args]
        if args[:1] != ('login',) and args[:1] != ('version',):
            cmd += ['--subscription', self.auth['subscription_id']]
        cmd += ['--only-show-errors', '--output', 'json']
        result = self.runner(cmd, env=self.env, text=True, capture_output=True)
        if result.returncode:
            # Never confuse forbidden, bad credentials or network failures with absence.
            if missing and any(x in result.stderr for x in
                               ('ResourceNotFound', 'ResourceGroupNotFound', 'ParentResourceNotFound',
                                'DeploymentNotFound')):
                return None
            message = result.stderr
            for key in ('password', 'app_id'):
                message = message.replace(self.auth[key], '<redacted>')
            raise AzureError(message)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def login(self):
        self.call('login', '--service-principal', '--username', self.auth['app_id'],
                  '--password=' + self.auth['password'], '--tenant', self.auth['tenant'])

    def group(self, name, region, create=False):
        group = self.call('group', 'show', '--name', name, missing=True)
        if group and not owned(group, self.project):
            raise AzureError('Refusing unowned resource group: ' + name)
        if group is None and create:
            group = self.call('group', 'create', '--name', name, '--location', region,
                              '--tags', 'agnosticd_project=' + self.project)
            self.changed = True
        return group

    def delete_group(self, name, region):
        if self.group(name, region):
            self.call('group', 'delete', '--name', name, '--yes')
            self.changed = True

    def wait_cluster(self, settings):
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            cluster = self.call('aro', 'show', '-g', settings['cluster_group'],
                                '-n', settings['cluster_name'])
            state = cluster['provisioningState']
            if state == 'Succeeded':
                return cluster
            if state not in ('Creating', 'Updating', 'AdminUpdating'):
                raise AzureError('ARO cluster state is ' + state + '; inspect Azure diagnostics before retrying')
            time.sleep(30)
        raise AzureError('Timed out waiting for ARO provisioning')

    def preflight(self, s):
        for provider in ('Microsoft.Compute', 'Microsoft.Storage', 'Microsoft.Network',
                         'Microsoft.Authorization', 'Microsoft.ManagedIdentity', 'Microsoft.RedHatOpenShift'):
            info = self.call('provider', 'show', '--namespace', provider)
            if info['registrationState'] != 'Registered':
                self.call('provider', 'register', '--namespace', provider, '--wait')
                self.changed = True
        versions = self.call('aro', 'get-versions', '--location', s['region'])
        if s['version'] not in versions:
            raise AzureError('ARO version is not available in this region: ' + s['version'])
        skus = self.call('vm', 'list-skus', '--location', s['region'], '--resource-type', 'virtualMachines', '--all')
        requirements = {}
        for size, count in ((s['master_size'], 4), (s['worker_size'], s['worker_count'])):
            matches = [x for x in skus if x['name'] == size and not x.get('restrictions')]
            if not matches:
                raise AzureError('Unavailable VM SKU: ' + size)
            sku = matches[0]
            cores = int(next(x['value'] for x in sku['capabilities'] if x['name'] == 'vCPUs'))
            requirements[sku['family'].lower()] = requirements.get(sku['family'].lower(), 0) + cores * count
        usage = self.call('vm', 'list-usage', '--location', s['region'])
        requirements['cores'] = sum(requirements.values())
        for name, needed in requirements.items():
            quota = next((x for x in usage if x['name']['value'].lower() == name.lower()), None)
            if quota is None or quota['limit'] - quota['currentValue'] < needed:
                raise AzureError('Insufficient or unavailable quota: ' + name)

    def aro_install(self, s):
        networks = {k: ipaddress.ip_network(s[k]) for k in
                    ('vnet_cidr', 'master_cidr', 'worker_cidr', 'pod_cidr', 'service_cidr')}
        if any(n.version != 4 for n in networks.values()):
            raise AzureError('ARO requires IPv4 networks')
        if not all(networks[k].subnet_of(networks['vnet_cidr']) for k in ('master_cidr', 'worker_cidr')):
            raise AzureError('ARO subnets must belong to its VNet')
        if networks['master_cidr'].overlaps(networks['worker_cidr']):
            raise AzureError('ARO control-plane and worker subnets must not overlap')
        for left, right in (('vnet_cidr', 'pod_cidr'), ('vnet_cidr', 'service_cidr'), ('pod_cidr', 'service_cidr')):
            if networks[left].overlaps(networks[right]):
                raise AzureError('ARO machine, pod and service networks must not overlap')
        self.group(s['cluster_group'], s['region'], create=True)
        cluster = self.call('aro', 'show', '-g', s['cluster_group'], '-n', s['cluster_name'], missing=True)
        if cluster is None:
            self.preflight(s)
            self.call('network', 'vnet', 'create', '-g', s['cluster_group'], '-n', 'aro-vnet',
                      '--address-prefixes', s['vnet_cidr'])
            for name, cidr in (('master', s['master_cidr']), ('worker', s['worker_cidr'])):
                self.call('network', 'vnet', 'subnet', 'create', '-g', s['cluster_group'],
                          '--vnet-name', 'aro-vnet', '-n', name, '--address-prefixes', cidr,
                          '--disable-private-link-service-network-policies', 'true')
            args = ['aro', 'create', '-g', s['cluster_group'], '-n', s['cluster_name'],
                    '--location', s['region'], '--vnet', 'aro-vnet', '--vnet-resource-group', s['cluster_group'],
                    '--cluster-resource-group', s['cluster_group'] + '-managed', '--master-subnet', 'master',
                    '--worker-subnet', 'worker', '--version', s['version'],
                    '--master-vm-size', s['master_size'], '--worker-vm-size', s['worker_size'],
                    '--worker-count', s['worker_count'], '--worker-vm-disk-size-gb', s['worker_disk'],
                    '--pod-cidr', s['pod_cidr'], '--service-cidr', s['service_cidr'],
                    '--tags', 'agnosticd_project=' + self.project, '--no-wait']
            if s['identity_mode'] == 'managed_identity':
                args += ['--enable-managed-identity', 'true']
            else:
                args += ['--client-id', self.auth['app_id'], '--client-secret', self.auth['password']]
            with tempfile.TemporaryDirectory() as tmp:
                if s.get('pull_secret'):
                    secret = os.path.join(tmp, 'pull-secret')
                    with open(secret, 'w') as stream:
                        stream.write(json.dumps(s['pull_secret']) if isinstance(s['pull_secret'], dict) else s['pull_secret'])
                    os.chmod(secret, 0o600)
                    args += ['--pull-secret', secret]
                self.call(*args)
            self.changed = True
        cluster = self.wait_cluster(s)
        credentials = self.call('aro', 'list-credentials', '-g', s['cluster_group'], '-n', s['cluster_name'])
        return dict(cluster=cluster, credentials=credentials)

    def aro_destroy(self, s):
        if self.group(s['cluster_group'], s['region']):
            cluster = self.call('aro', 'show', '-g', s['cluster_group'], '-n', s['cluster_name'], missing=True)
            if cluster:
                self.call('aro', 'delete', '-g', s['cluster_group'], '-n', s['cluster_name'], '--yes')
                self.changed = True
            # All created identities and network-scoped role assignments live in this owned group.
            self.delete_group(s['cluster_group'], s['region'])

    def inventory(self, s):
        if not self.group(s['infra_group'], s['region']):
            return []
        vms = self.call('vm', 'list', '-g', s['infra_group'], '--show-details')
        return [vm for vm in vms if owned(vm, self.project)]

    def lifecycle(self, s, action):
        vms = self.inventory(s)
        if s['method'] == 'openshift_install' and self.group(s['cluster_group'], s['region']):
            vms += self.call('vm', 'list', '-g', s['cluster_group'], '--show-details')
        if action in ('start', 'stop'):
            for vm in vms:
                self.call('vm', 'start' if action == 'start' else 'deallocate', '--ids', vm['id'])
                self.changed = True
        cluster = None
        if s['method'] == 'aro' and self.group(s['cluster_group'], s['region']):
            cluster = self.call('aro', 'show', '-g', s['cluster_group'], '-n', s['cluster_name'], missing=True)
        return dict(vms=vms, cluster=cluster)
