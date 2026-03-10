# delete_all_unused_environments

Runs a comprehensive unused environment cleanup by executing multiple deletion scripts in sequence.

## Steps Executed

1. [`delete_unused_environments`](delete_unused_environments.md) — environments not used in workspaces, models, or project defaults
2. [`delete_unused_private_environments`](delete_unused_private_environments.md) — private environments owned by deactivated Keycloak users

Optionally followed by:

3. [`run_registry_gc`](reports.md#run_registry_gc) — Docker registry garbage collection (internal registries only)

## Usage

```bash
# Dry-run all unused environments
docker-registry-cleaner delete_all_unused_environments

# Delete all with S3 backup
docker-registry-cleaner delete_all_unused_environments --apply --backup --s3-bucket my-bucket

# Force-regenerate reports before analysis
docker-registry-cleaner delete_all_unused_environments --generate-reports --apply
```

## Options

Accepts the same options as the individual scripts it wraps. Key options:

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompts | `false` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups | From config |
| `--generate-reports` | Force regeneration of usage reports | `false` |

## Notes

- Runs the individual cleanup scripts sequentially, not in parallel.
- If you only want one of the steps, run the individual script directly.
- Keycloak access is required for the private environments step (see [`delete_unused_private_environments`](delete_unused_private_environments.md)).
