# Uses Chainguard-style bases (cgr.dev) for fewer CVEs; pin by digest in production, e.g.:
#   FROM cgr.dev/dominodatalab.com/python:3.14.2@sha256:...
# ------------------------------------------------

# 1) Install frontend dependencies into a venv
FROM cgr.dev/dominodatalab.com/python:3.14.2-dev AS dev
WORKDIR /frontend
RUN python -m venv venv
ENV PATH="/frontend/venv/bin:${PATH}"

# Copy pyproject.toml and install only the [frontend] extras.
# tomllib is built into Python 3.11+, so no extra tooling is needed.
# This avoids pulling in the heavy backend dependencies (kubernetes, pymongo, boto3, etc.).
COPY pyproject.toml .
RUN python -c "\
import tomllib, subprocess, sys; \
data = tomllib.load(open('pyproject.toml', 'rb')); \
deps = data['project']['optional-dependencies']['frontend']; \
subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir'] + deps, check=True)"

# 2) Final runtime: minimal Python + app
FROM cgr.dev/dominodatalab.com/python:3.14.2
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
