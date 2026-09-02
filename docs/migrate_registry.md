# migrate_registry

Copies Docker images from the source registry to a different destination registry using `skopeo copy`, with an optional MongoDB metadata update so Domino starts referencing the new location. Typical scenarios:

- Moving from Domino's internal registry to a managed registry (ECR, GCR/GAR, ACR)
- Moving a Domino instance to a different cloud provider
- Copying system images into an internal registry for air-gapped deployments

## How It Works

1. Verifies connectivity to the source registry and MongoDB (both are required — see [Notes](#notes)).
2. Discovers repositories and tags in the source registry: either the ones passed via `--repos`, or auto-discovered by probing the configured base repository and its `/environment` and `/model` sub-repos.
3. Optionally narrows the discovered tags to only those belonging to archived or non-archived environments/models (`--archived`/`--unarchived`), by querying MongoDB.
4. Copies each remaining tag from source to destination with `skopeo copy`, one repository at a time, saving a checkpoint after each repository so an interrupted run can resume (`--resume`).
5. Optionally rewrites the repository prefix in MongoDB (`builds`, `environment_revisions`, `model_versions`) to point at the new registry (`--update-mongodb`).
6. Writes a migration report (default `reports/migration-report.json`).

Dry-run by default — pass `--apply` to actually copy images and/or update MongoDB.

## Usage

```bash
# Discover what would be migrated (dry-run)
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo

# Migrate with basic auth to the destination
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass --apply

# Migrate with token auth (e.g. GCR/GAR)
docker-registry-cleaner migrate_registry --dest-registry-url europe-west1-docker.pkg.dev/project/repo \
  --dest-registry-token "$(gcloud auth print-access-token)" --apply

# Migrate specific repositories only
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo --repos domino-abc,domino-def --apply

# Migrate and update MongoDB to reference the new registry
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass \
  --update-mongodb --apply

# Migrate only images for archived environments/models — a reversible alternative to deleting them
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass \
  --archived --apply

# Resume an interrupted migration
docker-registry-cleaner migrate_registry --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass \
  --apply --resume
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--dest-registry-url URL` | Destination registry URL (e.g. `ecr.example.com/my-repo`) | Required |
| `--dest-creds USER:PASS` | Destination registry basic-auth credentials | — |
| `--dest-registry-token TOKEN` | Destination registry token (e.g. GCR/GAR access token) | — |
| `--dest-tls-verify` | Verify TLS certificates for the destination registry | `false` |
| `--repos REPO1,REPO2` | Specific repositories to migrate | Auto-discover |
| `--registry-url URL` | Source registry URL | From config |
| `--repository REPO` | Source repository name | From config |
| `--apply` | Actually copy images / update MongoDB (dry-run without this) | `false` |
| `--force` | Skip confirmation prompt when using `--apply` | `false` |
| `--update-mongodb` | Rewrite the repository prefix in MongoDB to the new registry after copying | `false` |
| `--old-prefix PREFIX` | Repository prefix to match for the MongoDB update | Source repository |
| `--new-prefix PREFIX` | Repository prefix to replace it with | Derived from `--dest-registry-url` |
| `--unarchived` | Only migrate images for non-archived environments/models (mutually exclusive with `--archived`) | `false` |
| `--archived` | Only migrate images for archived environments/models (mutually exclusive with `--unarchived`) | `false` |
| `--resume` | Resume from checkpoint if a previous migration was interrupted | `false` |
| `--operation-id ID` | Checkpoint identifier, for running multiple migrations concurrently | `migrate_registry` |
| `--output FILE` | Output path for the migration report | `reports/migration-report.json` |

Neither `--dest-creds` nor `--dest-registry-token` is required — skopeo will attempt unauthenticated access to the destination if neither is given (a warning is logged).

## Notes

- **MongoDB is always required, not just for `--update-mongodb`/`--archived`/`--unarchived`.** Health checks are run unconditionally before anything else, and MongoDB connectivity is one of the checks `run_health_checks()` always treats as required (the same is true for every other command in this tool) — a pure image-copy migration with no Mongo-related flags still needs MongoDB reachable to get past that gate.
- **`--archived`/`--unarchived` model-tag resolution excludes failed builds.** A model version can have several build attempts recorded in MongoDB; only a non-`"Failed"` one corresponds to a real image in the registry. This filtering joins to the `builds` collection and uses the most recent non-failed build's tag — the same logic (and the same shared helper) other MongoDB-usage code in this tool uses, rather than reading every build's tag off the model version document directly.
- **This command doesn't support `--backup`/`--s3-bucket`, `--generate-reports`, or `--enable-docker-deletion`/`--registry-statefulset`**, unlike most other commands in this tool — it never deletes from the source registry (so there's nothing to back up or override deletion RBAC for), and it has no separate "analysis report" concept to force-regenerate; it always writes its own migration report.
- **The MongoDB update is idempotent and additive to the repository path**, not a literal string replacement — it prepends the new prefix (e.g. `dominodatalab/environment` → `my-ecr-repo/dominodatalab/environment`) and skips documents whose repository field already starts with the new prefix, so re-running `--update-mongodb --apply` after a partial or repeated run is safe.
- **Discovery only searches the base repository and its `/environment` and `/model` sub-repos.** If your source registry uses a different layout, pass `--repos` explicitly.
- **Checkpoints are per-repository**, not per-tag — resuming skips repositories already fully copied and restarts any partially-copied repository from its first tag.
