# integrity_check

Checks referential integrity across the MongoDB collections used by the registry cleaner, reporting any documents whose foreign-key references point to non-existent records.

## How It Works

Queries MongoDB and verifies the following cross-collection links:

| Collection | Field | Must reference |
|---|---|---|
| `environment_revisions` | `environmentId` | `environments_v2._id` |
| `environment_revisions` | `clonedEnvironmentRevisionId` | `environment_revisions._id` |
| `model_versions` | `modelId.value` | `models._id` |
| `runs` | `environmentId` | `environments_v2._id` |
| `runs` | `environmentRevisionId` | `environment_revisions._id` |

Only `runs` documents that actually have an `environmentId` or `environmentRevisionId` field are checked — runs that reference no environment are skipped.

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
    "runs_checked": 15302,
    "total_issues": 3,
    "issues_by_type": {
      "orphaned_revision": 2,
      "run_missing_revision": 1
    }
  },
  "issues": [
    {
      "collection": "environment_revisions",
      "document_id": "64a1f3...",
      "issue_type": "orphaned_revision",
      "referenced_id": "64a1f2...",
      "description": "environmentId 64a1f2... not found in environments_v2"
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
| `run_missing_environment` | A `runs` document's `environmentId` points to an environment that no longer exists |
| `run_missing_revision` | A `runs` document's `environmentRevisionId` points to a revision that no longer exists |

## Notes

- This is a read-only command — it never modifies the registry or MongoDB.
- Orphaned revisions and model versions are candidates for cleanup with `delete_unused_references`.
- A clean install will produce zero issues; issues typically appear after manual database edits or incomplete deletions.
