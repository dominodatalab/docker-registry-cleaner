# Web UI

Docker Registry Cleaner includes a Flask-based web interface for viewing and analyzing reports.

## Features

- **Report Browser**: Browse all generated JSON reports with formatted summaries, data tables, and raw JSON view
- **Report Downloads**: Download any report as JSON
- **Operations Dashboard**: Run analysis and dry-run commands from the browser via the backend API
- **Safety**: Destructive operations (those requiring `--apply`) must still be run via `kubectl exec`

## Accessing the Web UI

**Port-forward (quick access)**
```bash
kubectl port-forward -n domino-platform svc/docker-registry-cleaner-frontend 8080:8080
```
Then open [http://localhost:8080](http://localhost:8080).

**Ingress (production)**

The frontend is designed to be served at `/registry-cleaner` on your existing Domino hostname, consistent with other Domino integrations (e.g. `/grafana`, `/toolkit`).

Enable in your Helm values:
```yaml
frontend:
  enabled: true
  ingress:
    enabled: true
    hosts:
      - host: domino.example.com  # your Domino instance hostname
```

This exposes the UI at `https://domino.example.com/registry-cleaner`. The default annotations rewrite paths and forward `X-Forwarded-Prefix` so all links work correctly.

## Architecture

The frontend runs as a **sidecar container** in the same StatefulSet pod as the CLI tool, sharing the `/data/reports` volume. This avoids PVC access mode conflicts that would arise with a separate deployment.

## Helm Configuration

Key `values.yaml` options:

```yaml
frontend:
  enabled: true  # Set to false to disable
  image:
    repository: quay.io/domino/docker-registry-cleaner-frontend
    tag: v0.3.5
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
  service:
    type: ClusterIP
    port: 8080
  ingress:
    enabled: false
    hosts:
      - host: domino.example.com  # your Domino hostname
```

## Local Development

```bash
cd frontend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export REPORTS_DIR=/path/to/reports  # default: /app/reports
python app.py
```

## Security

The UI does not implement authentication — it relies on Kubernetes RBAC and network access controls. Destructive operations are blocked at the API level. For production deployments, restrict access via NetworkPolicies or add an authenticating proxy.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| No reports showing | Verify `REPORTS_DIR` and that JSON files exist; check volume mount permissions |
| UI not accessible | `kubectl get pods`, check ingress config, confirm port-forward |
| Container errors | `kubectl logs <pod-name> -c frontend` |
