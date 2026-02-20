# Docker Registry Cleaner

Cleans up unused Docker images from Domino's registry by analyzing MongoDB to see what's actually in use.

## What It Does

- Maps image layers, sizes, and tags (understands shared layers between images)
- Checks MongoDB collections (runs, workspaces, models, projects, scheduler_jobs, etc.) to identify actively used images
- Deletes unused images with optional S3 backup
- Resumes after failures using checkpoint files
- Can optionally clean up MongoDB records for deleted images

> **Warning:** MongoDB cleanup is risky. Environments and models link to many other Domino assets (projects, runs, workspaces, scheduler jobs, app versions, user preferences, etc.). Start with Docker-only cleanup first. Use `--mongo-cleanup` flags only when you understand the impact.

## Installation

### Using Helm (Recommended for Production)

```bash
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform
```

Run commands in the pod:
```bash
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- docker-registry-cleaner --help
```

See the [Helm Chart README](charts/docker-registry-cleaner/README.md) for configuration options, upgrades, and advanced usage.

**Option 2: Local Installation (Development/Testing)**
```bash
pip install -e .
```

Local installation requires access to cluster-internal services (MongoDB, Docker registry, Keycloak). You'll need to set up port-forwarding:

```bash
kubectl port-forward -n domino-platform svc/mongodb-replicaset 27017:27017 &
kubectl port-forward -n domino-platform svc/docker-registry 5000:5000 &
kubectl port-forward -n domino-platform svc/keycloak-http 8080:80 &
```

Update `config.yaml` to use localhost for registry, MongoDB, and Keycloak endpoints.

### Command Conventions

Commands shown below assume you're running in the Helm-deployed pod. For local development, use `python python/main.py` instead of `docker-registry-cleaner`.

## Web UI

Docker Registry Cleaner includes a web-based user interface for easier report viewing and operation management.

### Features

- **Report Browser**: View and analyze all generated JSON reports with visual summaries
- **Operations Dashboard**: Run analysis and dry-run cleanup commands from the browser
- **Real-time Output**: See command execution results in real-time
- **Safety-First Design**: Only dry-run commands can be executed via the UI

### Accessing the Web UI

