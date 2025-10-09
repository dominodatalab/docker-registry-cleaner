# Docker Registry Cleaner

An intelligent Docker registry cleanup tool that analyzes workload usage patterns and safely removes unused images while preserving actively used ones.

## 🎯 Overview

This project provides a comprehensive solution for cleaning up Docker registries by:

1. **Analyzing current workload usage** - Identifies which images are actively used by running Kubernetes pods
2. **Analyzing registry contents** - Maps image layers, sizes, and tag distributions with shared layer awareness
3. **Intelligent deletion** - Safely removes unused images while preserving all actively used ones
4. **ObjectID filtering** - Target specific models/environments using MongoDB ObjectIDs
5. **MongoDB integration** - Find and clean up archived environments, models, and orphaned records
6. **Unused reference detection** - Identify and remove MongoDB records referencing non-existent Docker images
7. **Deactivated user cleanup** - Find and delete private environments owned by deactivated Keycloak users
8. **Unused environment detection** - Find and delete environments not used in workspaces, models, or as project defaults
9. **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images

## 🏗️ Architecture

The project consists of several Python scripts that work together:

- **`python/main.py`** - Unified entrypoint for all operationsning pods (uses config defaults)
- **`python/config_manager.py`** - Centralized configuration and Skopeo client management
- **`python/delete_archived_env_tags.py`** - Finds and deletes Docker tags associated with archived environments
- **`python/delete_archived_model_tags.py`** - Finds and deletes Docker tags associated with archived models
- **`python/delete_image.py`** - Intelligently deletes unused images based on workload analysis
- **`python/delete_unused_environments.py`** - Finds and deletes environments not used in workspaces, models, or as project defaults (auto-generates reports)
- **`python/delete_unused_private_env_tags.py`** - Finds and deletes private environments owned by deactivated Keycloak users
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

The tool uses `config.yaml` for default settings. Create or modify this file:

```yaml
# config.yaml (key excerpts)
registry:
  url: "docker-registry:5000"
  repository: "dominodatalab"

kubernetes:
  platform_namespace: "domino-platform"
  compute_namespace: "domino-compute"
  pod_prefixes: ["model-", "run-"]

mongo:
  host: "mongodb-replicaset"
  port: 27017
  replicaset: "rs0"
  db: "domino"

analysis:
  max_workers: 4
  timeout: 300
  output_dir: "reports"

reports:
  workload_report: "workload-report.json"
  image_analysis: "final-report.json"
  deletion_analysis: "deletion-analysis.json"
  tags_per_layer: "tags-per-layer.json"
  layers_and_sizes: "layers-and-sizes.json"
  filtered_layers: "filtered-layers.json"
  tag_sums: "tag-sums.json"
  images_report: "images-report"
  archived_tags: "archived-tags.json"
  archived_model_tags: "archived-model-tags.json"
  unused_references: "unused-references.json"

security:
  dry_run_by_default: true
  require_confirmation: true
```

### Basic Workflow

```bash
# 1. Analyze current workload (uses config.yaml defaults, all flags optional)
python python/main.py inspect_workload

# 2. Analyze registry contents (uses config.yaml defaults, all flags optional)
python python/main.py image_data_analysis

# 3. Generate tag usage reports (auto-generates all required data)
python python/main.py reports

# 4. Intelligent deletion (dry run first)
python python/main.py delete_image mypassword

# 5. Actually delete unused images
python python/main.py delete_image mypassword --apply
```

### Cleanup Commands

```bash
# Find archived environment tags (dry-run)
python python/main.py delete_archived_env_tags

# Delete archived environment tags directly
python python/main.py delete_archived_env_tags --apply

# Find archived model tags (dry-run)
python python/main.py delete_archived_model_tags

# Delete archived model tags directly
python python/main.py delete_archived_model_tags --apply

# Find unused MongoDB references (dry-run)
python python/main.py delete_unused_references

# Delete unused MongoDB references directly
python python/main.py delete_unused_references --apply

# Find private environments owned by deactivated users (dry-run)
python python/main.py delete_unused_private_env_tags

# Delete private environments owned by deactivated users
python python/main.py delete_unused_private_env_tags --apply

# Find unused environments (auto-generates required reports if missing)
python python/main.py delete_unused_environments

# Force regeneration of metadata before analysis
python python/main.py delete_unused_environments --generate-reports

# Delete unused environments (with confirmation)
python python/main.py delete_unused_environments --apply

# Full workflow: generate reports and delete
python python/main.py delete_unused_environments --generate-reports --apply
```

### ObjectID Filtering Examples

