# Docker Registry Cleaner

An intelligent Docker registry cleanup tool that analyzes workload usage patterns and safely removes unused images while preserving actively used ones.

## 🎯 Overview

This project provides a comprehensive solution for cleaning up Docker registries by:

1. **Analyzing registry contents** - Maps image layers, sizes, and tags with shared layer awareness, to accurately predict space savings
2. **Domino-integrated intelligent detection** - Identifies which images are actively used by Domino workloads or project defaults
3. **Unused environment deletion** - Deletes unused and archived Domino environment and model images 
4. **Deactivated user cleanup** - Finds and deletes private environments owned by deactivated Domino users
5. **S3 backup and restore** - Optionally back up Docker images as individual tar files to S3  
6. **Unused reference detection** - Identify and remove MongoDB records referencing non-existent Docker images
7. **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images

## 🏗️ Architecture

The project consists of several Python scripts that work together:

- **`python/main.py`** - Unified entrypoint for all operations
- **`python/backup_restore.py`** - Backup and restore Docker images to/from S3 (used by all Docker delete scripts)
- **`python/config_manager.py`** - Centralized configuration and Skopeo client management
- **`python/delete_archived_tags.py`** - Finds and deletes Docker tags associated with archived environments and/or models
- **`python/delete_image.py`** - Intelligently deletes unused images based on workload analysis
- **`python/delete_unused_environments.py`** - Finds and deletes environments not used in Domino workloads or as project defaults (auto-generates reports)
- **`python/delete_unused_private_environments.py`** - Finds and deletes private environments owned by deactivated Domino users
- **`python/delete_unused_references.py`** - Finds and deletes MongoDB references to non-existent Docker images
- **`python/extract_metadata.py`** - Extracts metadata from MongoDB collections
- **`python/image_data_analysis.py`** - Analyzes Docker registry contents and layer information with shared layer detection
- **`python/inspect_workload.py`** - Analyzes Kubernetes workloads and run
- **`python/logging_utils.py`** - Logging configuration
- **`python/mongo_cleanup.py`** - Cleans up MongoDB records by Docker tag
- **`python/mongo_utils.py`** - MongoDB connection and utilities
- **`python/object_id_utils.py`** - ObjectID handling and validation
- **`python/report_utils.py`** - Report generation helpers
- **`python/reports.py`** - Generates tag usage reports comparing registry contents against workspace/model usage (auto-generates metadata)

## 🔍 ObjectID Filtering

The tool supports filtering by MongoDB ObjectIDs to target specific models or compute environments:

- **Image tags** contain 24-character MongoDB ObjectIDs as prefixes
- **Filter by ObjectIDs** to analyze/delete only specific models/environments
- **File-based input** - Read ObjectIDs from a file (supports typed format)
- **Validation** ensures ObjectIDs are 24 characters and valid hexadecimal
- **Cross-script support** - filtering works across all analysis and deletion scripts

### ObjectID File Format

The file supports a flexible format with optional type prefixes:

```
# One ObjectID per line. Lines starting with # are ignored.
# You can optionally prefix with a type to disambiguate:
#   environment:<ObjectID>
#   model:<ObjectID>
# If no prefix is provided, the ID applies to both ("any").

# Applies to both
62798b9bee0eb12322fc97e8

# Explicitly environment-only
environment:6286a3c76d4fd0362f8ba3ec

# Explicitly model-only
model:627d94043035a63be6140e93
```

ObjectIDs must be:
- **24 characters long** (standard MongoDB ObjectID length)
- **Valid hexadecimal** (0-9, a-f)
- **One per line** (additional columns are ignored)

## 🚀 Quick Start

### Prerequisites

1. **Kubernetes Access**: kubectl configured and access to the cluster
2. **Registry Access**: Password for the Docker registry (optional for ECR)
3. **MongoDB Access**: Connection details for MongoDB (optional via Kubernetes secrets)
4. **Keycloak Access** (for deactivated user cleanup): Keycloak admin credentials
5. **Python Dependencies**: Install required packages

```bash
pip install -r requirements.txt
```

