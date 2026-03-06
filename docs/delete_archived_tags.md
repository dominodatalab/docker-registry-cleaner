# delete_archived_tags

Finds and optionally deletes Docker images for environments and models that have been marked as archived in MongoDB (`isArchived: true`).

## How It Works

1. Queries MongoDB `environments_v2` and/or `models` collections for archived documents.
2. Finds all related `environment_revisions` or `model_versions` records.
3. Scans the Docker registry for tags matching those IDs.
4. Performs a real-time usage check — any image still in use is automatically skipped.
5. Optionally deletes matched Docker images and cleans up MongoDB records.

## Usage

```bash
# Dry-run: find archived environment tags
docker-registry-cleaner delete_archived_tags --environment

# Dry-run: find archived model tags
docker-registry-cleaner delete_archived_tags --model

# Dry-run: find both
docker-registry-cleaner delete_archived_tags --environment --model

# Delete with S3 backup
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-bucket

# Delete both environment and model tags
docker-registry-cleaner delete_archived_tags --environment --model --apply --backup --s3-bucket my-bucket

# Delete from a pre-generated report (skips re-analysis)
docker-registry-cleaner delete_archived_tags --environment --apply --input reports/archived-tags.json

# Resume an interrupted deletion
docker-registry-cleaner delete_archived_tags --environment --apply --resume
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--environment` | Process archived environments and revisions | — |
| `--model` | Process archived models and model versions | — |
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--input FILE` | Delete from a pre-generated report file | — |
| `--output FILE` | Output path for the analysis report | `reports/archived-tags.json` |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups | From config |
| `--region REGION` | AWS region for S3/ECR | `us-west-2` |
| `--mongo-cleanup` | Also delete MongoDB records after Docker deletion | `false` |
| `--run-registry-gc` | Run registry garbage collection after deletion (internal registries only) | `false` |
| `--unused-since-days N` | Only consider images unused if last used more than N days ago | — |
| `--resume` | Resume from a previous checkpoint | `false` |
| `--operation-id ID` | ID for checkpoint management | — |
| `--enable-docker-deletion` | Override registry in-cluster auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |
| `--registry-url URL` | Docker registry URL | From config |
| `--repository REPO` | Repository name | From config |

## Notes

- At least one of `--environment` or `--model` is required.
- `--mongo-cleanup` is high-risk: environments and models link to many other Domino assets. Only use it when you fully understand the impact and have recent backups. Start with Docker-only cleanup first.
- Supports [resume capability](backup-restore.md#resume) for interrupted long-running operations.
- See [ObjectID Filtering](objectid-filtering.md) if you need to target specific environments or models by ID.
