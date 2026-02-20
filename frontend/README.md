# Docker Registry Cleaner Web UI

A Flask-based web interface for the Docker Registry Cleaner tool, providing a **read-only** interface for viewing and analyzing reports.

## Features

- **Report Viewer**: Browse and visualize JSON reports from cleanup operations
  - List all available reports with metadata (size, modification date)
  - Interactive report viewing with formatted summaries
  - Support for multiple report types (size reports, usage reports, deletion results, etc.)
  - Raw JSON view for detailed analysis
  - Download reports as JSON

- **Security-First Design**:
  - **Read-only interface** - no command execution from web UI
  - No exposure of kubectl commands or operational details
  - Minimal attack surface
  - Runs with read-only volume mounts
  - Operations must be run via `kubectl exec` by authorized users

- **User-Friendly Interface**:
  - Modern, responsive design
  - Easy navigation and report browsing
  - Visual statistics and data tables
  - Mobile-friendly layout

## Security Rationale

The web UI is **intentionally read-only** to prevent:
- Advertising destructive operations to unauthorized users
- Exposing kubectl commands that could be misused
- Accidental execution of cleanup operations
- Security information disclosure

Operations should be run via `kubectl exec` with proper RBAC authorization.

## Architecture

```
frontend/
├── app.py                 # Flask application (read-only)
├── requirements.txt       # Python dependencies
├── Dockerfile            # Container image definition
├── README.md             # This file
├── ARCHITECTURE.md       # Sidecar architecture documentation
├── SECURITY.md           # Security design rationale
├── QUICKSTART.md         # Build and deployment guide
└── templates/
    ├── base.html         # Base template
    ├── index.html        # Reports list page
    ├── report.html       # Individual report viewer
    └── static/
        ├── css/
        │   └── style.css
        └── js/
            └── main.js
```

## Local Development

### Prerequisites

- Python 3.11+
- Access to the docker-registry-cleaner reports directory

### Setup

```bash
cd frontend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables (optional)
export REPORTS_DIR=/path/to/reports  # Default: /app/reports
export SECRET_KEY=your-secret-key

# Run the application
python app.py
```

The web UI will be available at `http://localhost:8080`.

## Docker Deployment

### Build the Image

```bash
# From the docker-registry-cleaner root directory
docker build -f frontend/Dockerfile -t docker-registry-cleaner-frontend:latest .
```

### Run the Container

```bash
docker run -d \
  -p 8080:8080 \
  -v /path/to/reports:/app/reports:ro \
  --name registry-cleaner-ui \
  docker-registry-cleaner-frontend:latest
```

## Kubernetes/Helm Deployment

### Architecture

The frontend runs as a **sidecar container** within the same StatefulSet pod as the docker-registry-cleaner CLI. This architecture was chosen because:

- **Shared Volume Access**: Both containers need access to the same reports PVC with `ReadWriteOnce` access mode
- **No PVC Conflicts**: Sidecar containers share pod volumes without access mode restrictions
- **Simpler Deployment**: Single pod with two containers instead of coordinating separate deployments
- **Resource Efficiency**: No need for volume duplication or sync mechanisms

The StatefulSet contains:
- **Main container**: `docker-registry-cleaner` - CLI tool for analysis and cleanup
- **Sidecar container**: `frontend` - Web UI for viewing reports and running dry-run commands

Both containers share the `/data/reports` volume where reports are generated and read.

### Deployment

The frontend is integrated into the Helm chart. See the main [Helm Chart README](../charts/docker-registry-cleaner/README.md) for deployment instructions.

Key configuration in `values.yaml`:

```yaml
frontend:
  enabled: true  # Set to false to disable the web UI
  image:
    repository: quay.io/domino/docker-registry-cleaner-frontend
    tag: v0.3.1
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
    enabled: false  # Enable for external access
    className: nginx
    hosts:
      - host: registry-cleaner.example.com
```

## API Endpoints

### Public Endpoints

- `GET /` - Main page (reports list)
- `GET /reports/<filename>` - View specific report
- `GET /operations` - Operations dashboard
- `GET /health` - Health check

### API Endpoints

- `GET /api/reports` - List all reports (JSON)
- `GET /api/reports/<filename>` - Get report content (JSON)
- `GET /api/commands` - List available commands (JSON)
- `POST /api/execute` - Execute a command (JSON body: `{"command": "..."}`)

## Safety Features

- **Dry-run Only**: The web interface only allows execution of read-only/dry-run commands
- **No Destructive Operations**: Commands with `--apply` or `--force` flags are blocked
- **Clear Warnings**: Users are informed that destructive operations require `kubectl exec`
- **Command Validation**: All commands are validated before execution
- **Timeout Protection**: Commands have a 5-minute timeout

## Report Types Supported

The UI provides specialized views for:

- **MongoDB Usage Reports**: Environment and model usage statistics
- **Size Reports**: Image and user storage consumption
- **Archived Tags**: Tags eligible for cleanup
- **Unused Environments**: Environments not actively in use
- **Deletion Results**: Outcomes of cleanup operations
- **Final Reports**: Complete image analysis with layers

## Configuration

### Environment Variables

- `REPORTS_DIR`: Directory containing JSON reports (default: `/app/reports`)
- `SCRIPTS_DIR`: Directory containing cleanup scripts (default: `/app/python/scripts`)
- `SECRET_KEY`: Flask secret key for session management
- `HOST`: Listen host (default: `0.0.0.0`)
- `PORT`: Listen port (default: `8080`)

## Troubleshooting

### No Reports Showing

- Verify the `REPORTS_DIR` path is correct
- Check that report JSON files exist in the directory
- Ensure the container has read permissions on the reports volume

### Commands Not Executing

- Check container logs: `kubectl logs <pod-name> -c frontend`
- Verify the docker-registry-cleaner CLI is available in the container
- Ensure proper RBAC permissions for Kubernetes operations

### UI Not Accessible

- Verify the service is running: `kubectl get pods`
- Check ingress configuration: `kubectl get ingress`
- Confirm port forwarding: `kubectl port-forward <pod> 8080:8080`

## Security Considerations

- The UI does not implement authentication by default (relies on Kubernetes RBAC)
- Consider adding authentication for production deployments
- Limit network access using Kubernetes NetworkPolicies
- Run with minimal container privileges
- Use read-only volume mounts for reports directory

## Future Enhancements

Potential features for future versions:

- [ ] User authentication (OAuth, OIDC)
- [ ] Real-time command streaming (WebSockets)
- [ ] Report comparison and diff views
- [ ] Scheduled cleanup operations
- [ ] Email notifications
- [ ] Advanced filtering and search
- [ ] Custom report generation
- [ ] Multi-cluster support

## Contributing

See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License - see the [LICENSE](../LICENSE) file for details.