### Configuration Setup

The tool uses `config.yaml` for default settings. 
A template `config-example.yaml` is provided. Rename it to `config.yaml` and modify the values as needed.  

### Basic Workflow

```bash
# 1. Analyze current image usage
python python/main.py reports

# 2. Basic deletion (dry-run)
python python/main.py delete_image environment:abc-123

# 3. Intelligent deletion of archived environments (dry-run)
python python/main.py delete_archived_tags

# 4. Intelligent deletion of unused environments (--apply flag to delete images)
python python/main.py delete_all_unused_environments --apply
```

### Backup and Restore

All delete scripts support backing up images to S3 before deletion:

```bash
# Backup images to S3 before deletion (all delete scripts that remove Docker images)
python python/main.py delete_archived_tags --environment --model --apply --backup --s3-bucket my-backup-bucket
python python/main.py delete_unused_environments --apply --backup --s3-bucket my-backup-bucket
python python/main.py delete_unused_private_environments --apply --backup --s3-bucket my-backup-bucket
python python/main.py delete_image --apply --backup --s3-bucket my-backup-bucket

# Specify custom AWS region (default: us-west-2)
python python/main.py delete_archived_tags --environment --apply --backup --s3-bucket my-backup-bucket --region us-east-1

# Restore images from S3 backup (uses config.yaml for registry/repo/S3 bucket)
python python/backup_restore.py restore --tags tag1 tag2

# Restore with explicit S3 bucket override
python python/backup_restore.py restore --s3-bucket my-backup-bucket --tags tag1 tag2

# Backup behavior:
# - Images are backed up to S3 BEFORE deletion
# - If backup fails, deletion is ABORTED to prevent data loss
# - Backup includes all image layers and metadata
# - Images can be restored to any compatible registry
```

### Cleanup Commands

```bash
# Find archived environment tags (dry-run)
python python/main.py delete_archived_tags --environment

# Find archived model tags (dry-run)
python python/main.py delete_archived_tags --model

# Find both archived environments and models (dry-run)
python python/main.py delete_archived_tags --environment --model

# Delete archived environment tags
python python/main.py delete_archived_tags --environment --apply

# Delete archived model tags
python python/main.py delete_archived_tags --model --apply

# Delete both with backup (recommended)
python python/main.py delete_archived_tags --environment --model --apply --backup --s3-bucket my-backup-bucket

# Find unused MongoDB references (dry-run)
python python/main.py delete_unused_references

# Delete unused MongoDB references
python python/main.py delete_unused_references --apply

# Find private environments owned by deactivated users (dry-run)
python python/main.py delete_unused_private_environments

# Delete private environments owned by deactivated users
python python/main.py delete_unused_private_environments --apply

# Find unused environments (auto-generates required reports if missing)
python python/main.py delete_unused_environments

# Force regeneration of metadata before analysis
python python/main.py delete_unused_environments --generate-reports

# Delete unused environments (with confirmation)
python python/main.py delete_unused_environments --apply

# Full workflow: generate reports and delete
python python/main.py delete_unused_environments --generate-reports --apply

# Comprehensive unused environment cleanup - analyze (dry-run, runs both scripts)
python python/main.py delete_all_unused_environments

# Comprehensive unused environment cleanup - delete (requires --apply)
python python/main.py delete_all_unused_environments --apply
```

### ObjectID Filtering Examples

```bash
# Filter by ObjectIDs from file (supports typed format)
python python/main.py inspect_workload --file environments
python python/main.py image_data_analysis --file environments
python python/main.py delete_image --file environments

# Combine with other options
python python/main.py inspect_workload --file environments --namespace my-namespace
python python/main.py delete_image --file environments --apply --force
```

### Environment Variables

You can also override settings with environment variables:

