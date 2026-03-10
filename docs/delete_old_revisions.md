# delete_old_revisions

Keeps only the N most recent revisions per environment and deletes the rest from the Docker registry.

## How It Works

1. Queries MongoDB `environment_revisions`, grouped by `environmentId`, sorted oldest-first by ObjectId (which encodes creation time). Only successfully built revisions are included.
2. For each environment with more than N revisions, marks the oldest `total - N` as deletion candidates.
3. **Build-chain protection:** any candidate that a kept revision was cloned from is excluded — deleting it would break the provenance chain of newer revisions.
4. Performs a real-time usage check immediately before deletion. Any revision found to be in active use (run, workspace, model, scheduled job, project, or app version) is automatically skipped.
5. Optionally deletes MongoDB `environment_revisions` records after successful Docker image deletion.

## Usage

```bash
# Dry-run: find old revisions (default: keep 5 most recent per environment)
docker-registry-cleaner delete_old_revisions

# Keep only 3 revisions per environment
docker-registry-cleaner delete_old_revisions --keep-revisions 3

# Restrict to specific environments from a file
docker-registry-cleaner delete_old_revisions --input my-envs.txt

# Delete old revisions (requires confirmation)
docker-registry-cleaner delete_old_revisions --apply

# Delete without confirmation
docker-registry-cleaner delete_old_revisions --apply --force --keep-revisions 3

# Also remove MongoDB environment_revision records after deletion
docker-registry-cleaner delete_old_revisions --apply --mongo-cleanup

# Force-regenerate usage reports before analysis
docker-registry-cleaner delete_old_revisions --generate-reports
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--keep-revisions N` | Number of most recent revisions to keep per environment | `5` |
| `--input FILE` | File of environment ObjectIDs to restrict processing to (supports `environment:` prefix) | — |
| `--apply` | Actually delete images (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--generate-reports` | Regenerate MongoDB usage reports before analysis | `false` |
| `--mongo-cleanup` | Also delete `environment_revisions` MongoDB records after Docker deletion | `false` |
| `--output FILE` | Output path for the analysis report | `reports/old-revisions.json` |
| `--enable-docker-deletion` | Override registry in-cluster auto-detection | `false` |
| `--registry-statefulset NAME` | StatefulSet/Deployment name for registry | `docker-registry` |
| `--registry-url URL` | Docker registry URL | From config |
| `--repository REPO` | Repository name | From config |

## Notes

- `--keep-revisions` must be at least 1.
- `--mongo-cleanup` only deletes a revision record if the revision's Docker image was successfully deleted first, and only if the revision is not referenced by any version of an unarchived model.
- This command does not support `--backup` (S3 backup before deletion). If you need to preserve old revisions, back them up manually before running with `--apply`.
