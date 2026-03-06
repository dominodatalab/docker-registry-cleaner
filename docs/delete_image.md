# delete_image

Deletes a specific Docker image, or analyzes and deletes unused images based on image analysis reports and MongoDB usage data.

## How It Works

1. If a specific image is provided, deletes it directly.
2. Otherwise, loads image analysis and MongoDB usage reports to identify images safe to delete.
3. Cross-references registry tags against active usage (runs, workspaces, models, projects, etc.).
4. Optionally filters to a specific set of ObjectIDs from a file.
5. Performs deletion with optional S3 backup and MongoDB cleanup.

## Usage

```bash
# Delete a specific image
docker-registry-cleaner delete_image environment:abc123-456 --apply

# Analyze and delete unused images using reports
docker-registry-cleaner delete_image --apply --backup --s3-bucket my-bucket

# Filter to specific ObjectIDs from a file
docker-registry-cleaner delete_image --input environments --apply

# Force-regenerate analysis reports before running
docker-registry-cleaner delete_image --generate-reports --apply

# Only delete images unused in the last 30 days
docker-registry-cleaner delete_image --unused-since-days 30 --apply

# Also clean up MongoDB records after deletion
docker-registry-cleaner delete_image --apply --mongo-cleanup
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `image` | Specific image to delete (`type:tag` format) | — |
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--generate-reports` | Force regeneration of image analysis and usage reports | `false` |
| `--input FILE` | ObjectID file or pre-generated report to filter images | — |
| `--skip-analysis` | Skip workload analysis, use traditional environments file | `false` |
| `--unused-since-days N` | Only delete images unused for more than N days | — |
| `--output FILE` | Output path for the deletion analysis report | From config |
| `--image-analysis FILE` | Path to image analysis report | From config |
| `--backup` | Back up images to S3 before deletion | `false` |
| `--s3-bucket BUCKET` | S3 bucket for backups | From config |
| `--region REGION` | AWS region for S3/ECR | `us-west-2` |
| `--mongo-cleanup` | Also delete MongoDB records after Docker deletion | `false` |
| `--run-registry-gc` | Run registry garbage collection after deletion (internal registries only) | `false` |
| `--enable-docker-deletion` | Override registry in-cluster auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |

## ObjectID File Format

When using `--input`, provide a file with one ObjectID per line using typed prefixes:

```
environment:6286a3c76d4fd0362f8ba3ec
environmentRevision:6286a3c76d4fd0362f8ba3ed
model:627d94043035a63be6140e93
modelVersion:627d94043035a63be6140e94
```

See [ObjectID Filtering](objectid-filtering.md) for details.

## Notes

- `--mongo-cleanup` is high-risk. Only use it when you understand the impact and have recent backups.
