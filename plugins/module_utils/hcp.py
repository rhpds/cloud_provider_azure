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

API = '2026-10-01'


def check_region(azure, region):
    """Read HCP-specific subscription availability without registering or creating resources."""
    provider = azure.call('provider', 'show', '--namespace', 'Microsoft.RedHatOpenShift')
    types = provider.get('resourceTypes', []) if isinstance(provider, dict) else []
    resource = next((item for item in (types if isinstance(types, list) else [])
                     if isinstance(item, dict) and str(item.get('resourceType', '')).lower() == 'hcpopenshiftclusters'), {})
    locations = resource.get('locations')
    if not isinstance(locations, list) or not locations or any(not isinstance(x, str) or not x.strip() for x in locations):
        raise AzureError('Azure did not advertise HCP regions for this subscription; '
                         'check Microsoft.RedHatOpenShift registration and access')
    regions = sorted({re.sub(r'[^a-z0-9]', '', location.lower()) for location in locations})
    if region not in regions:
        raise AzureError('ARO HCP is unavailable in region ' + region + '. '
                         'Supported HCP regions reported by Azure: ' + ', '.join(regions))
    return regions


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AzureError('Unexpected redirect from HCP ARM endpoint')


class HCP:
    def __init__(self, azure, settings):
        self.az, self.s = azure, settings
        self.resource_group_id = ('/subscriptions/' + azure.auth['subscription_id']
                                  + '/resourceGroups/' + settings['cluster_group'])
        self.root = 'https://management.azure.com' + self.resource_group_id
        self.url = self.root + '/providers/Microsoft.RedHatOpenShift/hcpOpenShiftClusters/' + settings['cluster_name']
        self.vault = 'hcp-' + hashlib.sha256((azure.auth['subscription_id'] + '/' + azure.project).encode()).hexdigest()[:20]
        self.vault_id = self.resource_group_id + '/providers/Microsoft.KeyVault/vaults/' + self.vault
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
            # Node pool creation returns 201 with an Azure-AsyncOperation URL.
            if status != 202 and not (status == 201 and operation) and state not in ('InProgress', 'Running', 'Accepted'):
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
        if not re.fullmatch(r'[a-zA-Z]([-a-zA-Z0-9]{0,52}[a-zA-Z0-9])?', s['cluster_name']):
            raise AzureError('Invalid HCP cluster name')
        if not re.fullmatch(r'[a-zA-Z]([-a-zA-Z0-9]{0,13}[a-zA-Z0-9])?', s['hcp_nodepool']):
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
        check_region(self.az, s['region'])
        versions = self.rest('GET', 'https://management.azure.com/subscriptions/' + self.az.auth['subscription_id']
                             + '/providers/Microsoft.RedHatOpenShift/locations/' + s['region']
                             + '/hcpOpenShiftVersions?api-version=' + API)
        while not any(item.get('name') == s['version']
                      and item.get('properties', {}).get('enabled', True)
                      and item.get('properties', {}).get('channelGroup') == 'stable'
                      for item in versions.get('value', [])):
            if not versions.get('nextLink'):
                raise AzureError('Requested HCP version is unavailable in the selected region',
                                 {'kind': 'version', 'version': s['version']})
            versions = self.rest('GET', versions['nextLink'])
        skus = self.az.call('vm', 'list-skus', '--location', s['region'], '--resource-type', 'virtualMachines', '--all')
        sku = next((x for x in skus if x['name'] == s['worker_size'] and not x.get('restrictions')), None)
        zone = s['hcp_zone']
        if not sku or (zone and not any(zone in info.get('zones', []) for info in sku.get('locationInfo', []))):
            raise AzureError('HCP worker SKU or availability zone is unavailable',
                             {'kind': 'sku', 'setting': s['worker_size'],
                              'reasons': ['SKU or logical availability zone ' + zone + ' is unavailable'
                                          if zone else 'SKU is unavailable']})
        self.physical_zone = None
        if zone:
            locations = self.az.call('account', 'list-locations')
            mapping = next((loc.get('availabilityZoneMappings', []) for loc in locations if loc['name'] == s['region']), [])
            self.physical_zone = next((item['physicalZone'] for item in mapping if item['logicalZone'] == zone), None)
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
        if not s['version'].startswith('5.'):
            raise AzureError('HCP provisioning requires an exact OpenShift 5 release',
                             {'kind': 'version', 'version': s['version']})
        self.az.group(s['cluster_group'], s['region'])
        cluster = self.show()
        new_cluster = cluster is None
        cluster_version = '.'.join(s['version'].split('.')[:2])
        if new_cluster:
            if self.az.call('group', 'show', '--name', s['cluster_group'] + '-managed', missing=True):
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
                          self.vault_id.lower()):
                raise AzureError('Refusing an unowned HCP encryption vault')
            # Only a matching trusted archive permits recovery of this deterministic vault.
            deleted = self.az.call('keyvault', 'list-deleted')
            for vault in deleted:
                if vault['name'] == self.vault:
                    expected = self.vault_id
                    if not s.get('hcp_recover_vault') or vault['properties']['vaultId'].lower() != expected.lower():
                        raise AzureError('Refusing recovery of a vault without matching archived ownership')
                    self.az.call('keyvault', 'recover', '--name', self.vault, '--resource-group', s['cluster_group'])
            parameters = dict(customerNsgName='hcp-nsg', customerVnetName='hcp-vnet', customerVnetSubnetName='workers',
                              customerVirtualNetworkIntegrationSubnetName='integration', clusterName=s['cluster_name'],
                              managedResourceGroupName=s['cluster_group'] + '-managed', clusterVersion=cluster_version,
                              customerKeyVaultName=self.vault, projectTag=self.az.project, addressPrefix=s['vnet_cidr'],
                              subnetPrefix=s['worker_cidr'], virtualNetworkIntegrationSubnetPrefix=s['hcp_integration_cidr'],
                              podCidr=s['pod_cidr'], serviceCidr=s['service_cidr'])
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
        elif not owned(cluster, self.az.project):
            raise AzureError('Refusing unowned HCP cluster')
        elif cluster.get('properties', {}).get('provisioningState') in ('Failed', 'Canceled'):
            raise AzureError('Existing HCP cluster failed; inspect and destroy explicitly')

        # A retry with an existing control plane resumes at the worker pool.
        if new_cluster or cluster.get('properties', {}).get('provisioningState') != 'Succeeded':
            cluster = self.wait()
        if not owned(cluster, self.az.project):
            raise AzureError('Refusing unowned HCP cluster')

        suffix = '/nodePools/' + s['hcp_nodepool']
        pool = self.show(suffix)
        if not pool:
            properties = cluster['properties']
            if properties.get('version', {}).get('id') != cluster_version:
                raise AzureError('Existing HCP control plane version differs from the requested minor release')
            subnet = properties.get('platform', {}).get('subnetId')
            if not subnet:
                raise AzureError('HCP control plane did not return a worker subnet ID')
            if not new_cluster:
                self.preflight()
            platform = dict(subnetId=subnet, vmSize=s['worker_size'],
                            osDisk={'sizeGiB': s['worker_disk'], 'diskStorageAccountType': 'Premium_LRS'})
            if self.physical_zone:
                platform['availabilityZone'] = self.physical_zone
            body = dict(location=s['region'], tags={'agnosticd_project': self.az.project},
                        properties=dict(version={'id': s['version'], 'channelGroup': 'stable'},
                                        platform=platform, replicas=s['worker_count'], autoRepair=True))
            self.rest('PUT', self.url + suffix + '?api-version=' + API, body)
            self.az.changed = True
        elif not owned(pool, self.az.project):
            raise AzureError('Refusing unowned HCP node pool')

        if not pool or pool.get('properties', {}).get('provisioningState') != 'Succeeded':
            pool = self.wait(suffix)
        if not owned(pool, self.az.project):
            raise AzureError('Refusing unowned HCP node pool')
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
        result['kubeconfig'] = result['kubeconfig'].replace('\\n', '\n')
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
                # Cluster deletion cascades to its pools. Deleting the last pool
                # separately is forbidden, including when pool provisioning failed.
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
