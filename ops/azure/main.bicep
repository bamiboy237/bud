@description('SSH public key for the VM administrator.')
param sshPublicKey string

param adminUsername string = 'budadmin'

@description('CIDR block allowed to connect over SSH. Use your public IP with /32.')
param sshSource string

@description('Public Linux container image for this Bud build, pinned by digest.')
param budImage string

@description('Wait for Move Bud instead of starting a new assistant.')
param receiveMove bool = true

param location string = resourceGroup().location
param vmSize string = 'Standard_B2s'
param name string = 'bud'

var dnsLabel = toLower('bud-${uniqueString(resourceGroup().id, name)}')
var config = loadTextContent('../../compose.yaml')
var caddy = loadTextContent('../Caddyfile')
var bootstrap = loadTextContent('./cloud-init.yaml')

resource internet 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${name}-ip'
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: { domainNameLabel: dnsLabel }
  }
}

resource firewall 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'web'
        properties: {
          access: 'Allow'
          direction: 'Inbound'
          priority: 100
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: 'Internet'
          destinationPortRanges: ['80', '443']
          destinationAddressPrefix: '*'
        }
      }
      {
        name: 'ssh'
        properties: {
          access: 'Allow'
          direction: 'Inbound'
          priority: 110
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: sshSource
          destinationPortRange: '22'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${name}-net'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.20.0.0/16'] }
  }

  resource subnet 'subnets' = {
    name: 'bud'
    properties: { addressPrefix: '10.20.0.0/24' }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${name}-nic'
  location: location
  properties: {
    networkSecurityGroup: { id: firewall.id }
    ipConfigurations: [
      {
        name: 'bud'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: { id: internet.id }
          subnet: { id: network::subnet.id }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: name
  location: location
  properties: {
    hardwareProfile: { vmSize: vmSize }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        deleteOption: 'Detach'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        diskSizeGB: 32
      }
    }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      customData: base64(replace(replace(replace(replace(replace(
        bootstrap,
        '__COMPOSE__', base64(config)),
        '__CADDY__', base64(caddy)),
        '__DOMAIN__', internet.properties.dnsSettings.fqdn),
        '__IMAGE__', base64(budImage)),
        '__RECEIVE_MOVE__', string(receiveMove)))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: nic.id }]
    }
  }
}

output budURL string = 'https://${internet.properties.dnsSettings.fqdn}'
output sshCommand string = 'ssh ${adminUsername}@${internet.properties.dnsSettings.fqdn}'
