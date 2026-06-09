# Running Operations as Kubernetes CronJobs

CronJobs allow docker-registry-cleaner operations to run on a schedule without
manual intervention via `kubectl exec` or the web UI.

---

## Why CronJobs?

The StatefulSet is well suited for interactive use, but scheduled cleanup work is a
better fit for CronJobs:

- **No idle container** — the pod exists only while the job runs, then terminates.
- **Audit trail** — each run produces a distinct pod with its own logs, visible in
  `kubectl get pods` and `kubectl logs`.
- **Independent resources** — heavy reporting jobs do not share CPU/memory with the
  long-running StatefulSet or frontend.
- **Native retries** — `backoffLimit` retries failed jobs automatically.

The StatefulSet remains useful for interactive use, the web UI, and operations that
require human review before applying (dry-run first, then manual apply).

---

## Storage: domino-shared-store

CronJob pods can land on any node, so the reports volume must be RWX
(ReadWriteMany). The chart uses `domino-shared-store`, the existing RWX PVC shared
across Domino services, with a dedicated subdirectory to avoid naming conflicts:

```
domino-shared-store  (RWX)
└── registry-cleaner/     ← subPath used by all registry-cleaner pods
    ├── final-report.json
    ├── deletion-analysis.json
    └── ...
```

This is configured in `values.yaml`:

```yaml
persistence:
  enabled: true
  pvcName: "domino-shared-store"
  subPath: "registry-cleaner"
```

Both the StatefulSet containers and every CronJob pod mount the same PVC with
`subPath: registry-cleaner`, so reports written by a CronJob are immediately
visible to the web UI and the FastAPI server.

> **Why domino-shared-store?** If it has an issue, Domino as a whole is down and
> we have bigger problems. It avoids the complexity of managing a dedicated RWX PVC.

---

## Helm configuration

CronJobs are defined in `values.yaml` under the `cronjobs` list. The chart creates
one `batch/v1 CronJob` resource per entry.

```yaml
cronjobs:
  - name: reports
    schedule: "0 2 * * *"          # 02:00 UTC nightly
    command: ["python", "main.py", "reports"]

  - name: delete-archived-tags
    schedule: "0 2 * * 0"          # Sundays 02:00 UTC
    ensureFreshReports: true
    command: ["python", "main.py", "delete_archived_tags",
              "--environment", "--model", "--apply"]
    resources:
      requests: { cpu: 100m, memory: 256Mi }
      limits:   { cpu: 1000m, memory: 1Gi }

  - name: delete-unused-environments
    schedule: "0 3 * * 0"          # Sundays 03:00 UTC
    ensureFreshReports: true
    command: ["python", "main.py", "delete_unused_environments",
              "--unused-since-days", "90"]

  - name: delete-unused-references
    schedule: "0 4 1 * *"          # 1st of month
    command: ["python", "main.py", "delete_unused_references", "--apply"]

  - name: run-registry-gc
    schedule: "0 5 * * 0"          # Sundays 05:00 UTC (after deletion jobs)
    command: ["python", "main.py", "run_registry_gc"]
```

### CronJob fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | — | Short name; appended to the chart's fullname (e.g. `docker-registry-cleaner-reports`) |
| `schedule` | Yes | — | Standard 5-field cron expression (UTC) |
| `command` | Yes | — | Container command list |
| `ensureFreshReports` | No | `false` | Run `ensure_reports` before `command` (see below) |
| `backoffLimit` | No | `1` | Number of pod retries on failure |
| `resources` | No | 100m/256Mi–500m/512Mi | Container resource requests and limits |

### Naming

Each CronJob is named `<fullname>-<entry.name>`, e.g. `docker-registry-cleaner-reports`.
Jobs spawned by the CronJob are labelled `registry-cleaner-cronjob: <entry.name>`,
which is how the backend's `/api/cronjobs/{name}/runs` endpoint locates their history.

---

## ensure_reports

Deletion jobs depend on the reports data being reasonably fresh. The `ensureFreshReports`
flag wraps the configured command with a pre-check:

```bash
python main.py ensure_reports && python main.py delete_archived_tags --apply
```

`ensure_reports` checks the mtime of `final-report.json` on the shared PVC:

- If the file is **younger than 24 hours**, it exits 0 and the deletion command runs.
- If the file is **older than 24 hours or missing**, it runs `reports` first, then exits 0.

The age threshold can be overridden with `--max-age-hours`:

```bash
python main.py ensure_reports --max-age-hours 12
```

This is implemented as a built-in subcommand in `python/main.py` — it is not a separate
script file.

---

## Schedule UI

The Schedule page at `/schedule` in the web UI lists all managed CronJobs with:

- Human-readable cron expression (e.g. "At 02:00 on Sunday")
- Status badge: Suspended / Running / Never succeeded / Healthy
- Last scheduled and last successful run times (relative)
- Expandable runs panel showing the 20 most recent Job runs with status and duration

The frontend proxies two backend endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/cronjobs` | Lists all CronJobs labelled `app.kubernetes.io/name=docker-registry-cleaner` |
| `GET /api/cronjobs/{name}/runs` | Lists the 20 most recent Jobs labelled `registry-cleaner-cronjob={name}` |

The backend reads these from the Kubernetes API via `kubernetes.client.BatchV1Api`.
On any error (e.g. running locally without cluster access), both endpoints return an
empty list rather than an error.

---

## RBAC

The existing namespaced `Role` in `rbac.yaml` has been extended with:

```yaml
- apiGroups: ["batch"]
  resources: ["cronjobs", "jobs"]
  verbs: ["get", "list"]
```

CronJob pods reuse the same `ServiceAccount` as the StatefulSet and need no additional
bindings. The `batch` read permissions are used only by the StatefulSet's FastAPI
server to populate the Schedule UI.

---

## Recommended schedule

| Operation | Suggested schedule | Notes |
|---|---|---|
| `reports` | Nightly, 02:00 | Keeps report data fresh for the web UI |
| `delete_archived_tags` | Weekly, Sunday 02:00 | Enable `--apply`; use `ensureFreshReports` |
| `delete_unused_environments` | Weekly, Sunday 03:00 | Dry-run first; enable `--apply` after reviewing reports |
| `delete_unused_private_environments` | Weekly, Sunday 03:30 | Same as above |
| `delete_unused_references` | Monthly, 1st of month | Safe; removes stale MongoDB references only |
| `run_registry_gc` | Weekly, Sunday 05:00 | Run **after** all deletion jobs to reclaim disk space |

> **`run_registry_gc` ordering**: GC must run after all tag deletion jobs for a given window
> have completed — running GC while a deletion job is in progress can corrupt the registry.
> Enforce ordering via schedule times (e.g. GC at 05:00, deletions at 02:00–04:00).

> **Note on `--apply` for environment deletion**: These operations are higher-risk.
> A safer pattern is to schedule dry-run first and enable `--apply` only after reviewing
> several weeks of reports.

---

## Operational considerations

**Concurrency with the StatefulSet**
Both the StatefulSet and a CronJob pod will run `skopeo` against the same registry.
The built-in rate limiter (`skopeo.rate_limit` in `config.yaml`) applies per-process,
not globally, so back-to-back heavy operations could temporarily exceed registry limits.
Schedule CronJobs during off-peak hours.

**Secret access**
CronJob pods need the same secrets as the StatefulSet: `keycloak-http` and
`mongodb-replicaset-admin`. The `registry-cleaner-api-key` secret is only needed by
StatefulSet containers (frontend ↔ backend communication); CronJob pods do not use it.
