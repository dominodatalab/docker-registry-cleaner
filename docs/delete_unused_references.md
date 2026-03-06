# delete_unused_references

Finds and optionally removes MongoDB records that reference Docker images which no longer exist in the registry.

This is a MongoDB-only command — it does not modify the Docker registry.

## How It Works

1. Scans MongoDB collections (`environment_revisions`, `model_versions`, etc.) for `metadata.dockerImageName.tag` fields.
2. Checks whether each referenced Docker tag still exists in the registry.
3. Reports (or removes) records whose referenced image is missing.

## Usage

```bash
# Dry-run: find stale MongoDB references
docker-registry-cleaner delete_unused_references

# Apply: remove stale records
docker-registry-cleaner delete_unused_references --apply
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually delete MongoDB records (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--registry-url URL` | Docker registry URL | From config |
| `--repository REPO` | Repository name | From config |

## Notes

- This command only modifies MongoDB; `--backup` is not applicable.
- Use this after bulk Docker deletions to clean up orphaned MongoDB references.
- This is an advanced operation. Only use it if you understand the Domino MongoDB schema and have recent backups.
