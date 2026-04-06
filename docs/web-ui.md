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
dominoUrl: "https://domino.example.com"  # your Domino instance URL
frontend:
  ingress:
    enabled: true
```

This exposes the UI at `https://domino.example.com/registry-cleaner`. The hostname is derived automatically from `dominoUrl`. The default annotations rewrite paths and forward `X-Forwarded-Prefix` so all links work correctly.

## Architecture

The frontend runs as a **sidecar container** in the same StatefulSet pod as the CLI tool, sharing the `/data/reports` volume. This avoids PVC access mode conflicts that would arise with a separate deployment.

## Helm Configuration

Key `values.yaml` options:

```yaml
dominoUrl: "https://domino.example.com"  # your Domino instance URL
frontend:
  enabled: true  # Set to false to disable
  image:
    repository: quay.io/domino/docker-registry-cleaner-frontend
    tag: v0.4.0
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
    enabled: true
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

Access requires Domino system administrator privileges. Every request is authenticated by forwarding the user's session cookie to the nucleus-frontend service (`/v4/auth/principal`) and verifying `isAdmin`. Unauthenticated requests receive a 401; authenticated non-admins receive a 403. Authentication is skipped when `DOMINO_API_URL` is not set (local dev mode).

## Troubleshooting

| Symptom | Check |
|---------|-------|
| No reports showing | Verify `REPORTS_DIR` and that JSON files exist; check volume mount permissions |
| UI not accessible | `kubectl get pods`, check ingress config, confirm port-forward |
| Container errors | `kubectl logs <pod-name> -c frontend` |
