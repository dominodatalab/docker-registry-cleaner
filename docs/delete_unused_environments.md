# delete_unused_environments

Finds and optionally deletes Docker images for environments that are not actively used anywhere in Domino.

## How It Works

1. Generates (or loads) MongoDB usage reports covering runs, workspaces, models, projects, scheduled jobs, organizations, and app versions.
2. Queries MongoDB `environments_v2` and `environment_revisions` for all non-archived environments.
3. Identifies environments not referenced in any usage report.
4. Scans the Docker registry for tags matching those unused IDs.
5. Performs a real-time usage check immediately before deletion to catch any new usage since the reports were generated.
6. Optionally deletes matched Docker images and cleans up MongoDB records.

## Usage

```bash
# Dry-run: find unused environments (auto-generates reports if missing)
docker-registry-cleaner delete_unused_environments

# Force-regenerate usage reports first
docker-registry-cleaner delete_unused_environments --generate-reports

# Only consider environments unused if not used in the last 30 days
docker-registry-cleaner delete_unused_environments --unused-since-days 30

# Delete with S3 backup
docker-registry-cleaner delete_unused_environments --apply --backup --s3-bucket my-bucket

# Delete with date filtering
docker-registry-cleaner delete_unused_environments --unused-since-days 30 --apply

# Delete from a pre-generated report (skips re-analysis)
docker-registry-cleaner delete_unused_environments --apply --input reports/unused-environments.json

# Resume an interrupted deletion
docker-registry-cleaner delete_unused_environments --apply --resume
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--generate-reports` | Force regeneration of usage reports | `false` |
| `--unused-since-days N` | Only consider environments unused if last used more than N days ago | — |
| `--input FILE` | Delete from a pre-generated report file | — |
| `--output FILE` | Output path for the analysis report | `reports/unused-environments.json` |
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

## Date Filtering

`--unused-since-days N` considers an environment in-use only if it was used in a run within the last N days (based on the `last_used`, `completed`, or `started` timestamp). Without this flag, any historical run — no matter how old — marks the environment as in-use and protects it from deletion.

Usage from sources without timestamps (models, projects, scheduled jobs, organizations, app versions) is always treated as current and always protects the environment, regardless of `--unused-since-days`.

## Notes

- `--mongo-cleanup` is high-risk: environments and models link to many other Domino assets. Only use it when you fully understand the impact and have recent backups.
- Supports [resume capability](backup-restore.md#resume) for interrupted operations.
- To archive environments in MongoDB without touching Docker images, use [`archive_unused_environments`](archive_unused_environments.md) instead.
- To delete environments owned by deactivated users, use [`delete_unused_private_environments`](delete_unused_private_environments.md).
