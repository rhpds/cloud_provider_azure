// Adapted from Microsoft Learn HCP reference (MIT): https://learn.microsoft.com/azure/openshift/howto-create-custom-hosted-cluster
@description('Network Security Group Name')
param customerNsgName string

@description('Virtual Network Name')
param customerVnetName string

@description('Subnet Name')
param customerVnetSubnetName string

@description('Virtual Network Integration Subnet Name')
param customerVirtualNetworkIntegrationSubnetName string

@description('Name of the cluster')
param clusterName string

@description('The cluster managed resource group name')
param managedResourceGroupName string

@description('The name of the node pool')
param nodePoolName string

@description('The OpenShift version for the cluster (X.Y format, e.g. 4.20)')
param clusterVersion string

@description('The OpenShift version for the node pool (X.Y.Z format, e.g. 4.20.8)')
param nodePoolVersion string

@description('Deploy ARO HCP with a private key vault')
param privateKeyVault bool = false

@description('API server visibility')
@allowed([
  'Public'
  'Private'
])
param apiVisibility string = 'Public'

@description('Default ingress type')
@allowed([
  'Public'
  'Private'
])
param ingressType string = 'Public'

@description('Cryptographic restrictions for kernel and userspace libraries (immutable after creation)')
@allowed(['None', 'FIPS'])
param cryptoRestrictions string = 'None'
param projectTag string
param workerSize string
param workerCount int
param workerDisk int
param workerZone string
param podCidr string
param serviceCidr string


var etcdEncryptionKeyName = 'etcd-data-kms-encryption-key'
var randomSuffix = toLower(uniqueString(clusterName))
param customerKeyVaultName string
param addressPrefix string
param subnetPrefix string
param virtualNetworkIntegrationSubnetPrefix string


// Network Security Group
resource customerNsg 'Microsoft.Network/networkSecurityGroups@2023-05-01' = {
  name: customerNsgName
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Virtual network with worker subnet and VNet integration subnet
resource customerVnet 'Microsoft.Network/virtualNetworks@2023-05-01' = {
  name: customerVnetName
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    subnets: [
      {
        name: customerVnetSubnetName
        properties: {
          addressPrefix: subnetPrefix
          networkSecurityGroup: {
            id: customerNsg.id
          }
        }
      }
      {
        name: customerVirtualNetworkIntegrationSubnetName
        properties: {
          addressPrefix: virtualNetworkIntegrationSubnetPrefix
          networkSecurityGroup: {
            id: customerNsg.id
          }
          delegations: [
            {
              name: 'aro-hcp-delegation'
              properties: {
                serviceName: 'Microsoft.RedHatOpenShift/hcpOpenShiftClusters'
              }
            }
          ]
        }
      }
    ]
  }
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2022-07-01' existing = {
  name: customerVnetSubnetName
  parent: customerVnet
}

resource vnetIntegrationSubnet 'Microsoft.Network/virtualNetworks/subnets@2022-07-01' existing = {
  name: customerVirtualNetworkIntegrationSubnetName
  parent: customerVnet
}

resource customerKeyVault 'Microsoft.KeyVault/vaults@2024-12-01-preview' = {
  name: customerKeyVaultName
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
  properties: {
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: false
    tenantId: subscription().tenantId
    publicNetworkAccess: privateKeyVault ? 'Disabled' : 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

resource etcdEncryptionKey 'Microsoft.KeyVault/vaults/keys@2024-12-01-preview' = {
  parent: customerKeyVault
  name: 'etcd-data-kms-encryption-key'
  properties: {
    kty: 'RSA'
    keySize: 2048
  }
}

resource privateEndpointDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (privateKeyVault) {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  properties: {}
  dependsOn: [
    privateEndpoint
  ]
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-09-01' = if (privateKeyVault) {
  name: 'kv-private-endpoint'
  properties: {
    privateLinkServiceConnections: [
      {
        name: 'kv-private-endpoint'
        properties: {
          privateLinkServiceId: customerKeyVault.id
          groupIds: ['vault']
        }
      }
    ]
    subnet: {
      id: customerVnet.properties.subnets[0].id
    }
  }
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource privateEndpointDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-09-01' = if (privateKeyVault) {
  name: 'kv-private-ep-dns-group'
  parent: privateEndpoint
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'config1'
        properties: {
          privateDnsZoneId: privateEndpointDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    privateDnsZoneVnetLink
  ]
}

resource privateDnsZoneVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (privateKeyVault) {
  name: uniqueString('kv-private-dns-zone-link')
  parent: privateEndpointDnsZone
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: customerVnet.id
    }
  }
}

