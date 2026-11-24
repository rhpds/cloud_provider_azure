# host_ocp4_aro_hcp_external_auth

Configure Red Hat Build of Keycloak as an external OIDC provider for the ARO HCP
console. The core `ocp4_workload_authentication` role deploys Keycloak and its
realm and calls this role after obtaining the Keycloak administrator credentials.
The core role then adds the OpenShift role bindings for the Keycloak users.

Run Azure `initialize` on the controller first and use a current HCP
administrative kubeconfig. The sandbox identity needs permission to write
`Microsoft.RedHatOpenShift/hcpOpenShiftClusters/externalAuths` on the cluster.
The Keycloak route must be reachable over trusted HTTPS by the controller,
the OpenShift console and hosted API server, and users' browsers. The controller
uses `community.general` and `kubernetes.core` from the execution environment.

The role obtains the callback URL from the console route, waits for OIDC
discovery, and reconciles the confidential Keycloak client through its Admin
API. This also works when the realm already exists from an earlier run. The
client uses the exact console `/auth/callback` redirect and has direct password
grants and service accounts disabled. ARO validates Keycloak's issuer and client
audience and maps the `preferred_username` claim to the OpenShift user name.

The role configures the Azure `externalAuths` resource and stores the client
secret under `clientSecret` in the associated
`openshift-config/<provider>-console-openshift-console` Secret. It checks the
console operator and sets `openshift_console_url` for the workload's access
output. Refresh the HCP
administrative kubeconfig if it expires before a retry. Cluster destruction
removes the external authentication resource and the in-cluster Keycloak instance.

See [defaults/main.yml](defaults/main.yml) for inputs and
[Microsoft's ARO HCP external authentication guide](https://learn.microsoft.com/en-us/azure/openshift/howto-configure-external-authentication)
for the console configuration contract.
