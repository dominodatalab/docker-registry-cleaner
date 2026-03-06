# archive_unused_environments

Marks unused environments as archived in MongoDB (`isArchived: true` on `environments_v2` documents) without touching Docker images.

Use this when you want to flag environments for cleanup without immediately deleting their Docker images. Once archived, the images can later be removed by [`delete_archived_tags`](delete_archived_tags.md).

## How It Works

1. Generates (or loads) MongoDB usage reports (same analysis as `delete_unused_environments`).
2. Identifies environments not referenced anywhere in Domino.
3. Sets `isArchived: true` on the corresponding `environments_v2` documents.

## Usage

```bash
# Dry-run: show which environments would be archived
docker-registry-cleaner archive_unused_environments

# Only consider environments unused if not used in the last 30 days
docker-registry-cleaner archive_unused_environments --unused-since-days 30

# Apply (requires confirmation)
docker-registry-cleaner archive_unused_environments --apply

# Archive environments unused for 60+ days, skip confirmation
docker-registry-cleaner archive_unused_environments --unused-since-days 60 --apply --force

# Force-regenerate usage reports first
docker-registry-cleaner archive_unused_environments --generate-reports --apply
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--apply` | Actually archive environments (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt | `false` |
| `--generate-reports` | Force regeneration of usage reports | `false` |
| `--unused-since-days N` | Only consider environments unused if last used more than N days ago | — |
| `--registry-url URL` | Docker registry URL | From config |
| `--repository REPO` | Repository name | From config |

## Notes

- This command is MongoDB-only — it does not modify the Docker registry. No `--backup` option is needed.
- After archiving, run [`delete_archived_tags --environment`](delete_archived_tags.md) to remove the Docker images.
- See [`delete_unused_environments`](delete_unused_environments.md) to perform both steps in one command.
