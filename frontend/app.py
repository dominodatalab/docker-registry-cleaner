"""
Flask web interface for Docker Registry Cleaner.
Serves a read-only report viewer and proxies operation requests to the
backend API running on localhost:8081.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from flask import Flask, jsonify, redirect, render_template, request, session

# Configuration
REPORTS_DIR = Path("/app/reports")  # In container
HOST = "0.0.0.0"
PORT = 8080
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "http://localhost:8081")
BACKEND_API_KEY = os.environ.get("BACKEND_API_KEY", "")
FLASK_BASE_PATH = os.environ.get("FLASK_BASE_PATH", "")
# Internal URL of the Domino nucleus-frontend service.  When set, every request
# is authenticated by forwarding the user's dominoAuth cookie to the Domino API
# and verifying the caller's access scope (admin, org-scoped, or denied).
# Leave unset to disable auth (useful for local development).
DOMINO_API_URL = os.environ.get("DOMINO_API_URL", "")
# Public URL of the Domino deployment, used to build clickable links to assets
# (runs, workspaces, projects, etc.) in reports.  Should be the external hostname,
# e.g. https://my-domino.example.com.
DOMINO_URL = os.environ.get("DOMINO_URL", "")
# How long a resolved access scope is trusted before re-checking with Nucleus.
_AUTH_CACHE_TTL = int(os.environ.get("AUTH_CACHE_TTL_SECONDS", "60"))
# Nucleus path for "orgs the calling (cookie-forwarded) user belongs to" —
# see docs/org-scoped-access-plan.md Appendix C for the confirmed response shape.
_ORGANIZATIONS_API_PATH = "/api/organizations/v1/organizations"

# Access-scope literals stored in the Flask session and returned by
# resolve_access_scope(). See docs/org-scoped-access-plan.md §3.1.
SCOPE_ADMIN = "admin"
SCOPE_ORG = "org_scoped"
SCOPE_DENIED = "denied"


# Flask app setup
app = Flask(__name__, static_url_path="/static", static_folder="templates/static")
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

if FLASK_BASE_PATH:
    # nginx rewrites /registry-cleaner/foo → /foo before forwarding to Flask, but
    # url_for() needs SCRIPT_NAME to generate correct prefixed URLs. This middleware
    # injects SCRIPT_NAME from FLASK_BASE_PATH so all links work without relying on
    # the configuration-snippet annotation (which is often blocked by cluster policy).
    _base = FLASK_BASE_PATH
    _inner = app.wsgi_app

    def _prefix_middleware(environ, start_response):
        environ["SCRIPT_NAME"] = _base
        return _inner(environ, start_response)

    app.wsgi_app = _prefix_middleware


# ── Authentication ──────────────────────────────────────────────────────────────


@app.before_request
def resolve_access_scope():
    """Resolve the caller into one of three access scopes: admin, org-scoped,
    or denied. See docs/org-scoped-access-plan.md §3.1.

    Forwards all browser cookies to the Domino API to verify identity and,
    for non-admins, org membership.  Sending the full Cookie header handles
    both vanilla Domino deployments (dominoAuth cookie) and Keycloak-based
    SSO deployments that use different session cookies.  The resolved scope
    is cached in the Flask session for AUTH_CACHE_TTL_SECONDS (default 60)
    to avoid one or two Domino API calls on every page load.

    Skipped when DOMINO_API_URL is not configured (local dev mode).
    Skipped for the /health endpoint (used by Kubernetes liveness probes).
    """
    if not DOMINO_API_URL:
        return  # auth disabled — local dev

    if request.endpoint == "health" or request.path.startswith("/static/"):
        return

    cached_at = session.get("auth_checked_at")
    if cached_at is not None and (time.time() - cached_at) < _AUTH_CACHE_TTL:
        cached_scope = session.get("access_scope")
        if cached_scope == SCOPE_DENIED:
            return _deny(authenticated=True)
        if cached_scope in (SCOPE_ADMIN, SCOPE_ORG):
            return
        # Missing/unrecognized cache entry — fall through and re-check.

    cookie_header = request.headers.get("Cookie", "")
    if not cookie_header:
        return _deny(authenticated=False)

    try:
        resp = httpx.get(
            f"{DOMINO_API_URL}/v4/auth/principal",
            headers={"Cookie": cookie_header},
            timeout=5,
        )
    except httpx.RequestError:
        return _auth_service_unavailable()

    if resp.status_code != 200:
        return _deny(authenticated=False)

    principal = resp.json()
    username = principal.get("canonicalName", "")

    if principal.get("isAdmin", False):
        _cache_scope(SCOPE_ADMIN, username=username)
        return

    org_ids = _fetch_org_ids(cookie_header)
    if org_ids is None:
        # Org-membership lookup was unreachable — fail closed, same as the
        # principal check above.
        return _auth_service_unavailable()

    if org_ids:
        _cache_scope(SCOPE_ORG, username=username, org_ids=org_ids)
        return

    _cache_scope(SCOPE_DENIED, username=username)
    return _deny(authenticated=True)


def _fetch_org_ids(cookie_header: str) -> Optional[List[str]]:
    """Return the ids of the orgs the cookie-forwarded caller belongs to.

    Returns None (not an empty list) if the lookup itself couldn't be made —
    that's the "Nucleus unreachable" case, distinct from "reachable, caller
    has zero org memberships".
    """
    try:
        resp = httpx.get(
            f"{DOMINO_API_URL}{_ORGANIZATIONS_API_PATH}",
            headers={"Cookie": cookie_header},
            timeout=5,
        )
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    orgs = resp.json().get("orgs", [])
    return [org["id"] for org in orgs if "id" in org]


def _cache_scope(scope: str, username: str = "", org_ids: Optional[List[str]] = None):
    """Store a resolved access scope in the Flask session for AUTH_CACHE_TTL_SECONDS."""
    session["access_scope"] = scope
    session["domino_username"] = username
    session["org_ids"] = org_ids or []
    session["auth_checked_at"] = time.time()


def _auth_service_unavailable():
    """Fail closed when a Domino API call needed to resolve scope is unreachable."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication service unavailable"}), 503
    return (
        render_template("error.html", message="Authentication service unavailable. Please try again shortly."),
        503,
    )


