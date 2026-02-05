# Docker Registry Cleaner Helm Chart

This Helm chart deploys the Docker Registry Cleaner as a StatefulSet in your Kubernetes cluster, providing persistent storage for reports and safe cleanup of Docker registry images.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Access to the Domino platform namespace
- Persistent Volume provisioner support (for report storage)

## Installation

### Basic Installation

```bash
# Install from local chart
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform

# Verify installation
kubectl get statefulset -n domino-platform docker-registry-cleaner
kubectl get pods -n domino-platform -l app.kubernetes.io/name=docker-registry-cleaner

# Run commands
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- docker-registry-cleaner --help
```

### Common Configuration Examples

**Customize image and resources:**
```bash
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set image.tag=v0.3.2 \
  --set resources.requests.memory=512Mi \
  --set persistence.size=20Gi
```

**Override registry URL (e.g., for AWS ECR or Azure ACR):**
```bash
# AWS ECR
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set env.registryUrl="946429944765.dkr.ecr.eu-west-1.amazonaws.com"

# Azure ACR
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set env.registryUrl="myregistry.azurecr.io"
```

**Use custom values file:**
```bash
cat > custom-values.yaml <<EOF
image:
  tag: v0.3.2
resources:
  requests:
    memory: 512Mi
config:
  registry:
    url: "my-registry:5000"
# Optional: Add extra environment variables
extraEnv:
  - name: AWS_DEFAULT_REGION
    value: "us-west-2"
EOF

helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --values custom-values.yaml
```

### Upgrading

```bash
# Upgrade to a new version
helm upgrade docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set image.tag=v0.3.3

# Upgrade with custom values
helm upgrade docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --values custom-values.yaml
```

### Uninstalling

```bash
helm uninstall docker-registry-cleaner --namespace domino-platform
```

## Configuration

The following table lists the configurable parameters of the Docker Registry Cleaner chart and their default values.

### Image Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Container image repository | `quay.io/domino/docker-registry-cleaner` |
| `image.tag` | Container image tag | `v0.3.2` |
| `image.pullPolicy` | Image pull policy | `Always` |
| `imagePullSecrets` | Image pull secrets | `[{name: domino-quay-repos}]` |

### Resource Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `resources.requests.cpu` | CPU resource requests | `100m` |
| `resources.requests.memory` | Memory resource requests | `256Mi` |
| `resources.limits.cpu` | CPU resource limits | `1000m` |
| `resources.limits.memory` | Memory resource limits | `1Gi` |

### Persistence Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `persistence.enabled` | Enable persistent storage for reports | `true` |
| `persistence.size` | Persistent volume size | `10Gi` |
| `persistence.storageClass` | Storage class name | `dominodisk` |

### Security Context

| Parameter | Description | Default |
|-----------|-------------|---------|
| `podSecurityContext.fsGroup` | FSGroup for volume permissions | `65532` |

### Environment Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `env.registryUrl` | Override registry URL (e.g., for ECR, Quay) | `""` |
| `env.registryAuthSecret` | Name of a K8s secret with `.dockerconfigjson` for secure credential storage | `""` |
| `env.azureClientId` | Client ID of the managed identity for ACR auth | `""` |
| `env.azureTenantId` | Azure AD tenant ID for ACR auth | `""` |
| `extraEnv` | Additional environment variables (array) | `[]` |
| `dominoPlatformNamespace` | Domino platform namespace | `domino-platform` |

### Service Account

| Parameter | Description | Default |
|-----------|-------------|---------|
| `serviceAccount.create` | Create service account | `true` |
| `serviceAccount.name` | Service account name | `docker-registry-cleaner` |
| `serviceAccount.fullnameOverride` | Override full name | `""` |

### Application Configuration

All configuration under the `config` key is mounted as `/config.yaml` in the container. See [values.yaml](values.yaml) for the complete configuration structure.

Key configuration sections:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `config.registry.url` | Docker registry URL | `docker-registry:5000` |
| `config.registry.repository` | Docker repository name | `dominodatalab` |
| `config.mongo.host` | MongoDB host | `mongodb-replicaset` |
| `config.mongo.port` | MongoDB port | `27017` |
| `config.mongo.db` | MongoDB database name | `domino` |
| `config.kubernetes.namespace` | Kubernetes namespace | `domino-platform` |
| `config.security.dry_run_by_default` | Dry run by default | `true` |
| `config.security.require_confirmation` | Require user confirmation | `true` |

## Advanced Configuration

### Adding Extra Environment Variables

The `extraEnv` field supports the full Kubernetes environment variable syntax, including `valueFrom` for secrets and configMaps:

```yaml
extraEnv:
  # Simple key-value pairs
  - name: AWS_DEFAULT_REGION
    value: "us-west-2"

  # Reference to a secret
  - name: CUSTOM_API_KEY
    valueFrom:
      secretKeyRef:
        name: my-secret
        key: api-key

  # Reference to a configMap
  - name: CUSTOM_CONFIG
    valueFrom:
      configMapKeyRef:
        name: my-configmap
        key: config-value
```

### Overriding Registry URL

For AWS ECR, Azure ACR, or other external registries, use the `env.registryUrl` parameter:

```bash
# AWS ECR
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set env.registryUrl="946429944765.dkr.ecr.eu-west-1.amazonaws.com"

# Azure ACR
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --set env.registryUrl="myregistry.azurecr.io"
```

This sets the `REGISTRY_URL` environment variable, which overrides the `config.registry.url` value.

### Custom Storage Class

To use a different storage class for the persistent volume:

```yaml
persistence:
  enabled: true
  size: 20Gi
  storageClass: "my-storage-class"
```

