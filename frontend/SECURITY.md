# Frontend Security Design

## Overview

The Docker Registry Cleaner web UI is **intentionally read-only** for security reasons. This document explains the security decisions and architecture.

## Security Principles

### 1. Principle of Least Privilege

The frontend container has **minimal permissions**:
- ✅ Read-only access to reports volume
- ✅ No kubectl access
- ✅ No access to Kubernetes secrets
- ✅ No access to MongoDB or Docker registry
- ✅ No ability to execute cleanup commands

### 2. No Security by Obscurity

Even "safe" operations like dry-runs should not be advertised in the UI because:
- **Information Disclosure**: Showing kubectl commands teaches users how to run operations
- **Social Engineering**: Attackers could use displayed commands to craft phishing attacks
- **Escalation Path**: Even read-only commands reveal system architecture and naming conventions

### 3. Defense in Depth

Multiple layers prevent unauthorized operations:
1. **UI Layer**: No operations page or command execution
2. **Backend Layer**: No command execution endpoints in Flask app
3. **Container Layer**: CLI not installed in frontend container
4. **Kubernetes Layer**: RBAC controls who can exec into main container
5. **Application Layer**: Main CLI requires proper authorization

## Design Decisions

### Why Not Show kubectl Commands?

**Original Design (Rejected)**:
```html
<!-- DON'T DO THIS -->
<button onclick="copyCommand()">
  Copy: kubectl exec -it docker-registry-cleaner-0 -n domino-platform --
    docker-registry-cleaner delete_archived_tags --environment
</button>
```

**Problems**:
- ❌ Advertises exact commands to potentially unauthorized users
- ❌ Reveals pod names, namespaces, and command syntax
- ❌ Provides a "recipe" for operations that should be restricted
- ❌ Could encourage users to run commands they don't understand
- ❌ Makes it easier for attackers to craft social engineering attacks

**Current Design (Secure)**:
- ✅ Web UI shows reports only
- ✅ Commands documented in README (requires intentional access)
- ✅ Operators must have kubectl access and know how to use it
- ✅ No "easy button" for potentially destructive operations

### Why Not Execute Commands from UI?

**Option 1: Unified Image (Rejected)**
- Including CLI in frontend would require:
  - kubectl binary
  - skopeo binary
  - MongoDB credentials
  - Keycloak credentials
  - RBAC permissions for Kubernetes operations
- This violates least privilege principle
- Frontend shouldn't need access to infrastructure secrets

**Option 2: Sidecar with kubectl (Rejected)**
- Frontend could exec into main container via kubectl
- Requires:
  - kubectl in frontend container
  - ServiceAccount with exec permissions
  - Complex error handling and security
- Still violates least privilege
- Creates unnecessary attack surface

**Option 3: Read-Only UI (Chosen) ✅**
- Frontend shows reports only
- Operations run via `kubectl exec` by authorized users
- Clean separation of concerns
- Minimal attack surface

## Comparison with domino-admin-toolkit

### domino-admin-toolkit Architecture

The admin-toolkit **can** execute commands from the UI because:
1. **Single unified image**: Web UI + CLI in same container
2. **Different threat model**: Internal Domino tool for admin users
3. **Controlled access**: Typically behind Domino auth and network policies
4. **Test execution**: Runs pytest tests, not destructive operations

### docker-registry-cleaner Architecture

The registry-cleaner uses **read-only UI** because:
1. **Higher risk operations**: Deletes Docker images and MongoDB records
2. **Broader audience**: May be accessed by various users for reports
3. **Security-first**: Follows principle of least privilege
4. **Clear authorization**: kubectl access = authorization to operate

## Threat Model

### Threats Mitigated

1. **Unauthorized Command Execution**
   - Threat: User with web UI access tries to delete images
   - Mitigation: No command execution from UI; requires kubectl access

2. **Information Disclosure**
   - Threat: Attacker learns system architecture from UI
   - Mitigation: No operational commands or details exposed

3. **Social Engineering**
   - Threat: Attacker uses displayed commands in phishing
   - Mitigation: Commands not displayed; must reference documentation

4. **Privilege Escalation**
   - Threat: Compromised frontend container used to attack cluster
   - Mitigation: Frontend has no cluster permissions

5. **Accidental Deletion**
   - Threat: User accidentally clicks wrong button
   - Mitigation: No buttons to click; must use kubectl deliberately

### Residual Risks

1. **Authorized User Misuse**
   - Users with kubectl access can still run operations
   - Mitigated by: Audit logging, RBAC, dry-run defaults

2. **Report Data Exposure**
   - Reports may contain sensitive information
   - Mitigated by: Network policies, ingress auth (if enabled)

## Best Practices for Deployment

### Required Security Measures

1. **Network Isolation**
   ```yaml
   # NetworkPolicy to restrict frontend access
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: docker-registry-cleaner-frontend
   spec:
     podSelector:
       matchLabels:
         app: docker-registry-cleaner
     policyTypes:
     - Ingress
     ingress:
     - from:
       - namespaceSelector:
           matchLabels:
             name: domino-platform
   ```

2. **Ingress Authentication**
   ```yaml
   frontend:
     ingress:
       enabled: true
       annotations:
         nginx.ingress.kubernetes.io/auth-type: basic
         nginx.ingress.kubernetes.io/auth-secret: basic-auth
   ```

3. **Read-Only Root Filesystem**
   ```yaml
   securityContext:
     readOnlyRootFilesystem: true
     runAsNonRoot: true
     runAsUser: 65532
   ```

### Optional Enhancements

1. **OAuth/OIDC Integration**
   - Add authentication to frontend
   - Integrate with Domino auth or corporate SSO

2. **Audit Logging**
   - Log all report views
   - Track who accessed which reports

3. **Rate Limiting**
   - Prevent report enumeration attacks
   - Limit download frequency

## Alternative Architectures Considered

### A. Command API with Authorization

**Design**: Frontend calls backend API, which checks permissions before executing

**Pros**:
- More user-friendly
- Could implement fine-grained permissions

**Cons**:
- Complex authorization logic
- Larger attack surface
- Requires custom auth/authz implementation
- Still advertises available operations

**Verdict**: ❌ Too complex, violates least privilege

### B. Webhook-Based Execution

**Design**: UI triggers webhook, which creates Kubernetes Job to run command

**Pros**:
- Jobs run with separate ServiceAccount
- Better audit trail

**Cons**:
- Still allows triggering from UI
- Complex Job template management
- Requires additional RBAC setup

**Verdict**: ❌ Unnecessary complexity for the benefit

### C. Read-Only UI (Chosen)

**Design**: UI shows reports only; operations via kubectl

**Pros**:
- Simplest design
- Minimal attack surface
- Clear authorization boundary
- Easy to audit (kubectl logs)
- Follows least privilege

**Cons**:
- Less convenient for users
- Requires kubectl knowledge

**Verdict**: ✅ **Best balance of security and simplicity**

## Conclusion

The read-only web UI provides:
- **Security**: Minimal attack surface and privilege
- **Clarity**: Operations require explicit kubectl access
- **Auditability**: All operations via kubectl are logged
- **Simplicity**: No complex authorization logic needed

For a tool that can delete Docker images and MongoDB records, this conservative approach is appropriate.

## References

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Kubernetes Security Best Practices](https://kubernetes.io/docs/concepts/security/security-best-practices/)
- [Principle of Least Privilege](https://en.wikipedia.org/wiki/Principle_of_least_privilege)
