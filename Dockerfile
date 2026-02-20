# Unified image: Python app + skopeo in a single hardened image.
# Uses Chainguard-style bases (cgr.dev) for fewer CVEs; pin by digest in production, e.g.:
#   FROM cgr.dev/dominodatalab.com/python:3.14.2@sha256:...
#   FROM cgr.dev/dominodatalab.com/skopeo:1.21.0@sha256:...
# ------------------------------------------------

# 1) Build Python dependencies and app into a venv
FROM cgr.dev/dominodatalab.com/python:3.14.2-dev AS dev
WORKDIR /app
RUN python -m venv venv
ENV PATH="/app/venv/bin:${PATH}"
COPY pyproject.toml .
COPY python python
RUN pip install --no-cache-dir -e .

# 2) Source hardened skopeo binary from dedicated image
FROM cgr.dev/dominodatalab.com/skopeo:1.21.0 AS skopeo

# 3) Final runtime: minimal Python + app + skopeo
FROM cgr.dev/dominodatalab.com/python:3.14.2
WORKDIR /app

# Copy app + venv from dev stage, owned by nonroot
COPY --from=dev --chown=nonroot:nonroot /app /app
ENV PATH="/app/venv/bin:${PATH}"

# Copy skopeo binary from hardened image, owned by nonroot
COPY --from=skopeo --chown=nonroot:nonroot /usr/bin/skopeo /usr/bin/skopeo

# Run as nonroot when the base supports it (Chainguard-style images use nonroot:65532)
# If your base does not define nonroot, remove the USER line or set your runtime user.
USER nonroot:nonroot

# Clear the base image's ENTRYPOINT and set our own CMD
# Base image has ENTRYPOINT ["/usr/bin/python"], which we don't want
ENTRYPOINT []

# Start the backend API server on port 8081.
# This port is not exposed by any Kubernetes Service — it is only reachable
# from within the pod (i.e. by the frontend sidecar via localhost:8081).
# CLI operations are still available via: kubectl exec -it <pod> -- docker-registry-cleaner <command>
CMD ["python", "-m", "uvicorn", "python.api:app", "--host", "0.0.0.0", "--port", "8081"]