```bash
# Filter by ObjectIDs from file (supports typed format)
python python/main.py inspect_workload --file environments
python python/main.py image_data_analysis --file environments
python python/main.py delete_image mypassword --file environments

# Combine with other options
python python/main.py inspect_workload --file environments --namespace my-namespace
python python/main.py delete_image mypassword --file environments --apply --force
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

### 4. Archived Environment Tags (`delete_archived_env_tags.py`)

Finds and optionally deletes Docker tags associated with archived environments:

- **Queries MongoDB** `environments_v2` collection for archived environments (`isArchived == true`)
- **Finds related revisions** in `environment_revisions` collection
- **Scans Docker registry** for tags containing archived ObjectIDs
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly (no subprocess overhead)
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Two-phase workflow** - Generate report first, then delete from report or directly

**Output**: `reports/archived-tags.json` - Detailed report with freed space calculation

### 5. Archived Model Tags (`delete_archived_model_tags.py`)

Finds and optionally deletes Docker tags associated with archived models:

- **Queries MongoDB** `models` collection for archived models (`isArchived == true`)
- **Finds related versions** in `model_versions` collection
- **Scans Docker registry** for tags containing archived ObjectIDs
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly (no subprocess overhead)
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Separate collection cleanup** - Handles both `models` and `model_versions` collections
- **Two-phase workflow** - Generate report first, then delete from report or directly

**Output**: `reports/archived-model-tags.json` - Detailed report with freed space calculation

### 6. Unused References (`delete_unused_references.py`)

Finds and optionally deletes MongoDB records referencing non-existent Docker images:

- **Queries MongoDB collections** for Docker image references
- **Checks Docker registry** to verify image existence
- **Identifies orphaned records** in MongoDB
- **Multi-collection support** - Works with `environment_revisions`, `model_versions`, etc.
- **Optional deletion** of MongoDB records with confirmation
- **Two-phase workflow** - Generate report first, then delete

**Output**: `reports/unused-references.json` - Detailed report of unused MongoDB references

### 7. Deactivated User Private Environments (`delete_unused_private_env_tags.py`)

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

### 8. Unused Environments (`delete_unused_environments.py`)

Finds and optionally deletes environments that are not being used anywhere:

- **Auto-generates required reports** - Automatically runs `extract_metadata.py` and `inspect_workload.py` if needed
- **Loads metadata** from auto-generated outputs (model and workspace environment usage)
- **Loads workload data** from Kubernetes pod inspection (running containers)
- **Queries MongoDB** for all non-archived environments and project defaults
- **Identifies unused environments** - Environments NOT in:
  - Model environment usage
  - Workspace environment usage  
  - Running workload pods
  - Project default environments
- **Scans Docker registry** for tags containing these unused environment ObjectIDs
- **Calculates freed space** with shared layer awareness
- **Direct deletion** - Uses skopeo client directly
- **Transaction safety** - Only deletes MongoDB records for successfully deleted Docker images
- **Separate collection cleanup** - Handles both `environments_v2` and `environment_revisions`
- **Two-phase workflow** - Generate report first, then delete

**Auto-generation**: Use `--generate-reports` to force regeneration of all required metadata

**Output**: `reports/unused-environments.json` - Detailed report of unused environments

### 9. Tag Usage Reports (`reports.py`)

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
# Analyze everything
python python/main.py inspect_workload
python python/main.py image_data_analysis
python python/main.py delete_image mypassword
```

### Targeted Analysis (Specific ObjectIDs from File)
```bash
# Analyze only specific models/environments
python python/main.py inspect_workload --file environments
python python/main.py image_data_analysis --file environments
python python/main.py delete_image mypassword --file environments
```

### Custom Configuration
```bash
# Override defaults (all flags optional, uses config.yaml if not specified)
python python/main.py inspect_workload --registry-url registry.example.com
python python/main.py image_data_analysis --registry-url registry.example.com --repository my-repo
```

### Deletion Modes
```bash
# Safe dry-run (default)
python python/main.py delete_image mypassword

# With confirmation
python python/main.py delete_image mypassword --apply

# Force deletion (no confirmation)
python python/main.py delete_image mypassword --apply --force
```

### Archive Management
```bash
# Find archived environment tags (dry-run with space calculation)
python python/main.py delete_archived_env_tags --output archived-env-tags.json

# Delete archived environment tags directly
python python/main.py delete_archived_env_tags --apply

# Delete from pre-generated file
python python/main.py delete_archived_env_tags --apply --input archived-env-tags.json

# Find archived model tags (dry-run with space calculation)
python python/main.py delete_archived_model_tags --output archived-model-tags.json

# Delete archived model tags directly
python python/main.py delete_archived_model_tags --apply
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
python python/main.py delete_unused_private_env_tags --output deactivated-user-envs.json

# Delete private environments owned by deactivated users
python python/main.py delete_unused_private_env_tags --apply

# Delete from pre-generated file
python python/main.py delete_unused_private_env_tags --apply --input deactivated-user-envs.json

# Force deletion without confirmation
python python/main.py delete_unused_private_env_tags --apply --force
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

# Full workflow: generate reports and delete
python python/main.py delete_unused_environments --generate-reports --apply

# Delete from pre-generated file
python python/main.py delete_unused_environments --apply --input unused-envs.json

# Force deletion without confirmation
python python/main.py delete_unused_environments --apply --force
```

### Mongo Cleanup

After deleting images, you can optionally clean up related Mongo records:

```bash
# Dry run (find matching records)
python python/main.py mongo_cleanup find --file python/to_delete.txt

# Delete matching records
python python/main.py mongo_cleanup delete --file python/to_delete.txt
```