_DENIED_MESSAGE = (
    "Access denied: this application requires Domino administrator privileges or membership in a Domino organization."
)


def _deny(authenticated: bool):
    """Return the appropriate response when access is denied."""
    if request.path.startswith("/api/"):
        status = 403 if authenticated else 401
        msg = _DENIED_MESSAGE if authenticated else "Authentication required."
        return jsonify({"error": msg}), status
    if authenticated:
        return render_template("error.html", message=_DENIED_MESSAGE), 403
    # Not logged in — send to Domino's own login page (same hostname, root path).
    return redirect("/")


@app.context_processor
def inject_auth():
    """Make the logged-in username and resolved access scope available in all templates."""
    scope = session.get("access_scope", "")
    return {
        "domino_username": session.get("domino_username", ""),
        "is_domino_admin": scope == SCOPE_ADMIN,
        "is_org_scoped": scope == SCOPE_ORG,
        "org_ids": session.get("org_ids", []),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _backend_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if BACKEND_API_KEY:
        headers["X-API-Key"] = BACKEND_API_KEY
    return headers


_USER_FACING_REPORT_PREFIXES = (
    "deletion-analysis",
    "archived-tags",
    "unused-environments",
    "old-revisions",
    "image-size-report",
    "user-size-report",
    "integrity-check",
)


def get_report_files() -> List[Dict]:
    """Get list of report files with metadata"""
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for file_path in sorted(
        (p for p in REPORTS_DIR.glob("*.json") if p.name.startswith(_USER_FACING_REPORT_PREFIXES)),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    ):
        stat = file_path.stat()
        reports.append(
            {
                "name": file_path.name,
                "size": format_bytes(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp": stat.st_mtime,
            }
        )
    return reports


def format_bytes(bytes_size: int) -> str:
    """Format bytes to human-readable size"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"


def load_report(filename: str) -> Optional[Dict]:
    """Load and parse a JSON report file"""
    try:
        file_path = REPORTS_DIR / filename
        if not file_path.exists():
            return None
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading report {filename}: {e}")
        return None


# ── Report routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Main page - list all available reports"""
    reports = get_report_files()
    return render_template("index.html", reports=reports)


@app.route("/api/reports")
def api_reports():
    """API endpoint to get list of reports"""
    reports = get_report_files()
    return jsonify(reports)


@app.route("/api/reports/<filename>")
def api_report_detail(filename):
    """API endpoint to get report content"""
    report_data = load_report(filename)
    if report_data is None:
        return jsonify({"error": "Report not found"}), 404
    return jsonify(report_data)


@app.route("/reports/<filename>")
def view_report(filename):
    """View a specific report"""
    report_data = load_report(filename)
    if report_data is None:
        return "Report not found", 404

    # Determine report type based on filename
    report_type = "generic"
    if "mongodb_usage" in filename:
        report_type = "mongodb_usage"
    elif "image-size" in filename or "user-size" in filename:
        report_type = "size_report"
    elif "archived-tags" in filename:
        report_type = "archived_tags"
    elif "unused-environments" in filename:
        report_type = "unused_environments"
    elif "integrity-check" in filename:
        report_type = "integrity_check"
    elif "deletion" in filename:
        report_type = "deletion_results"
    elif "final-report" in filename:
        report_type = "final_report"

    domino_url = DOMINO_URL.rstrip("/") if DOMINO_URL else ""
    return render_template(
        "report.html",
        filename=filename,
        report_type=report_type,
        report_data=json.dumps(report_data, indent=2),
        domino_url=domino_url,
    )


# ── Operations page ────────────────────────────────────────────────────────────


@app.route("/operations")
def operations():
    """Operations page — trigger backend jobs from the UI"""
    return render_template("operations.html")


# ── Backend API proxy routes ───────────────────────────────────────────────────
# The browser cannot reach localhost:8081 directly (it is inside the pod).
# These routes forward requests from the browser to the backend API.


@app.route("/api/operations")
def proxy_list_operations():
    """Proxy: GET /api/operations → backend"""
    try:
        resp = httpx.get(f"{BACKEND_API_URL}/api/operations", headers=_backend_headers(), timeout=10)
        return jsonify(resp.json()), resp.status_code
    except httpx.ConnectError:
        return jsonify({"error": "Backend API is unavailable"}), 503


@app.route("/api/jobs", methods=["GET"])
def proxy_list_jobs():
    """Proxy: GET /api/jobs → backend"""
    try:
        resp = httpx.get(f"{BACKEND_API_URL}/api/jobs", headers=_backend_headers(), timeout=10)
        return jsonify(resp.json()), resp.status_code
    except httpx.ConnectError:
        return jsonify({"error": "Backend API is unavailable"}), 503


@app.route("/api/jobs", methods=["POST"])
def proxy_create_job():
    """Proxy: POST /api/jobs → backend"""
    try:
        resp = httpx.post(
            f"{BACKEND_API_URL}/api/jobs",
            headers=_backend_headers(),
            json=request.get_json(),
            timeout=10,
        )
        return jsonify(resp.json()), resp.status_code
    except httpx.ConnectError:
        return jsonify({"error": "Backend API is unavailable"}), 503


@app.route("/api/jobs/<job_id>", methods=["GET"])
def proxy_get_job(job_id):
    """Proxy: GET /api/jobs/{job_id} → backend"""
    try:
        resp = httpx.get(f"{BACKEND_API_URL}/api/jobs/{job_id}", headers=_backend_headers(), timeout=10)
        return jsonify(resp.json()), resp.status_code
    except httpx.ConnectError:
        return jsonify({"error": "Backend API is unavailable"}), 503


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def proxy_cancel_job(job_id):
    """Proxy: DELETE /api/jobs/{job_id} → backend"""
    try:
        resp = httpx.delete(f"{BACKEND_API_URL}/api/jobs/{job_id}", headers=_backend_headers(), timeout=10)
        return jsonify(resp.json()), resp.status_code
    except httpx.ConnectError:
        return jsonify({"error": "Backend API is unavailable"}), 503


# ── Health ─────────────────────────────────────────────────────────────────────


@app.route("/health")
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


if __name__ == "__main__":
    from waitress import serve

    print(f"Starting Docker Registry Cleaner Web UI on {HOST}:{PORT}")
    serve(app, host=HOST, port=PORT)
