# ACR Authentication for the docker-registry-cleaner StatefulSet

This guide explains how to grant the docker-registry-cleaner StatefulSet access to Azure Container Registry (ACR) when deployed on AKS.

## How authentication works

The cleaner authenticates to ACR using **Azure Managed Identity** — not a Service Principal or App Registration. You do not need to register an application in Azure Portal or manage client secrets. Instead, you assign an Azure Managed Identity to the AKS node pool and grant that identity the `AcrPull` role on the ACR.

At runtime, `authenticate_acr()` in [python/utils/auth/providers.py](../python/utils/auth/providers.py):
1. Obtains an Azure AD access token from the instance metadata service (IMDS) using the managed identity.
2. Exchanges that token for an ACR refresh token via `https://<registry>/oauth2/exchange`.
3. Calls `skopeo login` with the refresh token.

When `AZURE_CLIENT_ID` is set in the container environment, a specific **user-assigned** managed identity is targeted. Without it, `DefaultAzureCredential` is used, which picks up whichever identity is available on the node (the system-assigned identity or a single user-assigned identity).

---

## Option A — Use the node pool's system-assigned identity (quickest)

AKS node pools have a system-assigned managed identity by default. You can grant it `AcrPull` access without creating anything new.

### 1. Find the node pool identity

```bash
# Get the node resource group (where VMSS lives)
az aks show \
  --resource-group <aks-resource-group> \
  --name <aks-cluster-name> \
  --query nodeResourceGroup -o tsv

# Find the managed identity principal ID for the node pool VMSS
az vmss identity show \
  --resource-group <node-resource-group> \
  --name <vmss-name> \
  --query principalId -o tsv
```

### 2. Assign AcrPull on the ACR

```bash
ACR_ID=$(az acr show \
  --resource-group <acr-resource-group> \
  --name <acr-name> \
  --query id -o tsv)

az role assignment create \
  --role AcrPull \
  --assignee <principal-id-from-step-1> \
  --scope $ACR_ID
```

### 3. Update values.yaml

```yaml
env:
  registryUrl: "myregistry.azurecr.io"
  # azureClientId is not required when there is only one identity on the node
```

No `AZURE_CLIENT_ID` is needed here because there is only one identity, so `DefaultAzureCredential` finds it automatically.

---

## Option B — Create a dedicated user-assigned managed identity (recommended)

A user-assigned identity is explicit, portable, and avoids granting ACR access to every workload on the node pool. This is the recommended approach for production.

### 1. Create a user-assigned managed identity

In Azure Portal:
1. Search for **Managed Identities** and click **Create**.
2. Fill in **Subscription**, **Resource group**, **Region**, and a **Name** (e.g. `docker-registry-cleaner-identity`).
3. Click **Review + create**, then **Create**.
4. Once created, open the identity and note the **Client ID** from the **Overview** page — you will need this later.

Via CLI:
```bash
az identity create \
  --resource-group <resource-group> \
  --name docker-registry-cleaner-identity

# Note the clientId from the output
az identity show \
  --resource-group <resource-group> \
  --name docker-registry-cleaner-identity \
  --query '{clientId: clientId, principalId: principalId}' -o json
```

### 2. Assign the identity to the AKS node pool VMSS

The managed identity must be attached to the VMSS that backs the node pool so that the IMDS endpoint can return a token for it.

In Azure Portal:
1. Navigate to the AKS cluster > **Node pools** > select the node pool.
2. Click **Settings > Identity** in the left panel of the node pool blade.
3. Switch to the **User assigned** tab and click **Add**.
4. Select the identity created in step 1 and click **Add**.

Via CLI:
```bash
# Get node resource group and VMSS name
NODE_RG=$(az aks show -g <aks-rg> -n <aks-name> --query nodeResourceGroup -o tsv)
VMSS=$(az vmss list -g $NODE_RG --query '[0].name' -o tsv)

az vmss identity assign \
  --resource-group $NODE_RG \
  --name $VMSS \
  --identities /subscriptions/<sub-id>/resourceGroups/<resource-group>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/docker-registry-cleaner-identity
```

### 3. Grant AcrPull on the ACR

```bash
IDENTITY_PRINCIPAL=$(az identity show \
  --resource-group <resource-group> \
  --name docker-registry-cleaner-identity \
  --query principalId -o tsv)

ACR_ID=$(az acr show \
  --resource-group <acr-resource-group> \
  --name <acr-name> \
  --query id -o tsv)

az role assignment create \
  --role AcrPull \
  --assignee $IDENTITY_PRINCIPAL \
  --scope $ACR_ID
```

Role assignments can take a few minutes to propagate.

### 4. Update values.yaml

```yaml
env:
  registryUrl: "myregistry.azurecr.io"
  azureClientId: "<client-id-from-step-1>"  # UUID shown in the identity's Overview
  azureTenantId: "<azure-ad-tenant-id>"     # Optional; helps with troubleshooting
```

`AZURE_CLIENT_ID` is required when the node pool has more than one user-assigned identity. It tells the SDK exactly which identity to use. Setting it is always safe and is recommended even when there is only one.

---

## Helm values reference

| Value | Description |
|---|---|
| `env.registryUrl` | ACR hostname, e.g. `myregistry.azurecr.io` |
| `env.azureClientId` | Client ID of the user-assigned managed identity |
| `env.azureTenantId` | Azure AD tenant ID (optional; used only in log output) |

These map to the `REGISTRY_URL`, `AZURE_CLIENT_ID`, and `AZURE_TENANT_ID` environment variables in the container.

---

## Verifying the setup

Run the health check command from inside the pod:

```bash
kubectl exec -it <pod-name> -n <namespace> -- \
  python -m main health_check
```

A successful ACR connection will print:
```
✓ Registry connectivity: <n> repositories reachable
```

If authentication fails, the logs will include:
- The value of `AZURE_CLIENT_ID` that was used.
- Whether the token exchange with `https://<registry>/oauth2/exchange` succeeded.
- Suggested remediation steps (identity not found, missing role assignment, etc.).

You can also test authentication directly inside the pod:

```bash
kubectl exec -it <pod-name> -n <namespace> -- bash
skopeo login myregistry.azurecr.io \
  --username 00000000-0000-0000-0000-000000000000 \
  --password "$(curl -s -H 'Metadata: true' \
    'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2019-08-01&resource=https://management.azure.com/' \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')"
```

---

## Common errors

| Error | Likely cause |
|---|---|
| `Identity not found` | The managed identity is not assigned to the VMSS, or `AZURE_CLIENT_ID` is wrong. |
| `401` from `oauth2/exchange` | The identity exists but does not have `AcrPull` on the ACR, or the role assignment has not propagated yet. |
| `DefaultAzureCredential` fails | No managed identity at all on the node. Complete Option A or B above. |
| `AZURE_CLIENT_ID` not set warning in logs | The node has multiple user-assigned identities; set `env.azureClientId` to disambiguate. |