```bash
# Docker Registry
export REGISTRY_URL="registry.example.com"
export REPOSITORY="my-repo"
export REGISTRY_PASSWORD="your_password"  # Optional - ECR auto-auth if not set

# Kubernetes
export PLATFORM_NAMESPACE="domino-platform"
export COMPUTE_NAMESPACE="domino-compute"

# MongoDB
export MONGODB_USERNAME="admin"  # Optional
export MONGODB_PASSWORD="mongo_password"  # Optional - uses K8s secrets if not set

# Keycloak (for deactivated user cleanup)
export KEYCLOAK_HOST="https://keycloak.example.com/auth/"
export KEYCLOAK_USERNAME="admin"
export KEYCLOAK_PASSWORD="keycloak_password"

# S3 Backup (optional - can also use --s3-bucket flag)
export S3_BUCKET="my-backup-bucket"
export S3_REGION="us-west-2"

# Skopeo Configuration (optional)
export SKOPEO_USE_POD="false"  # Set to "true" for Kubernetes pod mode
```

### Show Current Configuration

```bash
python python/main.py --config
```

## 📊 How It Works

### 1. Workload Analysis (`inspect_workload.py`)

Analyzes running Kubernetes pods to identify which images are currently in use:

- **Scans running pods** in specified namespaces
- **Extracts container images** from pod specifications
- **Tracks usage patterns** and workload types
- **Generates workload report** with image usage statistics
- **ObjectID filtering** - Only processes images matching provided ObjectIDs from file
- **Dynamic Kubernetes client** - Works both locally and in-cluster

**Output**: `reports/workload-report.json` - Maps image tags to pod usage information

### 2. Image Analysis (`image_data_analysis.py`)

Analyzes the Docker registry to understand image composition:

- **Lists all image tags** in specified repositories
- **Inspects image layers** and calculates sizes
- **Maps tag distributions** across layers
- **Shared layer detection** - Properly calculates freed space accounting for shared layers
- **Reference counting** - Tracks how many images use each layer
- **ObjectID filtering** - Only analyzes images matching provided ObjectIDs from file
- **Space analysis** - Calculates total space usage and layer sharing

**Output**: `reports/final-report.json` - Detailed layer analysis with size and tag information

### 3. Intelligent Deletion (`delete_image.py`)

Safely removes unused images based on comprehensive analysis:

- **Cross-references** workload and image analysis reports
- **Identifies unused images** that aren't referenced by running pods
- **Calculates space savings** with shared layer awareness
- **ObjectID filtering** - Only considers images matching provided ObjectIDs from file
- **Dry-run by default** for safety
- **Confirmation prompts** before actual deletion
- **MongoDB cleanup** - Optional cleanup of related MongoDB records

**Output**: `reports/deletion-analysis.json` - Summary of what would be deleted and space saved

### 4. Archived Tags (`delete_archived_tags.py`)

Unified script that finds and optionally deletes Docker tags associated with archived environments and/or models:

- **Flexible type selection** - Process environments (`--environment`), models (`--model`), or both
- **Queries MongoDB** for archived records:
  - `environments_v2` collection for archived environments (`isArchived == true`)
  - `environment_revisions` collection for related revisions
  - `models` collection for archived models (`isArchived == true`)
  - `model_versions` collection for related versions
- **Scans Docker registry** for tags containing archived ObjectIDs
- **Type-aware processing** - Automatically categorizes ObjectIDs by type (environment, revision, model, version)
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly (no subprocess overhead)
- **Smart MongoDB cleanup** - Cleans correct collections based on record type
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Two-phase workflow** - Generate report first, then delete from report or directly

**Output**: `reports/archived-tags.json` - Detailed report with freed space calculation

### 5. Unused References (`delete_unused_references.py`)

Finds and optionally deletes MongoDB records referencing non-existent Docker images:

- **Queries MongoDB collections** for Docker image references
- **Checks Docker registry** to verify image existence
- **Identifies orphaned records** in MongoDB
- **Multi-collection support** - Works with `environment_revisions`, `model_versions`, etc.
- **Optional deletion** of MongoDB records with confirmation
- **Two-phase workflow** - Generate report first, then delete

**Output**: `reports/unused-references.json` - Detailed report of unused MongoDB references

### 6. Deactivated User Private Environments (`delete_unused_private_environments.py`)