**Option 1: Port-forward (Quick Access)**
```bash
kubectl port-forward -n domino-platform svc/docker-registry-cleaner-frontend 8080:8080
```
Then open [http://localhost:8080](http://localhost:8080) in your browser.

**Option 2: Ingress (Production)**

Enable the ingress in your Helm values:
```yaml
frontend:
  enabled: true
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: registry-cleaner.your-domain.com
        paths:
          - path: /
            pathType: Prefix
```

Then access at [https://registry-cleaner.your-domain.com](https://registry-cleaner.your-domain.com)

### Web UI Usage

1. **View Reports**: Navigate to the home page to see all available reports
2. **Analyze Reports**: Click on any report to view formatted summaries and raw JSON
3. **Run Operations**: Go to the Operations page to execute analysis commands
4. **Safety**: Destructive operations (with `--apply`) must still be run via `kubectl exec`

See the [Frontend README](frontend/README.md) for more details on the web interface.

## Basic Workflow

```bash
# Check system health
docker-registry-cleaner health_check

# Analyze what would be deleted (dry-run)
docker-registry-cleaner delete_archived_tags --environment

# Delete with confirmation
docker-registry-cleaner delete_archived_tags --environment --apply

# Delete with S3 backup (recommended)
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-bucket
```

## Health Checks

Verify connectivity before running deletions:

```bash
docker-registry-cleaner health_check
```

Checks connectivity to registry, MongoDB, Kubernetes, and S3 (if configured). Deletion scripts automatically run health checks before operations.

## Common Tasks

### Reclaim Space from Archived Environments

Run health checks, then do a dry-run to see what would be deleted:

```bash
docker-registry-cleaner health_check
docker-registry-cleaner delete_archived_tags --environment
```

Review the output to verify no actively-used images are included. When ready, delete with S3 backup:

```bash
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-backup-bucket
```

### Periodic Cleanup of Unused Environments

Analyze unused environments:

```bash
docker-registry-cleaner delete_unused_environments --unused-since-days 30
```

Archive rather than delete (MongoDB-only):

```bash
docker-registry-cleaner archive_unused_environments --unused-since-days 30 --apply
```

Comprehensive deletion with backup:

```bash
docker-registry-cleaner delete_all_unused_environments --apply --backup --s3-bucket my-backup-bucket
```

### Find Where an Environment Is Used

```bash
docker-registry-cleaner find_environment_usage --environment-id <environmentObjectId>
```

Shows all references in runs, workspaces, models, projects, scheduler jobs, organizations, app versions, and user preferences.

### Clean Up MongoDB References to Missing Images

```bash
# Dry-run
docker-registry-cleaner delete_unused_references

# Apply
docker-registry-cleaner delete_unused_references --apply
```

## Common Options

All deletion scripts support these options:

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually perform deletions (dry-run without this) | `false` |
| `--force` | Skip confirmation prompts | `false` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups (required with `--backup`) | From config |
| `--region REGION` | AWS region for S3/ECR operations | `us-west-2` |
| `--generate-reports` | Force regeneration of analysis reports | `false` |
| `--enable-docker-deletion` | Override registry auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |

### Rate Limiting

Registry operations are automatically rate-limited (default: 10 requests/second, burst of 20). Configure in `config.yaml` under `skopeo.rate_limit`.

### Safety Defaults

- Dry-run mode unless `--apply` is specified
- Confirmation prompts when using `--apply` (use `--force` to skip)
- S3 backups with `--backup`
- MongoDB records only deleted after successful Docker deletion

## Available Commands

### Analysis

```bash
# Generate usage reports
docker-registry-cleaner reports

# Per-image size report (largest images by total size and potential freed space)
docker-registry-cleaner image_size_report

# Per-user size report (who uses the most registry space)
docker-registry-cleaner user_size_report
```

### Deletion

#### Delete Archived Tags

```bash
# Analyze archived environments
docker-registry-cleaner delete_archived_tags --environment

# Analyze archived models
docker-registry-cleaner delete_archived_tags --model

# Delete both with S3 backup
docker-registry-cleaner delete_archived_tags --environment --model --apply --backup --s3-bucket my-bucket
```

#### Delete Unused Environments

```bash
# Find unused environments
docker-registry-cleaner delete_unused_environments

# Delete with backup
docker-registry-cleaner delete_unused_environments --apply --backup --s3-bucket my-bucket

# Only consider environments unused if no execution in 30+ days
docker-registry-cleaner delete_unused_environments --unused-since-days 30 --apply
```

**Date filtering:** `--unused-since-days N` considers environments unused only if their last execution was more than N days ago (based on `last_used`, `completed`, or `started` timestamp). Without this flag, any historical run marks the environment as in-use.

#### Archive Unused Environments (MongoDB-Only)

Marks unused environments as archived by setting `isArchived = true` on `environments_v2` documents, without touching Docker images:

```bash
# Dry-run
docker-registry-cleaner archive_unused_environments

# Only consider environments unused if no execution in 30+ days
docker-registry-cleaner archive_unused_environments --unused-since-days 30

# Apply with confirmation
docker-registry-cleaner archive_unused_environments --apply

# Archive environments unused for 60+ days without confirmation
docker-registry-cleaner archive_unused_environments --unused-since-days 60 --apply --force
```

#### Delete Deactivated User Private Environments

```bash
# Find private environments owned by deactivated Keycloak users
docker-registry-cleaner delete_unused_private_environments

# Delete with backup
docker-registry-cleaner delete_unused_private_environments --apply --backup --s3-bucket my-bucket
```

#### Comprehensive Cleanup

Runs multiple cleanup operations in sequence:

```bash
# Dry-run all unused environments
docker-registry-cleaner delete_all_unused_environments

# Delete all with backup
docker-registry-cleaner delete_all_unused_environments --apply --backup --s3-bucket my-bucket
```

This runs:
1. Delete unused environments (not used in workspaces, models, or project defaults)
2. Delete deactivated user private environments
3. (Optional) Run Docker registry garbage collection:

```bash
docker-registry-cleaner run_registry_gc
```

#### Delete Unused MongoDB References

Cleans up MongoDB records referencing non-existent Docker images. This modifies primary Domino metadata collections, so only use it if you're comfortable with the schema and have recent backups.

```bash
# Dry-run
docker-registry-cleaner delete_unused_references

# Apply
docker-registry-cleaner delete_unused_references --apply
```

This command only modifies MongoDB, not Docker images, so `--backup` doesn't apply.

#### Intelligent Image Deletion

```bash
# Delete specific image
docker-registry-cleaner delete_image environment:abc-123 --apply

# Delete using analysis reports
docker-registry-cleaner delete_image --apply --backup --s3-bucket my-bucket

# Filter by ObjectIDs from file
docker-registry-cleaner delete_image --input environments --apply

# Clean up MongoDB references (opt-in)
docker-registry-cleaner delete_image --apply --mongo-cleanup
```

## ObjectID Filtering

Target specific models or environments by ObjectID. Prefixes are required:
- `environment:<id>`
- `environmentRevision:<id>`
- `model:<id>`
- `modelVersion:<id>`

```bash
# Create file with ObjectIDs (one per line, prefixes required)
cat > environments <<EOF
environment:6286a3c76d4fd0362f8ba3ec
environmentRevision:6286a3c76d4fd0362f8ba3ed
model:627d94043035a63be6140e93
modelVersion:627d94043035a63be6140e94
EOF

# Use with deletion commands
docker-registry-cleaner delete_image --input environments --apply
```

## Backup and Restore

### Backup to S3

All deletion commands support `--backup`:

```bash
# Backup and delete
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-bucket

# Backup only (no deletion)
docker-registry-cleaner delete_archived_tags --environment --backup --s3-bucket my-bucket --force
```

### Restore from S3

```bash
# Restore specific tags
docker-registry-cleaner backup_restore restore --tags tag1 tag2

# Restore with explicit S3 bucket override
docker-registry-cleaner backup_restore restore --s3-bucket my-backup-bucket --tags tag1 tag2
```

Images are backed up before deletion. If backup fails, deletion is aborted. Restoration of Docker images doesn't restore their MongoDB records, but restored images can be used as base images for new Domino environments.

## Resume Capability

Long-running operations can be interrupted and resumed:

```bash
# Resume from checkpoint
docker-registry-cleaner delete_archived_tags --environment --apply --resume

# Resume specific operation
docker-registry-cleaner delete_archived_tags --environment --apply --resume --operation-id 2026-01-15-14-30-00
```

Checkpoints are saved every 10 items in `reports/checkpoints/`. Use `--resume` to continue from the last checkpoint. Supported scripts: `delete_archived_tags`, `delete_unused_environments`, `delete_unused_private_environments`.

### Understanding the Numbers

**Tag references vs unique images:** Dry-run reports can show many "matching tags" (e.g., 1232). The same Docker image `(image_type, tag)` can match multiple archived MongoDB IDs. Deletion is done per unique `(image_type, tag)`, so "1232 tag references" might mean only "600 unique Docker images" are actually deleted.

**Checkpoint files:** Store `completed_items`, `failed_items`, and `skipped_items` as unique image identifiers (`image_type:tag`). `total_items` is the number of unique images in that run.

**Why dry-run after --apply shows different numbers:** Re-running dry-run re-queries MongoDB and re-scans the registry. Numbers differ due to: (1) different scope (`--environment --model` vs `--environment` only), (2) actual deletions reducing registry tags, (3) MongoDB changes (records unarchived or removed). Use the same flags when comparing runs, or use `--input <report.json>` to delete from a saved report.

## Timestamped Reports

Auto-generated reports include timestamps:

```
reports/mongodb_usage_report-2026-01-15-14-30-00.json
reports/tag-sums-2026-01-15-14-35-12.json
reports/final-report-2026-01-15-14-40-25.json
```

This lets you compare results across runs and track changes over time. User-specified output files (via `--output`) don't include timestamps.

## Safety Features

**Transaction safety:**
- Deletes Docker images before MongoDB records
- Only deletes MongoDB records for successfully deleted Docker images
- Preserves MongoDB records if Docker deletion fails

**Smart analysis:**
- Only deletes images not referenced in MongoDB (checks runs, workspaces, models, projects, scheduler_jobs, organizations, app_versions)
- Calculates freed space accounting for shared layers between images
- Only counts layers that would have zero references after deletion

**Registry management:**
- Automatically disables registry deletion mode after operations
- Waits for registry pods to restart and become ready
- Cleanup happens even if errors occur

## Configuration

### Configuration Priority

Configuration is loaded in this order (later values override earlier):

1. `config.yaml` in project root (or Helm values `config` section)
2. Environment variables
3. Command-line arguments

### Helm Configuration

Configuration is managed through [values.yaml](charts/docker-registry-cleaner/values.yaml). Key parameters:

- Image repository and tag
- Resource requests and limits
- Persistent storage size and storage class
- Registry URL overrides (e.g., for AWS ECR)
- Additional environment variables
- Application configuration (registry, MongoDB, Kubernetes settings)

See the [Helm Chart README](charts/docker-registry-cleaner/README.md) for complete configuration reference and examples.

### config.yaml

For non-Helm deployments, copy `config-example.yaml` to `config.yaml` and modify as needed.

### Environment Variables

For local installations, export environment variables to override configuration values. (For Helm deployments, see the [Helm Chart README](charts/docker-registry-cleaner/README.md) for `extraEnv`.)

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

# Keycloak
export KEYCLOAK_HOST="https://keycloak.example.com/auth/"
export KEYCLOAK_USERNAME="admin"
export KEYCLOAK_PASSWORD="keycloak_password"

# S3 Backup
export S3_BUCKET="my-backup-bucket"
export S3_REGION="us-west-2"
```

### Docker Registry Authentication

**Username & Password priority:**

1. `REGISTRY_USERNAME`|`REGISTRY_PASSWORD` environment variable (explicit override)
2. Custom Kubernetes secret (via `REGISTRY_AUTH_SECRET` or defaults to `domino-registry` secret)
3. AWS ECR authentication (automatic for `*.amazonaws.com` registries)
4. Azure ACR authentication (automatic for `*.azurecr.io` registries)

For most in-cluster Domino deployments, no explicit configuration is needed.

For AWS ECR and Azure ACR, authentication is automatic via managed identity when running in EKS or AKS respectively. For Azure ACR, set `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` to configure the managed identity. See [docs/acr-authentication.md](docs/acr-authentication.md) for step-by-step instructions on setting up a managed identity in Azure Portal and configuring the Helm chart.

For other external registries (Quay, GCR, etc.), you have two options:

1. **Kubernetes secret (recommended for production):** Set `REGISTRY_AUTH_SECRET` to the name of a secret containing `.dockerconfigjson` with your registry credentials. See the [Helm Chart README](charts/docker-registry-cleaner/README.md) for examples.

2. **Environment variables:** Set both `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` environment variables.

## How It Works

### Image Analysis

1. Lists all image tags in Docker registry
2. Inspects image layers and calculates sizes
3. Detects shared layers across images
4. Tracks reference counts for accurate space calculation
5. Generates `reports/final-report.json`

### Intelligent Deletion

1. Cross-references MongoDB usage data with image analysis
2. Identifies unused images not referenced in MongoDB collections (runs, workspaces, models, projects, scheduler_jobs, organizations, app_versions)
3. Calculates freed space with shared layer awareness
4. Optionally backs up to S3 before deletion
5. Deletes Docker images first, then MongoDB records
6. Ensures registry deletion is disabled after completion

## Troubleshooting

### Registry Auto-Detection Fails

If your registry URL doesn't contain enough information for auto-detection:

```bash
# Enable with default "docker-registry" statefulset
docker-registry-cleaner delete_archived_tags --environment --apply --enable-docker-deletion

# Or specify custom statefulset
docker-registry-cleaner delete_unused_environments --apply \
  --enable-docker-deletion \
  --registry-statefulset my-custom-registry
```

Useful when:
- Registry URL is an IP address or external DNS name
- Registry service has non-standard naming
- You want explicit control over which StatefulSet/Deployment is modified

### Common Issues

**Registry authentication:**
```bash
export REGISTRY_PASSWORD="your_password"
skopeo list-tags docker://registry.example.com/repository
```

**MongoDB connection:**
```bash
export MONGODB_PASSWORD="your_password"
python -c "from python.mongo_utils import get_mongo_client; print('Connected')"
```

**ObjectID format:**
```bash
# Valid: 62798b9bee0eb12322fc97e8 (24 hex chars)
# Valid: environment:62798b9bee0eb12322fc97e8
# Invalid: 62798b9bee0eb12322fc97e (23 chars)
```

## System Requirements

- Python 3.11+
- kubectl access for Kubernetes operations
- Registry access for image inspection and deletion
- MongoDB access for metadata and cleanup
- Keycloak access for deactivated user detection (optional)

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
