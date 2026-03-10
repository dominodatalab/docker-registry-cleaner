# delete_unused_private_environments

Finds and optionally deletes Docker images for private environments owned by deactivated Keycloak users.

## How It Works

1. Queries Keycloak for all deactivated (disabled) user accounts.
2. Finds environments in MongoDB `environments_v2` owned by those users.
3. Scans the Docker registry for tags matching those environment IDs.
4. Performs a real-time usage check — any image still in use is automatically skipped.
5. Optionally deletes matched Docker images and cleans up MongoDB records.

## Usage

```bash
# Dry-run: find private environments belonging to deactivated users
docker-registry-cleaner delete_unused_private_environments

# Delete with S3 backup
docker-registry-cleaner delete_unused_private_environments --apply --backup --s3-bucket my-bucket

# Delete from a pre-generated report (skips re-analysis)
docker-registry-cleaner delete_unused_private_environments --apply --input reports/deactivated-user-envs.json

# Resume an interrupted deletion
docker-registry-cleaner delete_unused_private_environments --apply --resume
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--input FILE` | Delete from a pre-generated report file | — |
| `--output FILE` | Output path for the analysis report | `reports/deactivated-user-envs.json` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups | From config |
| `--region REGION` | AWS region for S3/ECR | `us-west-2` |
| `--mongo-cleanup` | Also delete MongoDB records after Docker deletion | `false` |
| `--run-registry-gc` | Run registry garbage collection after deletion (internal registries only) | `false` |
| `--resume` | Resume from a previous checkpoint | `false` |
| `--operation-id ID` | ID for checkpoint management | — |
| `--enable-docker-deletion` | Override registry in-cluster auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |
| `--registry-url URL` | Docker registry URL | From config |
| `--repository REPO` | Repository name | From config |

## Notes

- Requires Keycloak access. Configure `KEYCLOAK_HOST`, `KEYCLOAK_USERNAME`, and `KEYCLOAK_PASSWORD` (or set via `config.yaml`).
- Supports [resume capability](backup-restore.md#resume) for interrupted operations.
- For a comprehensive cleanup that includes this command, see [`delete_all_unused_environments`](delete_all_unused_environments.md).