//
// Control plane identities
//

// Reader
var readerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'acdd72a7-3385-48ef-bd42-f606fba81ae7'
)

//
// Cluster API Azure managed identity
//

resource clusterApiAzureMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-cluster-api-azure-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift with hosted control planes Cluster API Provider
var hcpClusterApiProviderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '88366f10-ed47-4cc0-9fab-c8a06148393e'
)

resource hcpClusterApiProviderRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, clusterApiAzureMi.id, hcpClusterApiProviderRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: clusterApiAzureMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: hcpClusterApiProviderRoleId
  }
}

resource serviceManagedIdentityReaderOnClusterApiAzureMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, clusterApiAzureMi.id)
  scope: clusterApiAzureMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// KMS managed identity
//

resource kmsMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-kms-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Key Vault Crypto User
var keyVaultCryptoUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '12338af0-0e69-4776-bea7-57ae8d297424'
)

resource keyVaultCryptoUserToKeyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, kmsMi.id, keyVaultCryptoUserRoleId, customerKeyVault.id)
  scope: customerKeyVault
  properties: {
    principalId: kmsMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultCryptoUserRoleId
  }
}

resource serviceManagedIdentityReaderOnKmsMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, kmsMi.id)
  scope: kmsMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Control plane operator managed identity
//

resource controlPlaneMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-control-plane-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift with hosted control planes Control Plane Operator
var hcpControlPlaneOperatorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'fc0c873f-45e9-4d0d-a7d1-585aab30c6ed'
)

resource hcpControlPlaneOperatorVnetRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, controlPlaneMi.id, hcpControlPlaneOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: controlPlaneMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: hcpControlPlaneOperatorRoleId
  }
}

resource hcpControlPlaneOperatorNsgRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, controlPlaneMi.id, hcpControlPlaneOperatorRoleId, customerNsg.id)
  scope: customerNsg
  properties: {
    principalId: controlPlaneMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: hcpControlPlaneOperatorRoleId
  }
}

resource serviceManagedIdentityReaderOnControlPlaneMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, controlPlaneMi.id)
  scope: controlPlaneMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Cloud controller manager managed identity
//

resource cloudControllerManagerMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-cloud-controller-manager-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift Cloud Controller Manager
var cloudControllerManagerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a1f96423-95ce-4224-ab27-4e3dc72facd4'
)

resource cloudControllerManagerRoleNsgAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, cloudControllerManagerMi.id, cloudControllerManagerRoleId, customerNsg.id)
  scope: customerNsg
  properties: {
    principalId: cloudControllerManagerMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cloudControllerManagerRoleId
  }
}

resource cloudControllerManagerRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, cloudControllerManagerMi.id, cloudControllerManagerRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: cloudControllerManagerMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cloudControllerManagerRoleId
  }
}

resource serviceManagedIdentityReaderOnCloudControllerManagerMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, cloudControllerManagerMi.id)
  scope: cloudControllerManagerMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Ingress managed identity
//

resource ingressMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-ingress-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift Cluster Ingress Operator
var ingressOperatorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0336e1d3-7a87-462b-b6db-342b63f7802c'
)

// Azure Red Hat OpenShift Image Registry Operator
var imageRegistryOperatorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '8b32b316-c2f5-4ddf-b05b-83dacd2d08b5'
)

resource ingressOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, ingressMi.id, ingressOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: ingressMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: ingressOperatorRoleId
  }
}

resource serviceManagedIdentityReaderOnIngressMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, ingressMi.id)
  scope: ingressMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Disk CSI driver managed identity
//

resource diskCsiDriverMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-disk-csi-driver-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource serviceManagedIdentityReaderOnDiskCsiDriverMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, diskCsiDriverMi.id)
  scope: diskCsiDriverMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// File CSI driver managed identity
//

resource fileCsiDriverMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-file-csi-driver-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift File Storage Operator
var fileStorageOperatorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0d7aedc0-15fd-4a67-a412-efad370c947e'
)

resource fileStorageOperatorRoleNsgAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, fileCsiDriverMi.id, fileStorageOperatorRoleId, customerNsg.id)
  scope: customerNsg
  properties: {
    principalId: fileCsiDriverMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: fileStorageOperatorRoleId
  }
}

resource fileStorageOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, fileCsiDriverMi.id, fileStorageOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: fileCsiDriverMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: fileStorageOperatorRoleId
  }
}

