# Frontend Quick Start Guide

This guide will help you build, test, and deploy the Docker Registry Cleaner frontend.

## Local Development & Testing

### Prerequisites

- Python 3.11+
- Docker (for container builds)
- Access to the docker-registry-cleaner reports directory

### Quick Start

```bash
# Navigate to frontend directory
cd /Users/elliott/code/dominodatalab/docker-registry-cleaner/frontend

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up test environment (optional)
export REPORTS_DIR=../reports  # Point to your reports directory

# Run the application
python app.py
```

The application will start on [http://localhost:8080](http://localhost:8080).

## Building the Docker Image

### Build Locally

From the **root** of the docker-registry-cleaner project:

```bash
cd /Users/elliott/code/dominodatalab/docker-registry-cleaner

# Build the frontend image
docker build -f frontend/Dockerfile -t docker-registry-cleaner-frontend:latest .
```

### Test the Container Locally

```bash
# Run the container with local reports
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/reports:/app/reports:ro \
  --name registry-cleaner-ui \
  docker-registry-cleaner-frontend:latest

# Check logs
docker logs registry-cleaner-ui

# Access the UI
open http://localhost:8080

# Stop and remove
docker stop registry-cleaner-ui
docker rm registry-cleaner-ui
```

## Building and Pushing to Quay.io

### Tag and Push

```bash
# Build with version tag
VERSION=v0.3.1
docker build -f frontend/Dockerfile \
  -t quay.io/domino/docker-registry-cleaner-frontend:${VERSION} \
  -t quay.io/domino/docker-registry-cleaner-frontend:latest \
  .

# Login to Quay (if needed)
docker login quay.io

# Push both tags
docker push quay.io/domino/docker-registry-cleaner-frontend:${VERSION}
docker push quay.io/domino/docker-registry-cleaner-frontend:latest
```

## Deploying with Helm

### Update Helm Values

Edit `charts/docker-registry-cleaner/values.yaml`:

```yaml
frontend:
  enabled: true
  image:
    repository: quay.io/domino/docker-registry-cleaner-frontend
    tag: v0.3.1
    pullPolicy: Always
```

### Install/Upgrade

```bash
# Install new deployment
helm install docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform \
  --create-namespace

# Or upgrade existing deployment
helm upgrade docker-registry-cleaner ./charts/docker-registry-cleaner \
  --namespace domino-platform
```

### Verify Deployment

```bash
# Check pods (frontend runs as sidecar in StatefulSet)
kubectl get pods -n domino-platform | grep registry-cleaner
# Expected: docker-registry-cleaner-0   2/2   Running

# Check that both containers are running
kubectl get pod docker-registry-cleaner-0 -n domino-platform -o jsonpath='{.spec.containers[*].name}'
# Expected: docker-registry-cleaner frontend

# Check services
kubectl get svc -n domino-platform | grep registry-cleaner

# Check frontend logs (note the -c frontend flag for sidecar)
kubectl logs -n domino-platform docker-registry-cleaner-0 -c frontend

# Check main container logs
kubectl logs -n domino-platform docker-registry-cleaner-0 -c docker-registry-cleaner

# Port-forward for testing
kubectl port-forward -n domino-platform svc/docker-registry-cleaner-frontend 8080:8080
```

## Testing the Frontend

### Manual Testing Checklist

1. **Home Page (Reports List)**
   - [ ] Reports are listed with metadata (size, date)
   - [ ] Clicking a report opens the detail view
   - [ ] Download button works

2. **Report Detail Page**
   - [ ] Summary tab shows statistics
   - [ ] Raw JSON tab displays formatted JSON
   - [ ] Back button returns to reports list
   - [ ] Different report types render correctly

3. **Operations Page**
   - [ ] Commands are organized by category
   - [ ] Run button executes dry-run commands
   - [ ] Output is displayed in real-time
   - [ ] Error messages are shown clearly
   - [ ] Commands with --apply are blocked

4. **Navigation**
   - [ ] Nav menu works correctly
   - [ ] Active page is highlighted
   - [ ] Responsive on mobile/tablet

### API Testing

```bash
# List reports
curl http://localhost:8080/api/reports | jq

# Get specific report
curl http://localhost:8080/api/reports/final-report.json | jq

# List commands
curl http://localhost:8080/api/commands | jq

# Execute command (dry-run only)
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "docker-registry-cleaner health_check"}' | jq

# Health check
curl http://localhost:8080/health | jq
```

## Troubleshooting

### Issue: No reports showing

**Solution**: Check reports directory path (frontend is a sidecar in the StatefulSet)
```bash
# Check frontend container
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -c frontend -- ls -la /app/reports

# Check main container (reports written here)
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -c docker-registry-cleaner -- ls -la /data/reports

# Check PVC mount
kubectl describe pod docker-registry-cleaner-0 -n domino-platform
```

### Issue: Commands not executing

**Solution**: Verify docker-registry-cleaner CLI is available in frontend container
```bash
# The CLI should be in the PATH or accessible from the frontend container
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -c frontend -- which docker-registry-cleaner

# If not found, the frontend can't execute commands - this is expected if the CLI
# is only installed in the main container. You may need to adjust the frontend
# to execute commands via kubectl exec into the main container.
```

### Issue: Permission errors

**Solution**: Check fsGroup and PVC permissions
```yaml
# In values.yaml
podSecurityContext:
  fsGroup: 65532
```

### Issue: Image pull errors

**Solution**: Verify image exists and pull secrets are configured
```bash
# Check if image exists
docker pull quay.io/domino/docker-registry-cleaner-frontend:v0.3.1

# Verify pull secrets
kubectl get secret domino-quay-repos -n domino-platform
```

## Development Tips

### Hot Reload for Development

For faster development, use Flask's debug mode:

```python
# In app.py, change the last line to:
if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=True)
```

### Adding New Report Types

1. Update `view_report()` in `app.py` to add new report type detection
2. Add rendering function in `report.html` (e.g., `renderNewReportType()`)
3. Test with actual report data

### Styling Changes

Edit `frontend/templates/static/css/style.css` and refresh the browser.

### Adding New Commands

Update the `commands` list in the `/api/commands` route in `app.py`.

## Production Checklist

Before deploying to production:

- [ ] Build and push final image with version tag
- [ ] Update Helm chart version in `Chart.yaml`
- [ ] Test in staging environment
- [ ] Configure ingress with TLS
- [ ] Set up monitoring/alerting
- [ ] Document any custom configuration
- [ ] Verify RBAC permissions
- [ ] Test report generation workflow end-to-end
- [ ] Backup existing configuration

## Next Steps

After successful deployment:

1. **Configure Ingress**: Set up external access with proper TLS certificates
2. **Add Authentication**: Consider adding OAuth or basic auth for security
3. **Set up Monitoring**: Add Prometheus metrics for the frontend
4. **Automate Report Generation**: Create CronJobs to generate reports regularly
5. **User Training**: Document how users should access and use the UI

## Support

For issues or questions:

- Check the main [README.md](../README.md)
- Review the [Frontend README](README.md)
- Check Kubernetes logs: `kubectl logs -n domino-platform deployment/docker-registry-cleaner-frontend`
- Review the Helm chart: `charts/docker-registry-cleaner/`

## Resources

- [Flask Documentation](https://flask.palletsprojects.com/)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Helm Documentation](https://helm.sh/docs/)
- [Docker Registry Cleaner Main Repo](https://github.com/dominodatalab/docker-registry-cleaner)
