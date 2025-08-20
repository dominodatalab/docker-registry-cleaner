# Docker Registry Cleaner

An intelligent Docker registry cleanup tool that analyzes workload usage patterns and safely removes unused images while preserving actively used ones.

## 🎯 Overview

This project provides a comprehensive solution for cleaning up Docker registries by:

1. **Analyzing current workload usage** - Identifies which images are actively used by running Kubernetes pods
2. **Analyzing registry contents** - Maps image layers, sizes, and tag distributions
3. **Intelligent deletion** - Safely removes unused images while preserving all actively used ones
4. **ObjectID filtering** - Target specific models/environments using MongoDB ObjectIDs

## 🏗️ Architecture

The project consists of several Python scripts that work together:

- **`python/main.py`** - Unified entrypoint for all operations
- **`python/inspect_workload.py`** - Analyzes Kubernetes workloads and running pods
- **`python/image_data_analysis.py`** - Analyzes Docker registry contents and layer information
- **`python/delete_image.py`** - Intelligently deletes unused images based on workload analysis
- **`python/find_archived_env_tags.py`** - Finds Docker tags associated with archived environments in Mongo

## 🔍 ObjectID Filtering

The tool supports filtering by MongoDB ObjectIDs to target specific models or compute environments:

- **Image tags** contain 24-character MongoDB ObjectIDs as prefixes
- **Filter by ObjectIDs** to analyze/delete only specific models/environments
- **File-based input** - Read ObjectIDs from a file (first column contains ObjectIDs)
- **Validation** ensures ObjectIDs are 24 characters and valid hexadecimal
- **Cross-script support** - filtering works across all analysis and deletion scripts

### ObjectID File Format

The file should contain ObjectIDs in the first column, with additional data in subsequent columns:

```
62798b9bee0eb12322fc97e8 31 30
6286a3c76d4fd0362f8ba3ec 13 12 9
627d94043035a63be6140e93 10
```

ObjectIDs must be:
- **24 characters long** (standard MongoDB ObjectID length)
- **Valid hexadecimal** (0-9, a-f)
- **First column** in the file (additional columns are ignored)

Example file: `environments` - Contains ObjectIDs and revision numbers

## 🚀 Quick Start

### Prerequisites

1. **Kubernetes Access**: kubectl configured and access to the cluster
2. **Registry Access**: Password for the Docker registry
3. **Python Dependencies**: Install required packages

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
```

### Basic Workflow

```bash
# 1. Analyze current workload (uses config.yaml defaults)
python python/main.py inspect_workload

# 2. Analyze registry contents (uses config.yaml defaults)
python python/main.py image_data_analysis

# 3. Intelligent deletion (dry run first)
python python/main.py delete_image mypassword

# 4. Actually delete unused images
python python/main.py delete_image mypassword --apply
```

### ObjectID Filtering Examples

```bash
# Filter by ObjectIDs from file (first column contains ObjectIDs)
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
export REGISTRY_URL="registry.example.com"
export REPOSITORY="my-repo"
export REGISTRY_PASSWORD="your_password"
export PLATFORM_NAMESPACE="domino-platform"
export COMPUTE_NAMESPACE="domino-compute"
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

**Output**: `reports/workload-report.json` - Maps image tags to pod usage information

### 2. Image Analysis (`image_data_analysis.py`)

Analyzes the Docker registry to understand image composition:

- **Lists all image tags** in specified repositories
- **Inspects image layers** and calculates sizes
- **Maps tag distributions** across layers
- **ObjectID filtering** - Only analyzes images matching provided ObjectIDs from file

**Output**: `reports/final-report.json` - Detailed layer analysis with size and tag information

### 3. Intelligent Deletion (`delete_image.py`)

Safely removes unused images based on comprehensive analysis:

- **Cross-references** workload and image analysis reports
- **Identifies unused images** that aren't referenced by running pods
- **Calculates space savings** for each deletion
- **ObjectID filtering** - Only considers images matching provided ObjectIDs from file
- **Dry-run by default** for safety
- **Confirmation prompts** before actual deletion

**Output**: `reports/deletion-analysis.json` - Summary of what would be deleted and space saved

### 4. Find Archived Environment Tags (`find_archived_env_tags.py`)

Queries Mongo `environments_v2` for archived records and reports matching tags in the registry.

**Output**: `reports/archived-tags.json`

## 🛡️ Safety Features

### Default Safety Mode
- **Dry-run by default** - No images are deleted unless `--apply` is specified
- **Confirmation prompts** - User must confirm before actual deletion
- **Force mode** - Skip confirmation with `--force` flag