### Disabling Persistence

If you don't need persistent report storage:

```yaml
persistence:
  enabled: false
```

Note: Reports will be lost when the pod restarts.

## RBAC Permissions

The chart creates a ServiceAccount with the following permissions:

- **StatefulSets**: get, list, patch (in domino-platform namespace)
- **Pods**: get, list (in domino-platform namespace)
- **Secrets**: get (in domino-platform namespace)

These permissions are required for:
- Enabling/disabling registry deletion mode
- Checking pod readiness after registry restarts
- Auto-discovering registry credentials from Kubernetes secrets

## Authentication

### Docker Registry

The tool automatically discovers Docker registry credentials from:

**Password priority:**
1. `REGISTRY_PASSWORD` environment variable (explicit override)
2. Custom Kubernetes secret via `env.registryAuthSecret` (for external registries)
3. Kubernetes `domino-registry` secret (auto-discovery for in-cluster registries)
4. AWS ECR authentication (for `*.amazonaws.com` registries)
5. Azure ACR authentication (for `*.azurecr.io` registries)

**Username priority:**
1. `REGISTRY_USERNAME` environment variable (explicit override)
2. Custom Kubernetes secret via `env.registryAuthSecret` (for external registries)
3. Kubernetes `domino-registry` secret (auto-discovery for in-cluster registries)
4. AWS ECR registries automatically use `AWS` as the username
5. Azure ACR registries automatically use a placeholder GUID

For most Domino deployments with in-cluster registries, no additional configuration is needed.

#### AWS ECR and Azure ACR

For AWS ECR registries (`*.amazonaws.com`) and Azure ACR registries (`*.azurecr.io`), authentication is automatic via managed identity when running in EKS or AKS respectively. Simply set the registry URL:

```yaml
# For AWS ECR
env:
  registryUrl: "123456789.dkr.ecr.us-west-2.amazonaws.com"

# For Azure ACR
env:
  registryUrl: "myregistry.azurecr.io"
```

**For Azure AKS:** You must specify the managed identity client ID and tenant ID:

```yaml
env:
  registryUrl: "myregistry.azurecr.io"
  azureClientId: "12345678-1234-1234-1234-123456789abc"  # Client ID of the managed identity with AcrPull role
  azureTenantId: "87654321-4321-4321-4321-cba987654321"  # Azure AD tenant ID
```

Ensure the pod's service account has the appropriate permissions:
- **AWS EKS:** IAM role with `ecr:GetAuthorizationToken` and `ecr:BatchGetImage` permissions
- **Azure AKS:** Managed identity with `AcrPull` role on the ACR

#### External Registries (Quay, GCR, etc.)

**Option 1: Using a Kubernetes secret (recommended for production)**

Create a secret with `.dockerconfigjson` containing your registry credentials. See the [Kubernetes documentation on Docker config secrets](https://kubernetes.io/docs/concepts/configuration/secret/#docker-config-secrets) for more details.

```bash
kubectl create secret docker-registry quay-registry-creds \
  --namespace domino-platform \
  --docker-server=quay.io \
  --docker-username="myorg+robotname" \
  --docker-password="your-robot-token"
```

Then reference it in your values:

```yaml
env:
  registryUrl: "quay.io"
  registryAuthSecret: "quay-registry-creds"
```

**Option 2: Using environment variables (simpler but less secure)**

```yaml
extraEnv:
  - name: REGISTRY_USERNAME
    value: "myorg+robotname"  # For Quay robot accounts
  - name: REGISTRY_PASSWORD
    valueFrom:
      secretKeyRef:
        name: my-registry-secret
        key: password
```

### MongoDB

MongoDB credentials are automatically loaded from the `mongodb-replicaset-admin` secret in the Domino platform namespace.

### Keycloak

Keycloak credentials are automatically loaded from the `keycloak-http` secret in the Domino platform namespace.

## Troubleshooting

### Pod Not Starting

Check pod logs:
```bash
kubectl logs -n domino-platform docker-registry-cleaner-0
```

Check pod events:
```bash
kubectl describe pod -n domino-platform docker-registry-cleaner-0
```

### Permission Errors

Verify the ServiceAccount has correct RBAC permissions:
```bash
kubectl get serviceaccount -n domino-platform docker-registry-cleaner
kubectl get rolebinding -n domino-platform docker-registry-cleaner
```

### Storage Issues

Check PVC status:
```bash
kubectl get pvc -n domino-platform reports-storage-docker-registry-cleaner-0
```

Verify storage class exists:
```bash
kubectl get storageclass
```

### Running Health Checks

Before performing deletions, run health checks:
```bash
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  docker-registry-cleaner health_check
```

## Usage Examples

### Basic Workflow

```bash
# 1. Check system health
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  docker-registry-cleaner health_check

# 2. Analyze what would be deleted (dry-run)
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  docker-registry-cleaner delete_archived_tags --environment

# 3. Delete with confirmation
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  docker-registry-cleaner delete_archived_tags --environment --apply

# 4. Delete with S3 backup (recommended)
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  docker-registry-cleaner delete_archived_tags --environment --apply \
  --backup --s3-bucket my-bucket
```

### Accessing Reports

Reports are stored in the persistent volume at `/data/reports`:

```bash
# List reports
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- \
  ls -lh /data/reports

# Copy report to local machine
kubectl cp domino-platform/docker-registry-cleaner-0:/data/reports/report.json ./report.json
```

## Support

For issues and questions:
- GitHub Issues: https://github.com/dominodatalab/docker-registry-cleaner/issues
- Main Documentation: [README.md](../../README.md)