resource serviceManagedIdentityReaderOnFileCsiDriverMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, fileCsiDriverMi.id)
  scope: fileCsiDriverMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Image registry managed identity
//

resource imageRegistryMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-image-registry-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource serviceManagedIdentityReaderOnImageRegistryMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, imageRegistryMi.id)
  scope: imageRegistryMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

resource imageRegistryOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, imageRegistryMi.id, imageRegistryOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: imageRegistryMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: imageRegistryOperatorRoleId
  }
}

//
// Cloud network config managed identity
//

resource cloudNetworkConfigMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-cp-cloud-network-config-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift Network Operator
var networkOperatorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'be7a6435-15ae-4171-8f30-4a343eff9e8f'
)

resource networkOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, cloudNetworkConfigMi.id, networkOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: cloudNetworkConfigMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: networkOperatorRoleId
  }
}

resource serviceManagedIdentityReaderOnCloudNetworkMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, readerRoleId, cloudNetworkConfigMi.id)
  scope: cloudNetworkConfigMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRoleId
  }
}

//
// Data plane identities
//

// Azure Red Hat OpenShift Federated Credential
var federatedCredentialsRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ef318e2a-8334-4a05-9e4a-295a196c6a6e'
)

resource dpDiskCsiDriverMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-dp-disk-csi-driver-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource dpDiskCsiDriverMiFederatedCredentialsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpDiskCsiDriverMi.id, federatedCredentialsRoleId)
  scope: dpDiskCsiDriverMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: federatedCredentialsRoleId
  }
}

resource dpFileCsiDriverMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-dp-file-csi-driver-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource dpFileCsiDriverMiFederatedCredentialsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpFileCsiDriverMi.id, federatedCredentialsRoleId)
  scope: dpFileCsiDriverMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: federatedCredentialsRoleId
  }
}

resource dpFileCsiDriverFileStorageOperatorRoleNsgAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpFileCsiDriverMi.id, fileStorageOperatorRoleId, customerNsg.id)
  scope: customerNsg
  properties: {
    principalId: dpFileCsiDriverMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: fileStorageOperatorRoleId
  }
}

resource dpFileCsiDriverFileStorageOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpFileCsiDriverMi.id, fileStorageOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: dpFileCsiDriverMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: fileStorageOperatorRoleId
  }
}

resource dpImageRegistryMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-dp-image-registry-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

resource dpImageRegistryMiFederatedCredentialsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpImageRegistryMi.id, federatedCredentialsRoleId)
  scope: dpImageRegistryMi
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: federatedCredentialsRoleId
  }
}

resource dpImageRegistryOperatorRoleVnetAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, dpImageRegistryMi.id, imageRegistryOperatorRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: dpImageRegistryMi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: imageRegistryOperatorRoleId
  }
}

//
// Service managed identity
//

resource serviceManagedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-service-managed-identity-${randomSuffix}'
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
}

// Azure Red Hat OpenShift with hosted control planes Service Managed Identity
var hcpServiceManagedIdentityRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'c0ff367d-66d8-445e-917c-583feb0ef0d4'
)

resource serviceManagedIdentityRoleAssignmentVnet 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, hcpServiceManagedIdentityRoleId, customerVnet.id)
  scope: customerVnet
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: hcpServiceManagedIdentityRoleId
  }
}

resource serviceManagedIdentityRoleAssignmentNSG 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, serviceManagedIdentity.id, hcpServiceManagedIdentityRoleId, customerNsg.id)
  scope: customerNsg
  properties: {
    principalId: serviceManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: hcpServiceManagedIdentityRoleId
  }
}


