# Backup, Restore, and Resume

## Backup to S3

Most deletion commands support `--backup`, which copies each image to S3 before deleting it. If the backup fails, deletion is aborted to prevent data loss.

```bash
# Backup and delete
docker-registry-cleaner delete_archived_tags --environment --apply --backup --s3-bucket my-bucket

# Backup only (no deletion) — use --force to skip the "no images will be deleted" warning
docker-registry-cleaner delete_archived_tags --environment --backup --s3-bucket my-bucket --force
```

Provide the S3 bucket via `--s3-bucket`, the `S3_BUCKET` environment variable, or `config.yaml`.

## Restore from S3

```bash
# Restore specific tags
docker-registry-cleaner backup_restore restore --tags tag1 tag2

# Restore with explicit S3 bucket override
docker-registry-cleaner backup_restore restore --s3-bucket my-backup-bucket --tags tag1 tag2
```

Restoring a Docker image does not restore its MongoDB records. However, a restored image can be used as a base image for a new Domino environment.

## Resume

Long-running deletion operations can be interrupted and resumed without re-processing already-completed items.

```bash
# Resume from the most recent checkpoint
docker-registry-cleaner delete_archived_tags --environment --apply --resume

# Resume a specific operation by ID
docker-registry-cleaner delete_archived_tags --environment --apply --resume --operation-id 2026-01-15-14-30-00
```

Checkpoints are saved every 10 items in `reports/checkpoints/`. Supported commands: `delete_archived_tags`, `delete_unused_environments`, `delete_unused_private_environments`.

## Timestamped Reports

Auto-generated reports include a timestamp suffix so you can compare results across runs:

```
reports/mongodb_usage_report-2026-01-15-14-30-00.json
reports/tag-sums-2026-01-15-14-35-12.json
reports/final-report-2026-01-15-14-40-25.json
```

Reports generated via `--output` (user-specified paths) do not include timestamps.

## Understanding Report Numbers

**Tag references vs. unique images:** A dry-run may show many "matching tags" (e.g., 1232). The same Docker image `(image_type, tag)` can match multiple archived MongoDB IDs. Deletion is performed per unique `(image_type, tag)`, so "1232 tag references" might mean only ~600 unique images are actually deleted.

**Why dry-run numbers differ between runs:** Re-running a dry-run re-queries MongoDB and re-scans the registry. Numbers differ due to: (1) different scope flags, (2) actual deletions reducing registry tags from a previous run, or (3) MongoDB state changes. Use `--input <report.json>` to delete from a previously saved report for a consistent set.
