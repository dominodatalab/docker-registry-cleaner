# Prometheus Metrics

The backend API (`python/api.py`) exposes a Prometheus scrape endpoint at `GET /metrics`
on port 8081.  Port 8081 has no Kubernetes Service, so it is only reachable via
`kubectl port-forward` or a Prometheus `PodMonitor`.  No Pushgateway is needed.

The pod template is annotated for annotation-based Prometheus discovery:
```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8081"
prometheus.io/path: "/metrics"
```

To scrape manually:
```bash
kubectl port-forward pod/docker-registry-cleaner-0 8081:8081 -n domino-platform
curl -s localhost:8081/metrics
```

---

## Implemented metrics

### Dry-run findings

Refreshed from the latest report files on the shared PVC **at every scrape**.
CronJob pods write JSON reports to `/data/reports`; the StatefulSet reads them at
scrape time — no Pushgateway needed.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `registry_cleaner_tags_pending_deletion` | Gauge | `operation` | Tags eligible for deletion from the latest dry-run report |
| `registry_cleaner_space_recoverable_bytes` | Gauge | `operation` | Estimated bytes recoverable from the latest dry-run report |
| `registry_cleaner_last_report_timestamp` | Gauge | `operation` | Unix mtime of the latest report file |

Operations instrumented: `delete_archived_tags`, `delete_unused_environments`,
`delete_unused_private_environments`.

### Job queue

Computed from the in-memory job store at every scrape.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `registry_cleaner_jobs_total` | Gauge | `operation`, `status` | Tracked jobs by operation and status (`pending` / `running` / `completed` / `failed` / `cancelled`) |

### Recommended Grafana alerts

| Alert | Condition | Meaning |
|---|---|---|
| Archived tag backlog | `registry_cleaner_tags_pending_deletion{operation="delete_archived_tags"} > 50` | Archived tags accumulating; consider enabling `--apply` |
| Large recoverable space | `registry_cleaner_space_recoverable_bytes > 50 * 1024^3` | >50 GB recoverable; cleanup overdue |
| Reports stale | `time() - registry_cleaner_last_report_timestamp > 172800` | Reports not refreshed in 48 h; CronJob may be failing |

---

## Potential future metrics

The sections below are not yet implemented but are well-defined enough to add
incrementally.  Each one instruments an existing layer of the codebase with no
breaking changes.

### Operation outcomes

Instruments `python/main.py` — confirms work is happening and surfaces failures.

```python
from prometheus_client import Counter, Histogram

images_deleted = Counter(
    'registry_cleaner_images_deleted_total',
    'Total images deleted',
    ['operation', 'type'],   # type = "environment" | "model" | "reference"
)

operation_runs = Counter(
    'registry_cleaner_operation_runs_total',
    'Total operation invocations',
    ['operation', 'apply'],  # apply = "true" | "false" (dry-run vs real)
)

operation_duration = Histogram(
    'registry_cleaner_operation_duration_seconds',
    'Wall-clock duration of each operation',
    ['operation'],
    buckets=[10, 30, 60, 120, 300, 600, 1800],
)
```

---

### Staleness

Useful for alerting when cleanups have not run in a configurable window (e.g. 7 days).
Pair with an Alertmanager rule like
`time() - registry_cleaner_last_successful_run_timestamp > 604800`.

```python
from prometheus_client import Gauge

last_successful_run = Gauge(
    'registry_cleaner_last_successful_run_timestamp',
    'Unix timestamp of the last successful apply run for each operation',
    ['operation'],
)
```

---

### Component health

Maps directly onto the existing `HealthChecker` in `python/utils/health_checks.py`.
Set to `1.0` (healthy) or `0.0` (unhealthy) after each `run_all_checks()` call.

```python
from prometheus_client import Gauge

component_up = Gauge(
    'registry_cleaner_component_up',
    'Health check status per component (1 = healthy, 0 = unhealthy)',
    ['component'],  # "registry" | "mongodb" | "kubernetes" | "s3"
)
```

```python
for result in health_checker.run_all_checks():
    component_up.labels(component=result.name).set(1.0 if result.status else 0.0)
```

Alert: `registry_cleaner_component_up == 0`

---

### Cache effectiveness

Instruments the four cache types in `python/utils/cache_utils.py`
(`tag_list`, `image_inspect`, `mongo_query`, `layer_calc`).
Low hit rates indicate the TTLs or max-sizes in `config.yaml` may need tuning.

```python
from prometheus_client import Counter

cache_hits = Counter(
    'registry_cleaner_cache_hits_total',
    'Cache hits by cache type',
    ['cache'],
)

cache_misses = Counter(
    'registry_cleaner_cache_misses_total',
    'Cache misses by cache type',
    ['cache'],
)
```

---

### Retry behaviour

Instruments `python/utils/retry_utils.py`. Currently these failures are only visible
in logs. Surfacing them as metrics makes it possible to alert on sustained connectivity
problems with the registry, MongoDB, or Kubernetes API.

```python
from prometheus_client import Counter

retry_attempts = Counter(
    'registry_cleaner_retry_attempts_total',
    'Total retry attempts before success',
    ['function', 'error_type'],  # error_type = "network" | "temporary"
)

retry_exhausted = Counter(
    'registry_cleaner_retry_exhausted_total',
    'Operations that failed after exhausting all retries',
    ['function'],
)
```

---

### Registry API latency

Instruments the skopeo calls in `python/utils/skopeo_client.py`.
Useful for detecting registry slowdowns before they cause operation timeouts.

```python
from prometheus_client import Counter, Histogram

registry_api_latency = Histogram(
    'registry_cleaner_registry_api_latency_seconds',
    'Latency of skopeo calls to the registry',
    ['operation'],  # "list_tags" | "inspect" | "delete" | "copy"
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)

registry_api_errors = Counter(
    'registry_cleaner_registry_api_errors_total',
    'Registry API errors by type',
    ['operation', 'error_type'],  # error_type = "network" | "auth" | "not_found" | "rate_limited"
)
```