// Cluster resource
resource hcp 'Microsoft.RedHatOpenShift/hcpOpenShiftClusters@2026-09-01-preview' = {
  name: clusterName
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
  properties: {
    version: {
      id: clusterVersion
      channelGroup: 'stable'
    }
    dns: {}
    network: {
      networkType: 'OVNKubernetes'
      podCidr: podCidr
      serviceCidr: serviceCidr
      machineCidr: addressPrefix
      hostPrefix: 23
    }
    etcd: {
      dataEncryption: {
        keyManagementMode: 'CustomerManaged'
        customerManaged: {
          encryptionType: 'KMS'
          kms: {
            activeKey: {
              name: etcdEncryptionKeyName
              version: last(split(etcdEncryptionKey.properties.keyUriWithVersion, '/'))
            }
            vaultName: customerKeyVaultName
            visibility: privateKeyVault ? 'Private' : 'Public'
          }
        }
      }
    }
    api: {
      visibility: apiVisibility
    }
    ingress: {
      type: ingressType
    }
    cryptoRestrictions: cryptoRestrictions
    clusterImageRegistry: {
      state: 'Enabled'
    }
    platform: {
      managedResourceGroup: managedResourceGroupName
      subnetId: subnet.id
      vnetIntegrationSubnetId: vnetIntegrationSubnet.id
      outboundType: 'LoadBalancer'
      networkSecurityGroupId: customerNsg.id
      operatorsAuthentication: {
        userAssignedIdentities: {
          controlPlaneOperators: {
            'cluster-api-azure': clusterApiAzureMi.id
            'control-plane': controlPlaneMi.id
            'cloud-controller-manager': cloudControllerManagerMi.id
            ingress: ingressMi.id
            'disk-csi-driver': diskCsiDriverMi.id
            'file-csi-driver': fileCsiDriverMi.id
            'image-registry': imageRegistryMi.id
            'cloud-network-config': cloudNetworkConfigMi.id
            kms: kmsMi.id
          }
          dataPlaneOperators: {
            'disk-csi-driver': dpDiskCsiDriverMi.id
            'file-csi-driver': dpFileCsiDriverMi.id
            'image-registry': dpImageRegistryMi.id
          }
          serviceManagedIdentity: serviceManagedIdentity.id
        }
      }
    }
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${serviceManagedIdentity.id}': {}
      '${clusterApiAzureMi.id}': {}
      '${controlPlaneMi.id}': {}
      '${cloudControllerManagerMi.id}': {}
      '${ingressMi.id}': {}
      '${diskCsiDriverMi.id}': {}
      '${fileCsiDriverMi.id}': {}
      '${imageRegistryMi.id}': {}
      '${cloudNetworkConfigMi.id}': {}
      '${kmsMi.id}': {}
    }
  }
  dependsOn: [
    hcpClusterApiProviderRoleVnetAssignment
    keyVaultCryptoUserToKeyVaultRoleAssignment
    hcpControlPlaneOperatorVnetRoleAssignment
    hcpControlPlaneOperatorNsgRoleAssignment
    cloudControllerManagerRoleNsgAssignment
    cloudControllerManagerRoleVnetAssignment
    ingressOperatorRoleVnetAssignment
    fileStorageOperatorRoleNsgAssignment
    fileStorageOperatorRoleVnetAssignment
    networkOperatorRoleVnetAssignment
    dpDiskCsiDriverMiFederatedCredentialsRoleAssignment
    dpFileCsiDriverMiFederatedCredentialsRoleAssignment
    dpImageRegistryMiFederatedCredentialsRoleAssignment
    serviceManagedIdentityRoleAssignmentVnet
    serviceManagedIdentityRoleAssignmentNSG
    dpFileCsiDriverFileStorageOperatorRoleNsgAssignment
    dpFileCsiDriverFileStorageOperatorRoleVnetAssignment
    imageRegistryOperatorRoleVnetAssignment
    dpImageRegistryOperatorRoleVnetAssignment
    serviceManagedIdentityReaderOnControlPlaneMi
    serviceManagedIdentityReaderOnCloudControllerManagerMi
    serviceManagedIdentityReaderOnIngressMi
    serviceManagedIdentityReaderOnDiskCsiDriverMi
    serviceManagedIdentityReaderOnFileCsiDriverMi
    serviceManagedIdentityReaderOnImageRegistryMi
    serviceManagedIdentityReaderOnCloudNetworkMi
    serviceManagedIdentityReaderOnClusterApiAzureMi
    serviceManagedIdentityReaderOnKmsMi
  ]
}

// Node pool resource
resource nodepool 'Microsoft.RedHatOpenShift/hcpOpenShiftClusters/nodePools@2026-09-01-preview' = {
  parent: hcp
  name: nodePoolName
  location: resourceGroup().location
  tags: { agnosticd_project: projectTag }
  properties: {
    version: {
      id: nodePoolVersion
      channelGroup: 'stable'
    }
    platform: {
      subnetId: hcp.properties.platform.subnetId
      availabilityZone: workerZone
      vmSize: workerSize
      osDisk: {
        sizeGiB: workerDisk
        diskStorageAccountType: 'StandardSSD_LRS'
      }
    }
    replicas: workerCount
    autoRepair: true
  }
}
