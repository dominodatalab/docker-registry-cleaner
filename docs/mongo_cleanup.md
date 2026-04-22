# mongo_cleanup

Deletes MongoDB records from `environment_revisions` or `model_versions` that match a given set of Docker image tags or ObjectIDs, provided the records are no longer referenced anywhere in Domino.

> **Note:** For most use cases, prefer [`delete_unused_references`](delete_unused_references.md), which automatically discovers stale MongoDB records by comparing them against the live registry. Use `mongo_cleanup` when you have a specific list of tags or ObjectIDs to target.

## How It Works

1. Reads a list of targets (ObjectIDs or Docker tags) from a file.
2. For each target, finds matching documents in the specified collection using the tag field (`metadata.dockerImageName.tag` for `environment_revisions`, `metadata.builds.slug.image.tag` for `model_versions`).
3. Before deleting, checks whether each candidate document is still referenced anywhere in Domino. Candidates with active references are reported and skipped — only unreferenced documents are deleted.

### Reference checks for `environment_revisions`

Checks both the revision's own `_id` and its parent environment ID against:

| Collection | Field(s) |
|---|---|
| `runs` | `environmentRevisionId`, `environmentId` |
| `workspace` | `configTemplate.environmentId` |
| `workspace_session` | `environmentRevisionId`, `computeClusterEnvironmentRevisionId`, `environmentId`, `config.environmentId`, `computeClusterEnvironmentId`, `config.computeClusterProps.computeEnvironmentId` |
| `projects` | `overrideV2EnvironmentId` (non-archived projects only) |
| `scheduler_jobs` | `jobDataPlain.overrideEnvironmentId` |
| `organizations` | `defaultV2EnvironmentId` |
| `userPreferences` | `defaultEnvironmentId` |
| `app_versions` | `environmentId` (from unarchived apps only) |

### Reference checks for `model_versions`

Blocks deletion if the parent model (`modelId.value`) exists and is not archived.

## Usage

```bash
# Dry run — show what would be deleted and what would be blocked
docker-registry-cleaner mongo_cleanup --file ./targets.txt

# Apply deletions
docker-registry-cleaner mongo_cleanup --apply --file ./targets.txt

# Target model_versions instead of environment_revisions
docker-registry-cleaner mongo_cleanup --apply --file ./targets.txt --collection model_versions
```

## Options

| Option | Description |
|--------|-------------|
| `--file PATH` | Path to target file (required) |
| `--collection NAME` | MongoDB collection to clean up (default: `environment_revisions`) |
| `--apply` | Execute deletions (default: dry-run) |

## Target file format

Each line specifies one target. Lines starting with `#` are treated as comments. The first token on each line is used:

- **24-character hex string** — treated as an ObjectID; matches all documents whose tag starts with `<ObjectID>-`
- **Any other value** — treated as an exact Docker tag

```
# ObjectID prefix match — matches all revisions of this environment
507f1f77bcf86cd799439011

# Exact tag match
environment:507f1f77bcf86cd799439011-abc123def456
```

## Output

In dry-run mode, each candidate is shown with its status:

```
DRY RUN: 3 matched, 1 blocked, 2 safe to delete
  67a1f3b2c4d5e6f700000001: safe to delete
  67a1f3b2c4d5e6f700000002: safe to delete
  67a1f3b2c4d5e6f700000003: BLOCKED (projects.overrideV2EnvironmentId)
```

In apply mode, blocked documents are reported before deletion proceeds:

```
SKIPPING 1 document(s) still referenced in Domino:
  67a1f3b2c4d5e6f700000003: projects.overrideV2EnvironmentId
Deleting 2 document(s) for objectId='507f1f77bcf86cd799439011' in domino.environment_revisions
  deleted: 2
```

## Notes

- This is a MongoDB-only command — it does not delete Docker images. Delete the Docker images first (e.g. with `delete_archived_tags --apply`), then use this command to clean up the corresponding records.
- Always do a dry run first to verify what will be deleted and check for unexpected blocks.