### Intelligent Analysis
- **Workload-aware** - Only deletes images not used by running pods
- **Layer analysis** - Understands image composition and dependencies
- **Cross-validation** - Multiple data sources ensure accuracy

### ObjectID Targeting
- **Precise targeting** - Only process specific models/environments
- **File-based input** - Read ObjectIDs from files for easy management
- **Validation** - Ensures ObjectIDs are properly formatted
- **Reduced risk** - Smaller scope means fewer unintended deletions

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
# Override defaults
python python/main.py inspect_workload --registry-url registry.example.com --prefix-to-remove registry.example.com/
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

### Mongo Cleanup

After deleting images, you can optionally clean up related Mongo records for environment revisions:

```bash
# Dry run (find matching records)
python python/main.py mongo_cleanup find --file python/to_delete.txt

# Delete matching records
python python/main.py mongo_cleanup delete --file python/to_delete.txt
```

Requirements:
- Set `MONGODB_PASSWORD` in the environment
- The file should contain repo/image:tag or tags (one per line); only first token per line is read

## 📁 Project Structure

```
docker-registry-cleaner/
├── requirements.txt                  # Python dependencies
├── config.yaml                       # Configuration defaults
├── environments                      # ObjectID file (first column contains ObjectIDs)
├── python/
│   ├── main.py                      # Unified entrypoint
│   ├── inspect_workload.py          # Kubernetes workload analysis
│   ├── image_data_analysis.py       # Registry content analysis
│   ├── delete_image.py              # Intelligent image deletion
│   ├── find_archived_env_tags.py    # Archived env tag discovery
│   ├── config_manager.py            # Configuration management
│   └── report_utils.py              # Report helpers
└── reports/                         # Analysis output files
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

security:
  dry_run_by_default: true
  require_confirmation: true
```

### Environment Variables
- `REGISTRY_URL` - Docker registry URL
- `REPOSITORY` - Repository name
- `REGISTRY_PASSWORD` - Registry password (required for Skopeo operations)
- `PLATFORM_NAMESPACE` - Domino platform namespace
- `COMPUTE_NAMESPACE` - Compute namespace

### Skopeo Configuration

The tool uses a standardized `SkopeoClient` that provides consistent authentication and configuration across all scripts:

- **Authentication**: Uses `--creds domino-registry:{password}` for all operations
- **TLS**: Disabled with `--tls-verify=false` for internal registries
- **Execution modes**: 
  - **Local mode**: Direct subprocess calls (used by `image_data_analysis.py`)
  - **Pod mode**: Kubernetes pod execution (used by `delete_image.py`)
- **Centralized config**: All Skopeo operations use the same credentials from `REGISTRY_PASSWORD`

### Registry Access Requirements

- **Password**: Must be set via `REGISTRY_PASSWORD` environment variable
- **Authentication**: Uses `domino-registry` username with provided password
- **Permissions**: Requires read access for analysis, delete permissions for cleanup
- **Network**: Must be accessible from both local machine and Kubernetes pods

## 📊 Sample Output

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

### Image Analysis
```json
{
  "layers": {
    "sha256:abc123...": {
      "size": 1048576,
      "tags": ["62798b9bee0eb12322fc97e8-v1-202419163323"],
      "environments": ["62798b9bee0eb12322fc97e8"]
    }
  }
}
```

### Deletion Analysis
```json
{
  "summary": {
    "total_images_analyzed": 150,
    "used_images": 23,
    "unused_images": 127,
    "total_size_saved": 1073741824
  },
  "unused_images": [
    {
      "tag": "6286a3c76d4fd0362f8ba3ec-v1-202419163323",
      "size": 52428800,
      "layer_id": "sha256:def456...",
      "status": "unused"
    }
  ]
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

**ObjectID file format errors**
```bash
# Ensure ObjectID file has correct format
# Valid: 62798b9bee0eb12322fc97e8 31 30
# Invalid: 62798b9bee0eb12322fc97e (23 chars)
# Invalid: 62798b9bee0eb12322fc97eg (contains 'g')
```

**Pod execution errors**
```bash
# Check if Skopeo pod can be created
kubectl apply -f pod.yaml -n domino-platform
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
- `tabulate` - Pretty table formatting
- `tqdm` - Progress bars
- `PyYAML` - Configuration parsing
- `concurrent.futures` - Parallel processing

### System Requirements
- **kubectl access** - For initial setup and pod management
- **Pod exec permissions** - For running Skopeo commands
- **Registry access** - For image inspection and deletion
- **Python 3.7+** - For script execution

## 🤝 Contributing

1. **Fork the repository**
2. **Create a feature branch**
3. **Make your changes**
4. **Add tests if applicable**
5. **Submit a pull request**

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