Finds and optionally deletes private environments owned by deactivated Keycloak users:

- **Queries Keycloak** for deactivated users (`enabled == false`)
- **Extracts Domino user IDs** from Keycloak user attributes (`domino-user-id`)
- **Finds private environments** in MongoDB where `ownerId` matches deactivated user and `visibility == "Private"`
- **Finds related revisions** in `environment_revisions` collection
- **Scans Docker registry** for tags containing these ObjectIDs
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Separate collection cleanup** - Handles both `environments_v2` and `environment_revisions`
- **Two-phase workflow** - Generate report first, then delete

**Output**: `reports/deactivated-user-envs.json` - Detailed report grouped by deactivated user

### 7. Unused Environments (`delete_unused_environments.py`)

Finds and optionally deletes environments that are not being used anywhere:

- **Auto-generates required reports** - Automatically runs `extract_metadata.py` and `inspect_workload.py` if needed
- **Loads metadata** from auto-generated outputs (model and workspace environment usage)
- **Loads workload data** from Kubernetes pod inspection (running containers)
- **Queries MongoDB** for all non-archived environments, project defaults, scheduled jobs, and app versions
- **Identifies unused environments** - Environments NOT in:
  - Model environment usage
  - Workspace environment usage  
  - Running workload pods
  - Project default environments
  - Scheduled job environments (`scheduler_jobs` collection)
  - App version environments (from `app_versions` that reference unarchived `model_products`)
- **Scans Docker registry** for tags containing these unused environment ObjectIDs
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Separate collection cleanup** - Handles both `environments_v2` and `environment_revisions`
- **Two-phase workflow** - Generate report first, then delete

**Auto-generation**: Use `--generate-reports` to force regeneration of all required metadata

**Output**: `reports/unused-environments.json` - Detailed report of unused environments

### 8. Tag Usage Reports (`reports.py`)

Generates comprehensive tag usage reports by comparing registry contents against workspace/model usage:

- **Auto-generates required data** - Automatically runs `extract_metadata.py` and `image_data_analysis.py` if needed
- **Compares tags** - Checks each Docker tag against workspace and model usage data
- **Identifies unused tags** - Highlights tags not used in any workspace or model
- **Calculates space savings** - Shows potential disk space that can be freed
- **Human-readable output** - Sizes formatted in KB, MB, GB for easy reading
- **Detailed breakdown** - Lists each tag with its usage status and size

**Auto-generation**: Use `--generate-reports` to force regeneration of all required metadata

**Output**: Console output with usage summary and unused tags sorted by size

### 10. Metadata Extraction (`extract_metadata.py`)

Extracts metadata from MongoDB collections using PyMongo:

- **Model environment usage** - Aggregates model and environment relationships
- **Workspace environment usage** - Analyzes workspace and environment usage
- **Direct MongoDB access** - No kubectl exec required
- **Comprehensive reporting** - Detailed usage statistics

**Output**: `reports/model_env_usage_output.json`, `reports/workspace_env_usage_output.json`

### 11. MongoDB Cleanup (`mongo_cleanup.py`)

Cleans up MongoDB records based on Docker tag information:

- **Finds matching records** in MongoDB collections
- **Deletes orphaned records** after image deletion
- **Supports multiple formats** - ObjectID prefixes or full tags
- **Safe operation** - Find mode for verification before deletion

## 🛡️ Safety Features

### Default Safety Mode
- **Dry-run by default** - No images are deleted unless `--apply` is specified
- **Confirmation prompts** - User must confirm before actual deletion (unless `--force`)
- **Force mode** - Skip confirmation with `--force` flag
- **S3 Backup** - Optionally backup images to S3 before deletion with `--backup` flag

### Backup to S3 (New!)
- **Pre-deletion backup** - Docker image deletion scripts support `--backup` and `--s3-bucket` flags
- **Automatic abort** - If backup fails, deletion is aborted to prevent data loss
- **Region support** - Specify AWS region with `--region` (default: us-west-2)
- **Restore capability** - Use `backup_restore.py restore` to restore backed up images
- **Supported scripts**: 
  - `delete_archived_tags.py` (supports `--environment` and/or `--model`)
  - `delete_unused_environments.py`
  - `delete_unused_private_environments.py`
  - `delete_image.py`
