# Configuration

## Priority Order

Configuration is loaded in this order (later values override earlier):

1. `config.yaml` in the project root (or Helm values `config` section)
2. Environment variables
3. Command-line arguments

## Helm Configuration

Configuration is managed through [values.yaml](../charts/docker-registry-cleaner/values.yaml). Key parameters:

- Image repository and tag
- Resource requests and limits
- Persistent storage size and storage class
- Registry URL overrides (e.g., for AWS ECR)
- Additional environment variables
- Application configuration (registry, MongoDB, Kubernetes settings)

See the [Helm Chart README](../charts/docker-registry-cleaner/README.md) for complete configuration reference and examples.

## config.yaml

For non-Helm deployments, copy `config-example.yaml` to `config.yaml` and modify as needed.

## Environment Variables

For local installations, export environment variables to override `config.yaml` values. For Helm deployments, use `extraEnv` in `values.yaml`.

```bash
# Docker Registry
export REGISTRY_URL="registry.example.com"
export REPOSITORY="my-repo"
export REGISTRY_USERNAME="your_username"    # Required for external registries (Quay, GCR)
export REGISTRY_PASSWORD="your_password"
export REGISTRY_AUTH_SECRET="secret-name"   # Optional: K8s secret with .dockerconfigjson
export AZURE_CLIENT_ID="client-id"          # For ACR: managed identity client ID
export AZURE_TENANT_ID="tenant-id"          # For ACR: Azure AD tenant ID

# Kubernetes
export DOMINO_PLATFORM_NAMESPACE="domino-platform"

# MongoDB
export MONGODB_USERNAME="admin"
export MONGODB_PASSWORD="mongo_password"

# Keycloak (required for delete_unused_private_environments)
export KEYCLOAK_HOST="https://keycloak.example.com/auth/"
export KEYCLOAK_USERNAME="admin"
export KEYCLOAK_PASSWORD="keycloak_password"

# S3 Backup
export S3_BUCKET="my-backup-bucket"
export S3_REGION="us-west-2"
```

## Docker Registry Authentication

**Priority order:**

1. `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` environment variables (explicit override)
2. Custom Kubernetes secret via `REGISTRY_AUTH_SECRET` (or the default `domino-registry` secret)
3. AWS ECR — automatic for `*.amazonaws.com` registries
4. Azure ACR — automatic for `*.azurecr.io` registries

For most in-cluster Domino deployments no explicit configuration is needed.

For AWS ECR and Azure ACR, authentication is automatic via managed identity when running in EKS or AKS. For Azure ACR, set `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`. See [acr-authentication.md](acr-authentication.md) for step-by-step instructions.

For other external registries (Quay, GCR, etc.):

1. **Kubernetes secret (recommended for production):** Set `REGISTRY_AUTH_SECRET` to the name of a secret containing `.dockerconfigjson`. See the [Helm Chart README](../charts/docker-registry-cleaner/README.md) for examples.
2. **Environment variables:** Set both `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`.

## Rate Limiting

Registry operations are automatically rate-limited (default: 10 requests/second, burst of 20). Configure in `config.yaml` under `skopeo.rate_limit`.
