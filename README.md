# Docker Registry Cleaner

Cleans up unused Docker images from Domino's registry by analyzing MongoDB to see what's actually in use.

- Maps image layers, sizes, and tags (understands shared layers between images)
- Checks MongoDB collections (runs, workspaces, models, projects, scheduler_jobs, etc.) to identify actively used images
- Deletes unused images with optional S3 backup
- Resumes after failures using checkpoint files
- Can optionally clean up MongoDB records for deleted images

> **Warning:** MongoDB cleanup is risky. Environments and models link to many other Domino assets. Start with Docker-only cleanup first. Use `--mongo-cleanup` flags only when you understand the impact.

## Installation

### Helm (Recommended)

```bash
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform
```

```bash
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- docker-registry-cleaner --help
```

See the [Helm Chart README](charts/docker-registry-cleaner/README.md) for configuration options.

### Local (Development)

```bash
pip install -e .
```

Requires port-forwarding to cluster-internal services:

```bash
kubectl port-forward -n domino-platform svc/mongodb-replicaset 27017:27017 &
kubectl port-forward -n domino-platform svc/docker-registry 5000:5000 &
kubectl port-forward -n domino-platform svc/keycloak-http 8080:80 &
```

Update `config.yaml` to use localhost for registry, MongoDB, and Keycloak endpoints.

### Command Conventions

Commands shown below assume you're running in the Helm-deployed pod. For local development, use `python python/main.py` instead of `docker-registry-cleaner`.

## Web UI

Docker Registry Cleaner includes a web interface for browsing and analyzing reports. It is read-only — destructive operations must still be run via `kubectl exec`.

```bash
kubectl port-forward -n domino-platform svc/docker-registry-cleaner-frontend 8080:8080
```

See [docs/web-ui.md](docs/web-ui.md) for access options, Helm configuration, and local development setup.

## Quick Start

```bash
# Check connectivity
docker-registry-cleaner health_check

# Dry-run: see what would be deleted
docker-registry-cleaner delete_archived_tags --environment

# Delete with S3 backup
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-bucket
```

## Commands

### Deletion

| Command | Description | Docs |
|---------|-------------|------|
| `delete_archived_tags` | Delete images for archived environments and/or models | [docs](docs/delete_archived_tags.md) |
| `delete_unused_environments` | Delete images for environments not used anywhere in Domino | [docs](docs/delete_unused_environments.md) |
| `delete_old_revisions` | Delete old environment revisions, keeping the N most recent per environment | [docs](docs/delete_old_revisions.md) |
| `archive_unused_environments` | Mark unused environments as archived in MongoDB (no Docker changes) | [docs](docs/archive_unused_environments.md) |
| `delete_unused_private_environments` | Delete environments owned by deactivated Keycloak users | [docs](docs/delete_unused_private_environments.md) |
| `delete_all_unused_environments` | Run all unused environment cleanup steps in sequence | [docs](docs/delete_all_unused_environments.md) |
| `delete_unused_references` | Remove MongoDB records referencing non-existent Docker images | [docs](docs/delete_unused_references.md) |
| `delete_image` | Delete a specific image or analyze/delete unused images from reports | [docs](docs/delete_image.md) |

### Analysis

| Command | Description | Docs |
|---------|-------------|------|
| `health_check` | Verify connectivity to registry, MongoDB, Kubernetes, and S3 | [docs](docs/reports.md#health_check) |
| `reports` | Generate MongoDB usage reports | [docs](docs/reports.md#reports) |
| `image_size_report` | Report of largest images by total size and potential freed space | [docs](docs/reports.md#image_size_report) |
| `user_size_report` | Report of registry space usage grouped by user | [docs](docs/reports.md#user_size_report) |
| `find_environment_usage` | Show all places a specific environment is used | [docs](docs/find_environment_usage.md) |
| `run_registry_gc` | Run Docker registry garbage collection (internal registries only) | [docs](docs/reports.md#run_registry_gc) |
| `reset_default_environments` | Unset default environment references in MongoDB | [docs](docs/reports.md#reset_default_environments) |

## Common Options

All deletion commands support:

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually perform deletions (dry-run without this) | `false` |
| `--force` | Skip confirmation prompts | `false` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups | From config |
| `--generate-reports` | Force regeneration of analysis reports | `false` |
| `--resume` | Resume an interrupted operation from checkpoint | `false` |
| `--enable-docker-deletion` | Override registry in-cluster auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |

## Further Reading

- [Configuration](docs/configuration.md) — config.yaml, environment variables, registry authentication
- [Backup, Restore & Resume](docs/backup-restore.md) — S3 backup, restore, checkpoints, timestamped reports
- [ObjectID Filtering](docs/objectid-filtering.md) — target specific environments or models by ID
- [Safety & Troubleshooting](docs/safety-and-troubleshooting.md) — safety guarantees, how analysis works, common issues
- [ACR Authentication](docs/acr-authentication.md) — Azure Container Registry managed identity setup
- [Helm Chart README](charts/docker-registry-cleaner/README.md) — full Helm configuration reference

## System Requirements

- Python 3.11+
- `kubectl` access for Kubernetes operations
- Docker registry access for image inspection and deletion
- MongoDB access for metadata and cleanup
- Keycloak access for deactivated user detection (optional)

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License — see the LICENSE file for details.