- **Note**: `delete_unused_references.py` doesn't support backup as it only deletes MongoDB records, not Docker images

### Transaction Safety
- **Docker-first deletion** - Always deletes Docker images before MongoDB records
- **Success tracking** - Tracks which Docker deletions succeeded
- **Conditional MongoDB cleanup** - Only deletes MongoDB records for successfully deleted Docker images
- **Failure preservation** - Preserves MongoDB records if Docker deletion fails
- **Clear logging** - Shows which MongoDB records were skipped due to Docker failures

### Intelligent Analysis
- **Workload-aware** - Only deletes images not used by running pods
- **Shared layer analysis** - Properly calculates freed space accounting for shared layers
- **Reference counting** - Only counts layers that would have zero references after deletion
- **Cross-validation** - Multiple data sources ensure accuracy

### ObjectID Targeting
- **Precise targeting** - Only process specific models/environments
- **File-based input** - Read ObjectIDs from files for easy management
- **Validation** - Ensures ObjectIDs are properly formatted
- **Reduced risk** - Smaller scope means fewer unintended deletions

### MongoDB Safety
- **Connection validation** - Verifies MongoDB connectivity
- **Credential management** - Uses environment variables or Kubernetes secrets
- **Transaction safety** - Proper error handling and rollback
- **Audit trail** - Detailed logging of all operations

## 📋 Usage Examples

### Basic Analysis (All Images)
```bash
# Analyze everything (password optional - uses REGISTRY_PASSWORD env var if not provided)
python python/main.py inspect_workload
python python/main.py image_data_analysis
python python/main.py delete_image
```

### Targeted Analysis (Specific ObjectIDs from File)
```bash
# Analyze only specific models/environments
python python/main.py inspect_workload --file environments
python python/main.py image_data_analysis --file environments
python python/main.py delete_image --file environments
```

### Custom Configuration
```bash
# All commands use config.yaml for registry/repository configuration
```

### Deletion Modes
```bash
# Safe dry-run (default) - password optional
python python/main.py delete_image

# With confirmation (password from environment variable)
python python/main.py delete_image --apply

# Delete with S3 backup (recommended)
python python/main.py delete_image --apply --backup --s3-bucket my-backup-bucket

# Force deletion (no confirmation) - explicit password
python python/main.py delete_image <password> --apply --force

# Using environment variable for password
export REGISTRY_PASSWORD="your_password"
python python/main.py delete_image --apply
```

### Archive Management
```bash
# Find archived environment tags (dry-run with space calculation)
python python/main.py delete_archived_tags --environment --output archived-env-tags.json

# Find archived model tags (dry-run with space calculation)
python python/main.py delete_archived_tags --model --output archived-model-tags.json

# Find both archived environments and models
python python/main.py delete_archived_tags --environment --model --output archived-tags.json

# Delete archived environment tags directly
python python/main.py delete_archived_tags --environment --apply

# Delete archived model tags directly
python python/main.py delete_archived_tags --model --apply

# Delete from pre-generated file
python python/main.py delete_archived_tags --environment --apply --input archived-env-tags.json

# Delete with S3 backup (recommended for safety)
python python/main.py delete_archived_tags --environment --apply --backup --s3-bucket my-backup-bucket

# Delete both types with S3 backup and custom region
python python/main.py delete_archived_tags --environment --model --apply --backup --s3-bucket my-backup-bucket --region us-east-1
```

### Unused References Cleanup
```bash
# Find unused MongoDB references (dry-run)
python python/main.py delete_unused_references --output unused-refs.json

# Delete unused MongoDB references directly
python python/main.py delete_unused_references --apply

# Delete from pre-generated file
python python/main.py delete_unused_references --apply --input unused-refs.json
```

