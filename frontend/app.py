"""
Flask web interface for Docker Registry Cleaner.
Serves a read-only report viewer and proxies operation requests to the
backend API running on localhost:8081.
"""

import json
import os
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
# and verifying that the caller is a system administrator.  Leave unset to
# disable auth (useful for local development).
DOMINO_API_URL = os.environ.get("DOMINO_API_URL", "")
# External URL of the Domino web UI, used to build clickable links to assets
# (runs, workspaces, projects, etc.) in reports.  Should be the public hostname,
# e.g. https://my-domino.example.com.  Defaults to DOMINO_API_URL if not set
# (works when the internal and external URLs are the same).
DOMINO_UI_URL = os.environ.get("DOMINO_UI_URL", "") or DOMINO_API_URL


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
def require_domino_admin():
    """Validate the caller is a Domino system administrator.

    Forwards all browser cookies to the Domino API to verify identity and
    admin status.  Sending the full Cookie header handles both vanilla Domino
    deployments (dominoAuth cookie) and Keycloak-based SSO deployments that
    use different session cookies.  The result is cached in the Flask session
    for _AUTH_CACHE_TTL seconds to avoid a Domino API call on every page load.

    Skipped when DOMINO_API_URL is not configured (local dev mode).
    Skipped for the /health endpoint (used by Kubernetes liveness probes).
    """
    if not DOMINO_API_URL:
        return  # auth disabled — local dev

    if request.endpoint == "health" or request.path.startswith("/static/"):
        return

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
        # Domino API is unreachable — fail closed.
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication service unavailable"}), 503
        return (
            render_template("error.html", message="Authentication service unavailable. Please try again shortly."),
            503,
        )

    if resp.status_code != 200:
        return _deny(authenticated=False)

    principal = resp.json()
    if not principal.get("isAdmin", False):
        return _deny(authenticated=True)

    session["domino_username"] = principal.get("canonicalName", "")
    session["is_domino_admin"] = principal.get("isAdmin", False)


def _deny(authenticated: bool):
    """Return the appropriate response when access is denied."""
    if request.path.startswith("/api/"):
        status = 403 if authenticated else 401
        msg = "Administrator privileges required." if authenticated else "Authentication required."
        return jsonify({"error": msg}), status
    if authenticated:
        return render_template("error.html", message="Access denied: Domino administrator privileges required."), 403
    # Not logged in — send to Domino's own login page (same hostname, root path).
    return redirect("/")


@app.context_processor
def inject_auth():
    """Make the logged-in username available in all templates."""
    return {
        "domino_username": session.get("domino_username", ""),
        "is_domino_admin": session.get("is_domino_admin", False),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _backend_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if BACKEND_API_KEY:
        headers["X-API-Key"] = BACKEND_API_KEY
    return headers


def get_report_files() -> List[Dict]:
    """Get list of report files with metadata"""
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for file_path in sorted(
        REPORTS_DIR.glob("*.json"),
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
    elif "deletion" in filename:
        report_type = "deletion_results"
    elif "final-report" in filename:
        report_type = "final_report"

    domino_url = DOMINO_UI_URL.rstrip("/") if DOMINO_UI_URL else ""
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
