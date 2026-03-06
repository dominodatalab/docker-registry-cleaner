# Safety Features and Troubleshooting

## Safety Features

### Default Dry-Run

All deletion commands run in dry-run mode by default. Pass `--apply` to actually delete. Use `--force` to skip the confirmation prompt.

### Real-Time Usage Check

Immediately before any deletion, each script performs a live MongoDB query to confirm the image is still unused. This catches:
- New runs or workspaces started after the initial analysis
- Race conditions between analysis and deletion time

Any image found to be in use at deletion time is automatically skipped and logged.

### Transaction Safety

- Docker images are deleted before MongoDB records
- MongoDB records are only deleted after a successful Docker deletion
- If Docker deletion fails, the MongoDB record is preserved
- Registry deletion mode is automatically disabled after operations (even on error)

### Shared Layer Awareness

Space calculations account for layers shared between images. Only layers that would have zero remaining references after deletion are counted as freed space.

### MongoDB Cleanup Warning

`--mongo-cleanup` flags are high-risk. Environments and models link to many other Domino assets (projects, runs, workspaces, scheduler jobs, app versions, user preferences). Start with Docker-only cleanup first. Only add `--mongo-cleanup` when you understand the full impact and have recent MongoDB backups.

## Registry Auto-Detection

Scripts automatically detect whether the Docker registry is running in-cluster and enable deletion mode accordingly. If auto-detection fails:

```bash
# Enable with the default "docker-registry" statefulset
docker-registry-cleaner delete_archived_tags --environment --apply --enable-docker-deletion

# Specify a custom statefulset name
docker-registry-cleaner delete_unused_environments --apply \
  --enable-docker-deletion \
  --registry-statefulset my-custom-registry
```

Use this when:
- The registry URL is an IP address or external DNS name
- The registry service has non-standard naming
- You want explicit control over which StatefulSet/Deployment is modified

## How Image Analysis Works

1. Lists all image tags in the Docker registry
2. Inspects image layers and calculates sizes
3. Detects shared layers across images
4. Tracks reference counts for accurate freed-space calculation
5. Cross-references MongoDB usage data to identify unused images

## Troubleshooting

### Registry Authentication Errors

```bash
export REGISTRY_PASSWORD="your_password"
skopeo list-tags docker://registry.example.com/repository
```

See [configuration.md](configuration.md) for full authentication setup.

### MongoDB Connection Errors

```bash
export MONGODB_PASSWORD="your_password"
python -c "from utils.mongo_utils import get_mongo_client; print('Connected')"
```

For local development, make sure port-forwarding is active:

```bash
kubectl port-forward -n domino-platform svc/mongodb-replicaset 27017:27017 &
```

### Health Check

Run this to diagnose connectivity issues before running deletions:

```bash
docker-registry-cleaner health_check
```

### Invalid ObjectID

```bash
# Valid: 24 hex characters
62798b9bee0eb12322fc97e8

# Invalid: 23 characters
62798b9bee0eb12322fc97e
```

### Dry-Run Shows Different Numbers on Re-Run

See [Understanding Report Numbers](backup-restore.md#understanding-report-numbers) for an explanation.