### Deactivated User Cleanup
```bash
# Find private environments owned by deactivated Keycloak users (dry-run)
python python/main.py delete_unused_private_environments --output deactivated-user-envs.json

# Delete private environments owned by deactivated users
python python/main.py delete_unused_private_environments --apply

# Delete from pre-generated file
python python/main.py delete_unused_private_environments --apply --input deactivated-user-envs.json

# Force deletion without confirmation
python python/main.py delete_unused_private_environments --apply --force
```

### Unused Environment Cleanup
```bash
# Find unused environments (auto-generates required reports if missing)
python python/main.py delete_unused_environments

# Force regeneration of all metadata before analysis
python python/main.py delete_unused_environments --generate-reports

# Save to custom output file
python python/main.py delete_unused_environments --output unused-envs.json

# Delete unused environments (with confirmation)
python python/main.py delete_unused_environments --apply

# Delete with S3 backup for safety
python python/main.py delete_unused_environments --apply --backup --s3-bucket my-backup-bucket

# Full workflow: generate reports and delete
python python/main.py delete_unused_environments --generate-reports --apply

# Delete from pre-generated file
python python/main.py delete_unused_environments --apply --input unused-envs.json

# Force deletion without confirmation
python python/main.py delete_unused_environments --apply --force
```

### Comprehensive Unused Environment Cleanup

Run both unused environment cleanup scripts in a single command:

```bash
# Analyze (dry-run) - find unused environments from both sources
python python/main.py delete_all_unused_environments

# Delete after analysis (requires --apply)
python python/main.py delete_all_unused_environments --apply

# With S3 backup for safety
python python/main.py delete_all_unused_environments --apply --backup --s3-bucket my-backup-bucket

# With custom region
python python/main.py delete_all_unused_environments --apply --backup --s3-bucket my-backup-bucket --region us-east-1

# Force without confirmation
python python/main.py delete_all_unused_environments --apply --force

# Generate reports before analysis (dry-run)
python python/main.py delete_all_unused_environments --generate-reports

# Generate reports and delete
python python/main.py delete_all_unused_environments --generate-reports --apply
```

This runs two cleanup operations sequentially:
1. **Delete unused environments** - Environments not used in workspaces, models, or as project defaults
2. **Delete deactivated user private envs** - Private environments owned by deactivated Keycloak users

### Mongo Cleanup

After deleting images, you can optionally clean up related Mongo records:

```bash
# Dry run (find matching records)
python python/main.py mongo_cleanup --file environments

# Delete matching records
python python/main.py mongo_cleanup --apply --file environments

# Specify custom collection
python python/main.py mongo_cleanup --apply --file environments --collection environment_revisions
```

Requirements:
- Set `MONGODB_PASSWORD` in the environment (or it will use Kubernetes secrets)
- The file should contain ObjectIDs or full tags (one per line); only first token per line is read

### Metadata Extraction
```bash
# Extract model and environment usage metadata
python python/main.py extract_metadata --target both

# Extract only model usage
python python/main.py extract_metadata --target model

# Extract only workspace usage
python python/main.py extract_metadata --target workspace
```

## 📁 Project Structure

```
docker-registry-cleaner/
├── config.yaml                           # Configuration defaults
├── environments                          # ObjectID file (typed format)
├── environments-example                  # Example ObjectID file format
├── requirements.txt                      # Python dependencies
├── mongo_queries/                        # Legacy MongoDB query files
├── python/
│   ├── main.py                               # Unified entrypoint
│   ├── backup_restore.py                     # S3 backup/restore for Docker images
│   ├── config_manager.py                     # Configuration and Skopeo client management
│   ├── delete_archived_tags.py               # Archived tag discovery & deletion (environments and/or models)
│   ├── delete_image.py                       # Intelligent image deletion
│   ├── delete_unused_environments.py         # Unused environment cleanup (auto-generates reports)
│   ├── delete_unused_private_environments.py # Deactivated user private env cleanup
│   ├── delete_unused_references.py           # Unused MongoDB reference cleanup
│   ├── extract_metadata.py                   # MongoDB metadata extraction
│   ├── image_data_analysis.py                # Registry content analysis with shared layer detection
│   ├── inspect_workload.py                   # Kubernetes workload analysis
│   ├── logging_utils.py                      # Logging configuration
│   ├── mongo_cleanup.py                      # MongoDB record cleanup
│   ├── mongo_utils.py                        # MongoDB utilities
│   ├── object_id_utils.py                    # ObjectID handling utilities
│   ├── report_utils.py                       # Report helpers
│   └── reports.py                            # Tag usage report generator (auto-generates metadata)
└── reports/                              # Analysis output files
```

