"""ARO hosted control planes, using the pinned ARM REST API and owned templates."""
import hashlib
import ipaddress
import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ansible_collections.agnosticd.cloud_provider_azure.plugins.module_utils.provider import AzureError, owned

API = '2026-09-01-preview'
REGIONS = ('australiaeast', 'brazilsouth', 'canadacentral', 'centralindia', 'eastus2',
           'switzerlandnorth', 'uksouth', 'westeurope')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AzureError('Unexpected redirect from HCP ARM endpoint')


class HCP:
    def __init__(self, azure, settings):
        self.az, self.s = azure, settings
        self.root = ('https://management.azure.com/subscriptions/' + azure.auth['subscription_id']
                     + '/resourceGroups/' + settings['cluster_group'])
        self.url = self.root + '/providers/Microsoft.RedHatOpenShift/hcpOpenShiftClusters/' + settings['cluster_name']
        self.vault = 'hcp-' + hashlib.sha256((azure.auth['subscription_id'] + '/' + azure.project).encode()).hexdigest()[:20]
        if settings.get('hcp_vault_name') and settings['hcp_vault_name'] != self.vault:
            raise AzureError('HCP vault identity differs from deterministic ownership')

    def rest(self, method, url, body=None, missing=False):
        """Retain response headers for ARM asynchronous operations; never log tokens."""
        if not url.startswith('https://management.azure.com/'):
            raise AzureError('Refusing an ARM operation URL outside management.azure.com')
        deadline = time.monotonic() + 7200
        failures = 0
        result_url = None
        while True:
            if not url.startswith('https://management.azure.com/'):
                raise AzureError('Refusing an ARM operation URL outside management.azure.com')
            token = self.az.call('account', 'get-access-token', '--resource', 'https://management.azure.com/')['accessToken']
            request = urllib.request.Request(url, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                    content = response.read()
                    result = json.loads(content) if content else {}
                    status, headers = response.status, response.headers
            except urllib.error.HTTPError as exc:
                if missing and exc.code == 404:
                    return None
                if exc.code in (403, 429, 500, 502, 503, 504) and failures < 5 and time.monotonic() < deadline:
                    failures += 1
                    time.sleep(20)
                    continue
                raise AzureError('HCP ARM request failed with HTTP ' + str(exc.code)) from None
            state = result.get('status')
            if state in ('Failed', 'Canceled'):
                error = result.get('error') or {}
                raise AzureError('HCP operation failed: ' + str(error.get('code', state)))
            operation = headers.get('Azure-AsyncOperation') or headers.get('Location')
            result_url = headers.get('Location') or result_url
            if status != 202 and state not in ('InProgress', 'Running', 'Accepted'):
                if state == 'Succeeded' and result_url and result_url != url:
                    url, method, body = result_url, 'GET', None
                    continue
                return result
            if time.monotonic() >= deadline:
                raise AzureError('Timed out waiting for HCP ARM operation')
            if operation:
                url = operation
            elif method != 'GET':
                raise AzureError('HCP asynchronous response did not include an operation URL')
            method, body = 'GET', None
            time.sleep(min(int(headers.get('Retry-After', 10)), 60))

    def show(self, suffix=''):
        return self.rest('GET', self.url + suffix + '?api-version=' + API, missing=True)

    def validate(self):
        s = self.s
        if s['method'] != 'aro' or s['identity_mode'] != 'managed_identity':
            raise AzureError('HCP requires installation method aro and managed_identity')
        if s['region'] not in REGIONS:
            raise AzureError('ARO HCP preview is unavailable in region ' + s['region'] + '; choose a supported HCP region')
        if not re.fullmatch(r'[a-zA-Z]([-a-zA-Z0-9]{0,52}[a-zA-Z0-9])?', s['cluster_name']):
            raise AzureError('Invalid HCP cluster name')
        if not re.fullmatch(r'[a-zA-Z]([-a-zA-Z0-9]{0,52}[a-zA-Z0-9])?', s['hcp_nodepool']):
            raise AzureError('Invalid HCP node pool name')
        if not re.fullmatch(r'\d+\.\d+\.\d+', s['version']) or s['worker_count'] < 2:
            raise AzureError('HCP requires an exact release and at least two workers')
        networks = {k: ipaddress.ip_network(s[k]) for k in
                    ('vnet_cidr', 'worker_cidr', 'hcp_integration_cidr', 'pod_cidr', 'service_cidr')}
        if any(n.version != 4 for n in networks.values()):
            raise AzureError('HCP requires IPv4 networks')
        for key in ('worker_cidr', 'hcp_integration_cidr'):
            if not networks[key].subnet_of(networks['vnet_cidr']):
                raise AzureError('HCP subnets must belong to the machine VNet')
        if networks['hcp_integration_cidr'].prefixlen > 28 or networks['vnet_cidr'].prefixlen > 25:
            raise AzureError('HCP requires at least /28 integration and /25 machine CIDRs')
        for a, b in (('worker_cidr', 'hcp_integration_cidr'), ('vnet_cidr', 'pod_cidr'),
                     ('vnet_cidr', 'service_cidr'), ('pod_cidr', 'service_cidr')):
            if networks[a].overlaps(networks[b]):
                raise AzureError('HCP networks overlap: ' + a + '/' + b)

    def preflight(self):
        self.validate()
        s = self.s
        versions = self.rest('GET', 'https://management.azure.com/subscriptions/' + self.az.auth['subscription_id']
                             + '/providers/Microsoft.RedHatOpenShift/locations/' + s['region']
                             + '/hcpOpenShiftVersions?api-version=' + API)
        def version_strings(value):
            if isinstance(value, str):
                yield value.rsplit('/', 1)[-1]
            elif isinstance(value, dict):
                for item in value.values():
                    yield from version_strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from version_strings(item)
        available = set(version_strings(versions))
        while versions.get('nextLink'):
            versions = self.rest('GET', versions['nextLink'])
            available.update(version_strings(versions))
        if s['version'] not in available:
            raise AzureError('Requested HCP version is unavailable in the selected region',
                             {'kind': 'version', 'version': s['version']})
        skus = self.az.call('vm', 'list-skus', '--location', s['region'], '--resource-type', 'virtualMachines', '--all')
        sku = next((x for x in skus if x['name'] == s['worker_size'] and not x.get('restrictions')), None)
        if not sku or not any(s['hcp_zone'] in info.get('zones', []) for info in sku.get('locationInfo', [])):
            raise AzureError('HCP worker SKU or availability zone is unavailable',
                             {'kind': 'sku', 'setting': s['worker_size'],
                              'reasons': ['SKU or logical availability zone ' + s['hcp_zone'] + ' is unavailable']})
        locations = self.az.call('account', 'list-locations')
        mapping = next((loc.get('availabilityZoneMappings', []) for loc in locations if loc['name'] == s['region']), [])
        self.physical_zone = next((zone['physicalZone'] for zone in mapping if zone['logicalZone'] == s['hcp_zone']), None)
        if not self.physical_zone:
            raise AzureError('Could not resolve HCP logical zone to its physical availability zone')
        cores = int(next(x['value'] for x in sku['capabilities'] if x['name'] == 'vCPUs'))
        required = max(20, cores * (s['worker_count'] + 1))
        usage = self.az.call('vm', 'list-usage', '--location', s['region'])
        for family in ('cores', sku['family'].lower()):
            quota = next((q for q in usage if q['name']['value'].lower() == family), None)
            if not quota or int(quota['limit']) - int(quota['currentValue']) < required:
                raise AzureError('Insufficient HCP core quota: ' + family,
                                 {'kind': 'quota', 'family': family, 'required': required,
                                  'available': int(quota['limit']) - int(quota['currentValue']) if quota else 0})

    def wait(self, suffix=''):
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            result = self.show(suffix)
            state = (result or {}).get('properties', {}).get('provisioningState')
            if state == 'Succeeded':
                return result
            if state in ('Failed', 'Canceled'):
                raise AzureError('HCP resource state is ' + state + '; destroy explicitly before recreating')
            time.sleep(30)
        raise AzureError('Timed out waiting for HCP cluster/node pool')

    def install(self, template):
        self.validate()
        s = self.s
        self.az.group(s['cluster_group'], s['region'])
        current = self.show()
        if current and not owned(current, self.az.project):
            raise AzureError('Refusing unowned HCP cluster')
        if current and current.get('properties', {}).get('provisioningState') not in ('Succeeded', 'Failed', 'Canceled'):
            current = self.wait()
        pool = self.show('/nodePools/' + s['hcp_nodepool']) if current else None
        if pool and not owned(pool, self.az.project):
            raise AzureError('Refusing unowned HCP node pool')
        for resource in (current, pool):
            if resource and resource.get('properties', {}).get('provisioningState') in ('Failed', 'Canceled'):
                raise AzureError('Existing HCP resource failed; inspect and destroy explicitly')
        if not current or not pool:
            if not current and self.az.call('group', 'show', '--name', s['cluster_group'] + '-managed', missing=True):
                raise AzureError('HCP managed resource group already exists; complete previous destruction first')
            for namespace in ('Microsoft.RedHatOpenShift', 'Microsoft.Compute', 'Microsoft.Network', 'Microsoft.Storage',
                              'Microsoft.ManagedIdentity', 'Microsoft.Authorization', 'Microsoft.KeyVault'):
                info = self.az.call('provider', 'show', '--namespace', namespace)
                if info['registrationState'] != 'Registered':
                    self.az.call('provider', 'register', '--namespace', namespace, '--wait')
            self.preflight()
            self.az.group(s['cluster_group'], s['region'], create=True)
            vault = self.az.call('keyvault', 'show', '--name', self.vault,
                                 '--resource-group', s['cluster_group'], missing=True)
            if vault and (not owned(vault, self.az.project) or vault['id'].lower() !=
                          (self.root + '/providers/Microsoft.KeyVault/vaults/' + self.vault).lower()):
                raise AzureError('Refusing an unowned HCP encryption vault')
            # Only a matching trusted archive permits recovery of this deterministic vault.
            deleted = self.az.call('keyvault', 'list-deleted')
            for vault in deleted:
                if vault['name'] == self.vault:
                    expected = self.root + '/providers/Microsoft.KeyVault/vaults/' + self.vault
                    if not s.get('hcp_recover_vault') or vault['properties']['vaultId'].lower() != expected.lower():
                        raise AzureError('Refusing recovery of a vault without matching archived ownership')
                    self.az.call('keyvault', 'recover', '--name', self.vault, '--resource-group', s['cluster_group'])
            parameters = dict(customerNsgName='hcp-nsg', customerVnetName='hcp-vnet', customerVnetSubnetName='workers',
                customerVirtualNetworkIntegrationSubnetName='integration', clusterName=s['cluster_name'],
                managedResourceGroupName=s['cluster_group'] + '-managed', nodePoolName=s['hcp_nodepool'],
                clusterVersion='.'.join(s['version'].split('.')[:2]), nodePoolVersion=s['version'],
                customerKeyVaultName=self.vault, projectTag=self.az.project, addressPrefix=s['vnet_cidr'],
                subnetPrefix=s['worker_cidr'], virtualNetworkIntegrationSubnetPrefix=s['hcp_integration_cidr'],
                workerSize=s['worker_size'], workerCount=s['worker_count'], workerDisk=s['worker_disk'],
                workerZone=self.physical_zone, podCidr=s['pod_cidr'], serviceCidr=s['service_cidr'])
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'parameters.json'
                path.write_text(json.dumps({k: {'value': v} for k, v in parameters.items()}))
                for attempt in range(6):
                    try:
                        self.az.call('deployment', 'group', 'create', '-g', s['cluster_group'], '-n', 'aro-hcp',
                                     '--template-file', template, '--parameters', '@' + str(path))
                        break
                    except AzureError as exc:
                        if attempt == 5 or not any(code in str(exc) for code in
                                ('AuthorizationFailed', 'PrincipalNotFound', 'Forbidden')):
                            raise
                        resource = self.show()
                        if resource and resource.get('properties', {}).get('provisioningState') == 'Failed':
                            raise
                        time.sleep(30)
            self.az.changed = True
        cluster = self.wait()
        pool = self.wait('/nodePools/' + s['hcp_nodepool'])
        return dict(cluster=cluster, nodepool=pool)

    def credentials(self, csr):
        self.az.group(self.s['cluster_group'], self.s['region'])
        cluster = self.show()
        if not cluster:
            raise AzureError('Cannot request HCP credentials before the cluster exists')
        if not owned(cluster, self.az.project):
            raise AzureError('Refusing credentials for an unowned HCP cluster')
        result = self.rest('POST', self.url + '/requestAdminCredential?api-version=' + API,
                           {'certificateSigningRequest': csr})
        if not result.get('kubeconfig') or not result.get('expirationTimestamp'):
            raise AzureError('HCP credential operation did not return a kubeconfig and expiry')
        return result

    def destroy(self):
        s = self.s
        if not self.az.group(s['cluster_group'], s['region']):
            return
        cluster = self.show()
        if cluster:
            if not owned(cluster, self.az.project):
                raise AzureError('Refusing unowned HCP cluster')
            if cluster.get('properties', {}).get('provisioningState') != 'Deleting':
                pools = self.rest('GET', self.url + '/nodePools?api-version=' + API, missing=True) or {}
                for pool in pools.get('value', []):
                    self.rest('DELETE', self.url + '/nodePools/' + pool['name'].split('/')[-1]
                              + '?api-version=' + API, missing=True)
                self.rest('DELETE', self.url + '?api-version=' + API, missing=True)
            deadline = time.monotonic() + 7200
            while self.show():
                if time.monotonic() >= deadline:
                    raise AzureError('Timed out deleting HCP; preserving prerequisites')
                time.sleep(20)
            self.az.changed = True
        managed = s['cluster_group'] + '-managed'
        deadline = time.monotonic() + 7200
        while self.az.call('group', 'show', '--name', managed, missing=True):
            if time.monotonic() >= deadline:
                raise AzureError('HCP managed resource group remains; preserving prerequisites')
            time.sleep(20)
        self.az.delete_group(s['cluster_group'], s['region'])
