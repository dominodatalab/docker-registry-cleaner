# Uses Chainguard-style bases (cgr.dev) for fewer CVEs; pin by digest in production, e.g.:
#   FROM cgr.dev/dominodatalab.com/python:3.14.2@sha256:...
# ------------------------------------------------

# 1) Install frontend dependencies into a venv
FROM cgr.dev/dominodatalab.com/python:3.14.5-dev AS dev
WORKDIR /frontend
RUN python -m venv venv
ENV PATH="/frontend/venv/bin:${PATH}"

# Install pinned dependencies from the lock file (Flask, httpx, waitress + their transitive deps).
# To regenerate frontend/requirements.lock: pip-compile --annotation-style=line --no-emit-index-url --output-file=frontend/requirements.lock frontend/requirements.in
COPY frontend/requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# 2) Final runtime: minimal Python + app
FROM cgr.dev/dominodatalab.com/python:3.14.5
WORKDIR /frontend

# Copy venv from dev stage, owned by nonroot
COPY --from=dev --chown=nonroot:nonroot /frontend/venv /frontend/venv
ENV PATH="/frontend/venv/bin:${PATH}"

# Copy application files (built from project root)
COPY --chown=nonroot:nonroot frontend/app.py .
COPY --chown=nonroot:nonroot frontend/templates templates/

USER nonroot:nonroot

# Clear the base image's ENTRYPOINT and set our own CMD
# Base image has ENTRYPOINT ["/usr/bin/python"], which we don't want
ENTRYPOINT []

EXPOSE 8080

CMD ["python", "app.py"]