## 🔧 Configuration

### config.yaml
Docker Registry Cleaner will attempt to take its configuration from `config.yaml`, in the root of the project.
`config-example.yaml` has been provided as a template. Feel free to copy it and replace values with your own.

### Environment Variables
Alternatively, you can provide values as environment variables, either by manually exporting them in your environment
e.g.
`export REGISTRY_URL=my-docker-registry:5000`
Or– if running in a Kubernetes pod– by adding them to the manifest for the pod.

**Docker Registry:**
- `REGISTRY_URL` - Docker registry URL
- `REPOSITORY` - Repository name
- `REGISTRY_PASSWORD` - Registry password (optional - ECR auto-auth if not set)

**Kubernetes:**
- `PLATFORM_NAMESPACE` - Domino platform namespace
- `COMPUTE_NAMESPACE` - Domino compute namespace

**MongoDB:**
- `MONGODB_USERNAME` - MongoDB username (optional, defaults to 'admin')
- `MONGODB_PASSWORD` - MongoDB password (optional - uses Kubernetes secrets if not set)

**Keycloak (for deactivated user cleanup):**
- `KEYCLOAK_HOST` - Keycloak server URL (e.g. `https://domino.example.com/auth/`)
- `KEYCLOAK_USERNAME` - Keycloak admin username
- `KEYCLOAK_PASSWORD` - Keycloak admin password

**S3 Backup:**
- `S3_BUCKET` - S3 bucket name for image backups (optional)
- `S3_REGION` - AWS region for S3 and ECR operations (default: us-west-2)

**Skopeo:**
- `SKOPEO_USE_POD` - Set to `true` to run Skopeo in Kubernetes pod mode instead of local subprocess (default: false)

### Skopeo Configuration

The tool uses a standardized `SkopeoClient` that provides consistent authentication and configuration across all scripts:

- **Authentication**: Uses `--creds domino-registry:{password}` for all operations
- **ECR Support**: Automatic ECR authentication if registry URL contains "amazonaws.com"
- **TLS**: Deactivated with `--tls-verify=false` for internal registries
- **Execution modes**: 
  - **Local mode** (default): Direct subprocess calls to locally installed Skopeo
  - **Pod mode**: Kubernetes pod execution for environments without local Skopeo installation
  - Configure via `config.yaml` (`skopeo.use_pod`) or environment variable (`SKOPEO_USE_POD`)
- **Centralized config**: All Skopeo operations use the same credentials from `REGISTRY_PASSWORD`

### MongoDB Configuration

- **Connection**: Uses MongoDB connection string with replica set support
- **Authentication**: Supports both environment variables and Kubernetes secrets
- **Collections**: Works with `environments_v2`, `environment_revisions`, `models`, `model_versions`
- **Error handling**: Graceful fallback and retry mechanisms

### Registry Access Requirements

- **Password**: Optional - can be provided via:
  - Command-line argument: `python delete_image.py <password>`
  - Environment variable: `export REGISTRY_PASSWORD="your_password"`
  - Not required for ECR (automatic authentication)
  - Not required for registries without authentication
- **ECR**: Automatic authentication for AWS ECR registries using AWS CLI
- **Authentication**: Uses `domino-registry` username with provided password
- **Permissions**: Requires read access for analysis, delete permissions for cleanup
- **Network**: Must be accessible from both local machine and Kubernetes pods

## 📊 Sample Output

### Freed Space Calculation (Shared Layer Aware)
```json
{
  "summary": {
    "freed_space_bytes": 1073741824,
    "freed_space_mb": 1024.0,
    "freed_space_gb": 1.0,
    "total_matching_tags": 45
  }
}
```

