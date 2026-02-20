# Frontend Architecture

## Sidecar Design

The Docker Registry Cleaner frontend runs as a **sidecar container** within the same StatefulSet pod as the main CLI tool.

### Why Sidecar?

The sidecar architecture was chosen to solve a critical constraint:

**PVC Access Mode Limitation**
- The StatefulSet uses a PVC with `ReadWriteOnce` (RWO) access mode
- RWO volumes can only be mounted by pods on a single node at a time
- A separate Deployment cannot mount the same PVC while the StatefulSet is running
- **Solution**: Run frontend as a sidecar in the same pod to share the volume

### Pod Structure

```
docker-registry-cleaner-0 Pod
├── docker-registry-cleaner (main container)
│   ├── Has CLI installed
│   ├── Mounts /data/reports (read-write)
│   ├── Generates reports
│   └── Executes cleanup operations
│
└── frontend (sidecar container)
    ├── Flask web application
    ├── Mounts /app/reports (read-only from same PVC)
    ├── Serves web UI
    └── Displays reports
```

### Volume Sharing

Both containers in the pod share the same PVC:
- **Main container**: Mounts at `/data/reports` (read-write)
- **Frontend container**: Mounts at `/app/reports` (read-only)
- Both point to the same underlying storage

```yaml
# In StatefulSet
volumeMounts:
  - name: reports-storage
    mountPath: /data/reports        # Main container
  - name: reports-storage
    mountPath: /app/reports          # Frontend container
    readOnly: true
```

## Command Execution

### Why Frontend Doesn't Execute Commands

The frontend container does **not** execute `docker-registry-cleaner` commands directly because:

1. **CLI Not Available**: The docker-registry-cleaner CLI is only installed in the main container
2. **Dependencies Missing**: The CLI requires kubectl, skopeo, MongoDB credentials, Keycloak access, etc.
3. **Image Size**: Including the full CLI and dependencies would significantly bloat the frontend image
4. **Separation of Concerns**: Frontend focuses on visualization; CLI container handles operations

### How Commands Are Run

The frontend provides **kubectl commands** that users copy and paste:

```bash
# Example command shown in UI:
kubectl exec -it docker-registry-cleaner-0 -n domino-platform -- docker-registry-cleaner health_check
```

Users run these commands in their own terminal, which executes them in the main container where the CLI is available.

### User Workflow

1. User browses reports in the web UI
2. User goes to Operations page
3. User clicks "Copy" button for desired command
4. kubectl command is copied to clipboard
5. User pastes and runs command in their terminal
6. Command executes in the main container
7. Results are visible via `kubectl logs` or in generated reports

## Network Access

### Service Configuration

The frontend Service selects the StatefulSet pods:

```yaml
selector:
  app.kubernetes.io/name: docker-registry-cleaner
  app.kubernetes.io/instance: <release-name>
```

This routes traffic to the frontend container's port 8080 within the StatefulSet pod.

### Access Methods

**Port Forward (Development/Testing)**
```bash
kubectl port-forward -n domino-platform svc/docker-registry-cleaner-frontend 8080:8080
```

**Ingress (Production)**
```yaml
frontend:
  ingress:
    enabled: true
    hosts:
      - host: registry-cleaner.example.com
```

## Deployment Considerations

### Enabling/Disabling Frontend

Toggle the frontend sidecar via Helm values:

```yaml
frontend:
  enabled: true  # Set to false to disable
```

When disabled:
- Only the main container runs
- No frontend Service/Ingress is created
- PVC is still available for main container

### Resource Allocation

The pod's total resources include both containers:

```yaml
# Main container
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 1000m
    memory: 1Gi

# Frontend container
frontend:
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi

# Total pod: ~150m CPU, ~384Mi memory (requests)
```

### Scaling

The StatefulSet is designed for single-replica operation:
- `replicas: 1` (not configurable)
- Frontend scales with the StatefulSet (same replica count)
- No independent scaling of frontend vs main container

## Alternative Architectures Considered

### ❌ Separate Deployment

**Why not used:**
- Would require separate PVC or ReadWriteMany access mode
- Most storage classes don't support RWX
- Would need complex volume sync mechanisms
- Increased resource overhead

### ❌ CLI in Frontend Image

**Why not used:**
- Would massively increase image size (CLI + kubectl + skopeo + dependencies)
- Would need MongoDB, Keycloak credentials in frontend
- Violates principle of least privilege
- Complicates frontend container security

### ❌ kubectl Exec from Frontend

**Why not used:**
- Requires RBAC permissions for frontend ServiceAccount
- Needs kubectl binary in frontend image
- Complex error handling
- Security concerns (frontend can exec into main container)

### ✅ Sidecar with kubectl Copy-Paste

**Chosen because:**
- Simplest implementation
- No PVC conflicts
- Clean separation of concerns
- Secure (no elevated permissions needed)
- Users maintain full control over command execution
- Follows principle of least privilege

## Security

### Container Isolation

- Frontend runs as non-root user (65532)
- Read-only access to reports volume
- No access to secrets (MongoDB, Keycloak, etc.)
- No network access to internal services

### User Access Control

- Users must have kubectl access to run commands
- Existing RBAC policies apply
- No authentication in frontend (relies on network policies)
- Consider adding Ingress authentication for production

## Future Enhancements

Potential improvements to the architecture:

1. **API-based Command Execution**
   - Main container exposes HTTP API
   - Frontend calls API instead of providing kubectl commands
   - Would enable real-time command execution via UI

2. **Shared Unix Socket**
   - Main container listens on Unix socket
   - Frontend communicates via socket
   - Faster than HTTP, no network exposure

3. **Kubernetes Job Creation**
   - Frontend creates Kubernetes Jobs to run commands
   - Jobs use same ServiceAccount and volumes
   - Better tracking and history

4. **Authentication Layer**
   - Add OAuth/OIDC to frontend
   - Integrate with Domino's auth system
   - Role-based access control

Currently, the copy-paste approach is the simplest and most maintainable solution given the constraints.
