# integrity_check

Checks referential integrity across the MongoDB collections used by the registry cleaner, reporting any documents whose foreign-key references point to non-existent records.

## How It Works

Queries MongoDB, verifies the following cross-collection links, and then cross-references each orphaned document against the Docker registry to determine whether a Docker image still exists for it:

| Collection | Field | Must reference |
|---|---|---|
| `environment_revisions` | `environmentId` | `environments_v2._id` |
| `environment_revisions` | `clonedEnvironmentRevisionId` | `environment_revisions._id` |
| `model_versions` | `modelId.value` | `models._id` |

For orphaned documents the check fetches only the affected documents' tags from MongoDB (not all registry tags), then queries the Docker registry to set `has_image` on each issue. Environment revisions where `metadata.isBuilt=false` are skipped for the registry check since they will never have a corresponding image.

`runs` are intentionally excluded: when images are deleted with `--unused-since`, old runs will legitimately reference environments and revisions whose images have been cleaned up. Checking runs would produce false positives with no way to distinguish expected cleanup from genuine corruption.

## Usage

```bash
docker-registry-cleaner integrity_check
```

The report is saved to the `reports/` directory and is visible in the web UI.

## Options

| Option | Description |
|--------|-------------|
| `--output PATH` | Custom output file path (default: `reports/integrity-check.json`) |

## Output

The report contains a summary and a flat list of issues:

```json
{
  "summary": {
    "environments_checked": 120,
    "revisions_checked": 843,
    "models_checked": 45,
    "versions_checked": 210,
    "total_issues": 2,
    "issues_by_type": {
      "orphaned_revision": 2
    },
    "orphaned_with_image": 1,
    "orphaned_without_image": 1
  },
  "issues": [
    {
      "collection": "environment_revisions",
      "document_id": "64a1f3...",
      "issue_type": "orphaned_revision",
      "referenced_id": "64a1f2...",
      "description": "environmentId 64a1f2... not found in environments_v2",
      "has_image": true,
      "image_tag": "64a1f2...-rev3-20240101_abc123"
    }
  ]
}
```

### Issue types

| Issue type | Meaning |
|---|---|
| `orphaned_revision` | An `environment_revisions` document references an environment that no longer exists |
| `missing_environment_id` | An `environment_revisions` document has no `environmentId` field at all |
| `broken_clone_reference` | An `environment_revisions` document's `clonedEnvironmentRevisionId` points to a revision that no longer exists |
| `orphaned_model_version` | A `model_versions` document references a model that no longer exists |
| `missing_model_id` | A `model_versions` document has no `modelId.value` field |

## Notes

- This is a read-only command — it never modifies the registry or MongoDB.
- `orphaned_with_image: true` means there is a Docker image in the registry but no parent environment or model document. This needs manual investigation — the parent document may have been deleted accidentally.
- `orphaned_with_image: false` means there is neither a parent document nor a Docker image. These are safe to clean up with `delete_unused_references`.
- If the Docker registry is unreachable when the check runs, `has_image` will be `null` on all orphaned issues rather than causing the check to fail.
- A clean install will produce zero issues; issues typically appear after manual database edits or incomplete deletions.
