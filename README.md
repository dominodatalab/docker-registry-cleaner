# Docker Registry Cleaner

An intelligent Docker registry cleanup tool that analyzes workload usage patterns and safely removes unused images while preserving actively used ones.

## 🎯 Overview

This project provides a comprehensive solution for cleaning up Docker registries by:

1. **Analyzing registry contents** - Maps image layers, sizes, and tags with shared layer awareness
2. **Domino-integrated intelligent detection** - Identifies which images are actively used by workloads or project defaults
3. **Safe deletion with backups** - Optionally backs up Docker images to S3 before deletion
4. **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
5. **Unused reference detection** - Identifies and removes MongoDB records referencing non-existent Docker images

## 🚀 Quick Start

### Prerequisites

```bash
# Install dependencies
pip install -r requirements.txt

# Configure (copy and edit config-example.yaml)
cp config-example.yaml config.yaml
```

### Basic Workflow

All scripts are invoked through `python/main.py` with a standardized interface:

```bash
# 1. Analyze what would be deleted (dry-run is default)
python python/main.py delete_archived_tags --environment

# 2. Delete with confirmation
python python/main.py delete_archived_tags --environment --apply

# 3. Delete with S3 backup (recommended)
python python/main.py delete_archived_tags --environment --apply --backup --s3-bucket my-bucket

# 4. Comprehensive cleanup
python python/main.py delete_all_unused_environments --apply --backup --s3-bucket my-bucket
```

## 🎛️ Common Options