Requirements:
- Set `MONGODB_PASSWORD` in the environment (or it will use Kubernetes secrets)
- The file should contain repo/image:tag or tags (one per line); only first token per line is read

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
├── config.yaml                          # Configuration defaults
├── environments                          # ObjectID file (typed format)
├── environments-example                  # Example ObjectID file format
├── requirements.txt                     # Python dependencies
├── mongo_queries/                       # Legacy MongoDB query files
├── python/
│   ├── main.py                          # Unified entrypoint
│   ├── config_manager.py                # Configuration and Skopeo client management
│   ├── delete_archived_env_tags.py      # Archived env tag discovery & deletion
│   ├── delete_archived_model_tags.py    # Archived model tag discovery & deletion
│   ├── delete_image.py                  # Intelligent image deletion
│   ├── delete_unused_environments.py    # Unused environment cleanup (auto-generates reports)
│   ├── delete_unused_private_env_tags.py # Deactivated user private env cleanup
│   ├── delete_unused_references.py      # Unused MongoDB reference cleanup
│   ├── extract_metadata.py              # MongoDB metadata extraction
│   ├── image_data_analysis.py           # Registry content analysis with shared layer detection
│   ├── inspect_workload.py              # Kubernetes workload analysis
│   ├── logging_utils.py                 # Logging configuration
│   ├── mongo_cleanup.py                 # MongoDB record cleanup
│   ├── mongo_utils.py                   # MongoDB utilities
│   ├── object_id_utils.py               # ObjectID handling utilities
│   ├── report_utils.py                  # Report helpers
│   └── reports.py                       # Tag usage report generator (auto-generates metadata)
└── reports/                             # Analysis output files
```

## 🔧 Configuration

### config.yaml
```yaml
# Docker Registry Cleaner Configuration
registry:
  url: "docker-registry:5000"
  repository: "dominodatalab"

kubernetes:
  platform_namespace: "domino-platform"
  compute_namespace: "domino-compute"
  pod_prefixes: ["model-", "run-"]

mongo:
  host: "mongodb-replicaset"
  port: 27017
  replicaset: "rs0"
  db: "domino"

analysis:
  max_workers: 4
  timeout: 300
  output_dir: "reports"

reports:
  workload_report: "workload-report.json"
  image_analysis: "final-report.json"
  deletion_analysis: "deletion-analysis.json"
  tags_per_layer: "tags-per-layer.json"
  layers_and_sizes: "layers-and-sizes.json"
  filtered_layers: "filtered-layers.json"
  tag_sums: "tag-sums.json"
  images_report: "images-report"
  archived_tags: "archived-tags.json"
  archived_model_tags: "archived-model-tags.json"
  unused_references: "unused-references.json"

security:
  dry_run_by_default: true
  require_confirmation: true
```

### Environment Variables

**Docker Registry:**
- `REGISTRY_URL` - Docker registry URL
- `REPOSITORY` - Repository name
- `REGISTRY_PASSWORD` - Registry password (optional - ECR auto-auth if not set)

**Kubernetes:**
- `PLATFORM_NAMESPACE` - Domino platform namespace
- `COMPUTE_NAMESPACE` - Compute namespace

**MongoDB:**
- `MONGODB_USERNAME` - MongoDB username (optional, defaults to 'admin')
- `MONGODB_PASSWORD` - MongoDB password (optional - uses K8s secrets if not set)

**Keycloak (for deactivated user cleanup):**
- `KEYCLOAK_HOST` - Keycloak server URL (e.g., `https://keycloak.example.com/auth/`)
- `KEYCLOAK_USERNAME` - Keycloak admin username
- `KEYCLOAK_PASSWORD` - Keycloak admin password

### Skopeo Configuration

The tool uses a standardized `SkopeoClient` that provides consistent authentication and configuration across all scripts:

- **Authentication**: Uses `--creds domino-registry:{password}` for all operations
- **ECR Support**: Automatic ECR authentication if registry URL contains "amazonaws.com"
- **TLS**: Deactivated with `--tls-verify=false` for internal registries
- **Execution modes**: 
  - **Local mode**: Direct subprocess calls (used by most scripts)
  - **Pod mode**: Kubernetes pod execution (used by `delete_image.py`)
- **Centralized config**: All Skopeo operations use the same credentials from `REGISTRY_PASSWORD`

### MongoDB Configuration

- **Connection**: Uses MongoDB connection string with replica set support
- **Authentication**: Supports both environment variables and Kubernetes secrets
- **Collections**: Works with `environments_v2`, `environment_revisions`, `models`, `model_versions`
- **Error handling**: Graceful fallback and retry mechanisms

### Registry Access Requirements

- **Password**: Optional - can be set via `REGISTRY_PASSWORD` environment variable
- **ECR**: Automatic authentication for AWS ECR registries
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
- `kubernetes` - Kubernetes API client
- `pandas` - Data analysis and manipulation
- `pymongo` - MongoDB client
- `python-keycloak` - Keycloak admin client
- `PyYAML` - Configuration parsing
- `requests` - HTTP client
- `tabulate` - Pretty table formatting
- `tqdm` - Progress bars

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