The tool properly calculates freed space by:
- Analyzing all layers across images
- Tracking reference counts for each layer
- Only counting layers that would have zero references after deletion
- Avoiding overestimation from shared layers

### Workload Analysis
```json
{
  "summary": {
    "total_pods": 45,
    "total_images": 23,
    "unique_tags": 15
  },
  "image_tags": {
    "62798b9bee0eb12322fc97e8-v1-202419163323": {
      "tag": "62798b9bee0eb12322fc97e8-v1-202419163323",
      "count": 3,
      "pods": ["model-abc123", "model-def456", "model-ghi789"],
      "workload_count": 3
    }
  }
}
```

### Archived Tags Analysis
```json
{
  "summary": {
    "total_archived_object_ids": 25,
    "archived_environment_ids": 10,
    "archived_revision_ids": 15,
    "total_matching_tags": 45,
    "freed_space_gb": 12.5,
    "tags_by_image_type": {
      "environment": 20,
      "model": 25
    }
  }
}
```

### Deactivated User Environments
```json
{
  "summary": {
    "total_deactivated_users": 5,
    "total_environment_ids": 12,
    "total_revision_ids": 24,
    "total_matching_tags": 36,
    "freed_space_gb": 8.3
  },
  "grouped_by_user": {
    "user@example.com": {
      "user_id": "507f1f77bcf86cd799439011",
      "tag_count": 15,
      "environment_count": 5,
      "environments": ["env1", "env2", ...]
    }
  }
}
```

## 🚨 Troubleshooting

### Common Issues

**Kubernetes API access permissions**
```bash
# Check kubectl access
kubectl get pods -n domino-compute
```

**Registry authentication**
```bash
# Verify registry access
export REGISTRY_PASSWORD="your_password"
skopeo list-tags docker://registry.example.com/repository
```

**Keycloak authentication**
```bash
# Verify Keycloak access
export KEYCLOAK_HOST="https://keycloak.example.com/auth/"
export KEYCLOAK_USERNAME="admin"
export KEYCLOAK_PASSWORD="your_password"
```

**ObjectID file format errors**
```bash
# Ensure ObjectID file has correct format
# Valid: 62798b9bee0eb12322fc97e8
# Valid: environment:62798b9bee0eb12322fc97e8
# Valid: model:62798b9bee0eb12322fc97e8
# Invalid: 62798b9bee0eb12322fc97e (23 chars)
# Invalid: 62798b9bee0eb12322fc97eg (contains 'g')
```

**MongoDB connection issues**
```bash
# Check MongoDB connectivity
export MONGODB_PASSWORD="your_password"
python -c "from python.mongo_utils import get_mongo_client; print('MongoDB connection successful')"
```

**Transaction safety verification**
```bash
# Check logs for transaction safety messages
# Look for: "MongoDB record will NOT be cleaned" for failed Docker deletions
# Look for: "Skipping MongoDB cleanup for X ObjectIDs due to Docker deletion failures"
```

### Debug Mode
```bash
# Enable verbose logging
export PYTHONPATH=python
python python/main.py inspect_workload --max-workers 1
```

## 📝 Requirements

### Python Dependencies
- `boto3` - AWS SDK for S3 backup/restore functionality
- `kubernetes` - Kubernetes API client
- `pandas` - Data analysis and manipulation
- `pymongo` - MongoDB client
- `python-keycloak` - Keycloak admin client
- `PyYAML` - Configuration parsing
- `requests` - HTTP client

### System Requirements
- **kubectl access** - For initial setup and pod management
- **Pod exec permissions** - For running Skopeo commands
- **Registry access** - For image inspection and deletion
- **MongoDB access** - For metadata extraction and cleanup
- **Keycloak access** - For deactivated user detection (optional)
- **Python 3.7+** - For script execution

## 🤝 Contributing

1. **Fork the repository**
2. **Create a feature branch**
3. **Make your changes**
4. **Add tests if applicable**
5. **Submit a pull request**

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