All Docker deletion scripts share a standardized interface with these common options:

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually perform deletions (without this, it's dry-run only) | `false` |
| `--force` | Skip confirmation prompts | `false` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups (required with `--backup`) | From config |
| `--region REGION` | AWS region for S3/ECR operations | `us-west-2` |
| `--generate-reports` | Force regeneration of analysis reports | `false` |
| `--enable-docker-deletion` | Override registry auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |

### Safety by Default

- **Dry-run mode** - No changes are made to Docker or MongoDB unless `--apply` is specified.
- **Confirmation prompts** - User must confirm deletions when using `--apply`. Use `--force` to skip the confirmation prompt.
- **S3 backups** - Use `--backup` to back up images before deletion.
- **Transaction safety** - MongoDB records are only deleted after successful Docker deletion.

## 📊 Available Commands

### Analysis Commands

```bash
# Analyze registry contents
python python/main.py image_data_analysis [--file OBJECTIDS]

# Generate usage reports
python python/main.py reports [--generate-reports]

# Extract MongoDB metadata
python python/main.py extract_metadata --target both
```

### Deletion Commands

All deletion commands support the common options listed above.

#### Delete Archived Tags

```bash
# Analyze archived environments (dry-run)
python python/main.py delete_archived_tags --environment

# Analyze archived models (dry-run)
python python/main.py delete_archived_tags --model

# Delete both archived environments and models
python python/main.py delete_archived_tags --environment --model --apply

# Delete archived environments with S3 backup
python python/main.py delete_archived_tags --environment --apply --backup --s3-bucket my-bucket
```

#### Delete Unused Environments

```bash
# Find unused environments (dry-run)
python python/main.py delete_unused_environments

# Delete with S3 backup and confirmation
python python/main.py delete_unused_environments --apply --backup --s3-bucket my-bucket

# Only consider environments unused if last execution was >30 days ago
python python/main.py delete_unused_environments --unused-since-days 30 --apply

# Force regenerate reports and delete
python python/main.py delete_unused_environments --generate-reports --apply --force
```

**Date Range Filtering:** Use `--unused-since-days N` to only consider environments as unused if their last execution was more than N days ago. This filters based on the `last_used`, `completed`, or `started` timestamp from runs. If omitted, any historical run marks the environment as in-use.

#### Archive Unused Environments (Mongo-only)

Marks unused environments as archived in MongoDB by setting `isArchived = true` on `environments_v2` documents, without touching Docker images:

```bash
# Dry-run: list environments that would be archived
python python/main.py archive_unused_environments

# Only consider environments unused if last execution was >30 days ago
python python/main.py archive_unused_environments --unused-since-days 30

# Actually mark unused environments as archived (with confirmation)
python python/main.py archive_unused_environments --apply

# Archive environments unused for >60 days without confirmation
python python/main.py archive_unused_environments --unused-since-days 60 --apply --force
```

**Date Range Filtering:** Use `--unused-since-days N` to only consider environments as unused if their last execution was more than N days ago. This filters based on the `last_used`, `completed`, or `started` timestamp from runs. If omitted, any historical run marks the environment as in-use.

#### Delete Deactivated User Private Environments

```bash
# Find private environments owned by deactivated Keycloak users
python python/main.py delete_unused_private_environments

# Delete with backup
python python/main.py delete_unused_private_environments --apply --backup --s3-bucket my-bucket
```

#### Comprehensive Cleanup

Run multiple cleanup operations in sequence:

```bash
# Analyze all unused environments (dry-run)
python python/main.py delete_all_unused_environments

# Delete all unused environments with backup
python python/main.py delete_all_unused_environments --apply --backup --s3-bucket my-bucket
```

This command runs:
1. Delete unused environments (not used in workspaces, models, or project defaults)
2. Delete deactivated user private environments

#### Delete Unused MongoDB References

Cleans up MongoDB records referencing non-existent Docker images:

```bash
# Find unused references (dry-run)
python python/main.py delete_unused_references

# Delete unused references
python python/main.py delete_unused_references --apply
```

**Note:** This command only modifies MongoDB, not Docker images, so `--backup` is not applicable.

#### Intelligent Image Deletion

```bash
# Delete specific image
python python/main.py delete_image environment:abc-123 --apply

# Delete using analysis reports
python python/main.py delete_image --apply --backup --s3-bucket my-bucket

# Filter by ObjectIDs from file (prefixes required; see ObjectID Filtering)
python python/main.py delete_image --file environments --apply

# Clean up MongoDB references (opt-in)
python python/main.py delete_image --apply --mongo-cleanup
```

## 🔍 ObjectID Filtering

Target specific models or compute environments by ObjectID. Prefixes are required to avoid ambiguity:
- `environment:<id>`
- `environmentRevision:<id>`
- `model:<id>`
- `modelVersion:<id>`

```bash
# Create a file with ObjectIDs (one per line, prefixes required)
cat > environments <<EOF
environment:6286a3c76d4fd0362f8ba3ec

# Explicitly environment revision
environmentRevision:6286a3c76d4fd0362f8ba3ed

# Explicitly model
model:627d94043035a63be6140e93

# Explicitly model version
modelVersion:627d94043035a63be6140e94
EOF

# Use with any analysis or deletion command
python python/main.py image_data_analysis --file environments
python python/main.py delete_image --file environments --apply
```

## 📦 Backup and Restore

### Backup Images to S3

All Docker deletion commands support `--backup`:

```bash
# Backup and delete
python python/main.py delete_archived_tags --environment --apply --backup --s3-bucket my-bucket

# Backup only (no deletion)
python python/main.py delete_archived_tags --environment --backup --s3-bucket my-bucket --force
```

### Restore Images from S3

```bash
# Restore specific tags from S3 backup
python python/backup_restore.py restore --tags tag1 tag2

# Restore with explicit S3 bucket override
python python/backup_restore.py restore --s3-bucket my-backup-bucket --tags tag1 tag2
```

**Behavior:**
- Images are backed up to S3 **before** deletion
- If backup fails, deletion is **aborted** to prevent data loss
- Images can be restored to any compatible registry
- Restoration of Docker images does not restore their records in Mongo
- However, once an image has been restored, its URL can be used as the base image for a new Domino Compute Environment

## 🛡️ Safety Features

### Transaction Safety
- **Docker-first deletion** - Always deletes Docker images before MongoDB records
- **Success tracking** - Tracks which Docker deletions succeeded
- **Conditional cleanup** - Only deletes MongoDB records for successfully deleted images
- **Failure preservation** - Preserves MongoDB records if Docker deletion fails

### Intelligent Analysis
- **Workload-aware** - Only deletes images not used by running pods
- **Shared layer analysis** - Properly calculates freed space accounting for shared layers
- **Reference counting** - Only counts layers that would have zero references after deletion

### Registry Deletion Cleanup

All delete scripts ensure that registry deletion is properly disabled after operations:

- **Automatic cleanup** - Registry deletion (`REGISTRY_STORAGE_DELETE_ENABLED`) is always disabled after script completion
- **Error handling** - Cleanup occurs even if errors occur during deletion
- **Pod readiness checks** - Scripts wait for registry pods to restart and become ready after configuration changes

## 🔧 Configuration

### Priority Order

Configuration is loaded in this order (later values override earlier):

1. `config.yaml` in project root
2. Environment variables
3. Command-line arguments

### config.yaml

Copy `config-example.yaml` to `config.yaml` and modify as needed.

### Environment Variables

```bash
# Docker Registry
export REGISTRY_URL="registry.example.com"
export REPOSITORY="my-repo"
export REGISTRY_PASSWORD="your_password"  # Optional for ECR

# Kubernetes
export DOMINO_PLATFORM_NAMESPACE="domino-platform"

# MongoDB
export MONGODB_USERNAME="admin"  # Optional
export MONGODB_PASSWORD="mongo_password"  # Optional - uses K8s secrets if not set

# Keycloak (for deactivated user cleanup)
export KEYCLOAK_HOST="https://keycloak.example.com/auth/"
export KEYCLOAK_USERNAME="admin"
export KEYCLOAK_PASSWORD="keycloak_password"

# S3 Backup
export S3_BUCKET="my-backup-bucket"
export S3_REGION="us-west-2"

# Skopeo
export SKOPEO_USE_POD="false"  # Set to "true" for K8s pod mode
```

### View Current Configuration

```bash
python python/main.py --config
```

## 🏗️ Architecture

### Core Scripts

- **`python/main.py`** - Unified entrypoint for all operations
- **`python/config_manager.py`** - Centralized configuration and Skopeo client management
- **`python/backup_restore.py`** - S3 backup and restore functionality

### Deletion Scripts

All deletion scripts follow the same pattern and support common options:

- **`python/delete_image.py`** - Intelligent deletion based on workload analysis
- **`python/delete_archived_tags.py`** - Delete archived environments and/or models
- **`python/delete_unused_environments.py`** - Delete environments not used anywhere
- **`python/archive_unused_environments.py`** - Mark unused environments as archived in MongoDB (`isArchived = true` on `environments_v2`)
- **`python/delete_unused_private_environments.py`** - Delete private environments owned by deactivated users
- **`python/delete_unused_references.py`** - Delete MongoDB references to non-existent images

### Analysis Scripts

- **`python/extract_metadata.py`** - Extract MongoDB metadata
- **`python/image_data_analysis.py`** - Analyze registry contents with shared layer detection
- **`python/reports.py`** - Generate tag usage reports

### Utility Scripts

- **`python/mongo_cleanup.py`** - MongoDB record cleanup
- **`python/mongo_utils.py`** - MongoDB connection utilities
- **`python/object_id_utils.py`** - ObjectID handling
- **`python/logging_utils.py`** - Logging configuration

## 📊 How It Works

### Image Analysis

1. Lists all image tags in Docker registry
2. Inspects image layers and calculates sizes
3. Detects shared layers across images
4. Tracks reference counts for accurate space calculation
5. Generates `reports/final-report.json`

### Intelligent Deletion

1. Cross-references MongoDB usage data and image analysis
2. Identifies unused images not referenced in MongoDB (runs, workspaces, models, scheduled jobs, etc.)
3. Queries MongoDB for additional usage (project defaults, scheduled jobs, etc.)
4. Calculates freed space with shared layer awareness
5. Optionally backs up to S3 before deletion
6. Deletes Docker images first, then MongoDB records
7. Ensures registry deletion is disabled after completion

## 🚨 Troubleshooting

### Registry Auto-Detection Fails

If your Docker registry URL doesn't contain enough information for auto-detection:

```bash
# Enable registry deletion with default "docker-registry" statefulset
python python/main.py delete_archived_tags --environment --apply --enable-docker-deletion

# Or specify custom statefulset/deployment name
python python/main.py delete_unused_environments --apply \
  --enable-docker-deletion \
  --registry-statefulset my-custom-registry
```

Programmatic usage:

```python
from python.config_manager import config_manager, SkopeoClient

skopeo_client = SkopeoClient(
    config_manager, 
    use_pod=False,
    enable_docker_deletion=True,
    registry_statefulset="my-custom-registry"
)
```

This is useful when:
- Registry URL is an IP address or external DNS name
- Registry service has non-standard naming
- You want explicit control over which StatefulSet/Deployment is modified

### Common Issues

**Kubernetes API access:**
```bash
kubectl get pods -n domino-compute
```

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

### Debug Mode

```bash
export PYTHONPATH=python
```

## 📝 Requirements

### Python Dependencies

```bash
pip install -r requirements.txt
```

- `boto3` - AWS SDK for S3 operations
- `kubernetes` - Kubernetes API client
- `pymongo` - MongoDB client
- `python-keycloak` - Keycloak admin client
- `PyYAML` - Configuration parsing
- `requests` - HTTP client

### System Requirements

- **Python 3.8+**
- **kubectl access** - For Kubernetes operations
- **Registry access** - For image inspection and deletion
- **MongoDB access** - For metadata and cleanup
- **Keycloak access** - For deactivated user detection (optional)

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
