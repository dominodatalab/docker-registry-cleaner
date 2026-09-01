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
from report_scope import get_filter_for_filename

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
# How long a resolved org-scope (from the backend's /api/org-scope, itself a
# real Mongo aggregation) is cached before re-fetching — a separate knob from
# _AUTH_CACHE_TTL since org membership/ownership changes far less often than
# an admin session's validity, and this call is considerably more expensive
# per hit than the principal/org-membership checks above.
_ORG_SCOPE_CACHE_TTL = int(os.environ.get("ORG_SCOPE_CACHE_TTL_SECONDS", "60"))
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
        if cached_scope == SCOPE_ORG:
            return _reject_org_scoped_operations()
        if cached_scope == SCOPE_ADMIN:
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

    orgs = _fetch_orgs(cookie_header)
    if orgs is None:
        # Org-membership lookup was unreachable — fail closed, same as the
        # principal check above.
        return _auth_service_unavailable()

    if orgs:
        _cache_scope(SCOPE_ORG, username=username, orgs=orgs)
        return _reject_org_scoped_operations()

    _cache_scope(SCOPE_DENIED, username=username)
    return _deny(authenticated=True)


def _fetch_orgs(cookie_header: str) -> Optional[List[Dict[str, str]]]:
    """Return {"id", "name"} for every org the cookie-forwarded caller
    belongs to. Names are kept (not just ids) so the multi-org selector
    (U1) has something readable to label its checkboxes with — org_ids
    alone was sufficient for A2/D4's filtering but not for a usable UI.

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
    return [{"id": org["id"], "name": org.get("name") or org["id"]} for org in orgs if "id" in org]


def _cache_scope(scope: str, username: str = "", orgs: Optional[List[Dict[str, str]]] = None):
    """Store a resolved access scope in the Flask session for AUTH_CACHE_TTL_SECONDS."""
    orgs = orgs or []
    session["access_scope"] = scope
    session["domino_username"] = username
    session["org_ids"] = [o["id"] for o in orgs]
    session["org_names"] = {o["id"]: o["name"] for o in orgs}
    session["auth_checked_at"] = time.time()


# Job-triggering / operation-launching endpoints — org-scoped members get
# read-only reporting only, no job-triggering, no destructive actions
# (docs/org-scoped-access-plan.md §3.2, decided). The UI already hides
# these (base.html's nav, U2), but that's not enforcement — a scoped
# session hitting one of these routes directly must be rejected here too
# (defense in depth, same §3.2), even though the *backend's* own API-key
# identity gap (§2.1, out of scope for v1) means the backend itself can't
# yet distinguish an org-scoped caller from an admin one.
_OPERATIONS_ENDPOINTS = {
    "operations",
    "proxy_list_operations",
    "proxy_list_jobs",
    "proxy_create_job",
    "proxy_get_job",
    "proxy_cancel_job",
}

_ORG_READ_ONLY_MESSAGE = (
    "This view is read-only for organization members: job-triggering and destructive "
    "operations require Domino administrator privileges."
)


def _reject_org_scoped_operations():
    """Called once a request's access scope is confirmed org-scoped (both
    the cache-hit and freshly-resolved paths above) — rejects the request
    if it's aimed at the operations surface, otherwise lets it through
    (returning None from a before_request hook means "continue normally").
    """
    if request.endpoint in _OPERATIONS_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify({"error": _ORG_READ_ONLY_MESSAGE}), 403
        return render_template("error.html", message=_ORG_READ_ONLY_MESSAGE), 403
    return None


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
    org_ids = session.get("org_ids", [])
    org_names = session.get("org_names", {})
    return {
        "domino_username": session.get("domino_username", ""),
        "is_domino_admin": scope == SCOPE_ADMIN,
        "is_org_scoped": scope == SCOPE_ORG,
        "org_ids": org_ids,
        # {"id", "name"} pairs for every org the session belongs to — the
        # multi-org selector (U1) renders these as its checkbox labels.
        "orgs": [{"id": oid, "name": org_names.get(oid, oid)} for oid in org_ids],
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _backend_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if BACKEND_API_KEY:
        headers["X-API-Key"] = BACKEND_API_KEY
    return headers


_EMPTY_ORG_SCOPE = {"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}}


def _get_org_scope(org_ids_override: Optional[List[str]] = None) -> Optional[Dict]:
    """Fetch (or return the cached) org-scope resolution for the current
    org-scoped session, via the backend's GET /api/org-scope
    (docs/org-scoped-access-plan.md §3.3, Option C). Cached in the Flask
    session for ORG_SCOPE_CACHE_TTL_SECONDS, since this triggers real Mongo
    aggregation work on the backend — not something to redo on every single
    report view.

    org_ids_override, when given, narrows the resolution to a subset of
    the session's own orgs (U1's multi-org selector) — clamped to the
    session's actual org_ids here, not just trusted from the caller, so a
    scoped session can never resolve scope for an org it doesn't belong
    to no matter what a request asks for. A narrowed request always hits
    the backend fresh rather than using/updating the session cache: it's a
    deliberate, occasional interactive choice (a user unchecking an org),
    not the page-load hot path the cache exists for, and caching every
    distinct subset a session might ask for isn't worth the complexity.

    Returns None if the backend call itself failed (as opposed to
    succeeding with an empty scope) — callers must treat that as "couldn't
    determine scope" and fail closed, not fail open.
    """
    session_org_ids = session.get("org_ids", [])
    if org_ids_override is not None:
        org_ids = [oid for oid in org_ids_override if oid in session_org_ids]
    else:
        org_ids = session_org_ids

    if not org_ids:
        return dict(_EMPTY_ORG_SCOPE)

    if org_ids_override is None:
        cached_at = session.get("org_scope_checked_at")
        if cached_at is not None and (time.time() - cached_at) < _ORG_SCOPE_CACHE_TTL:
            cached = session.get("org_scope")
            if cached is not None:
                return cached

    try:
        resp = httpx.get(
            f"{BACKEND_API_URL}/api/org-scope",
            params=[("org_id", oid) for oid in org_ids],
            headers=_backend_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        scope = resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching org scope: {e}")
        return None

    if org_ids_override is None:
        session["org_scope"] = scope
        session["org_scope_checked_at"] = time.time()
    return scope


def _org_ids_override() -> Optional[List[str]]:
    """Read a `?narrow=1&org_ids=...` narrowing selection off the query
    string (U1's multi-org selector) — the explicit `narrow` marker (not
    just the presence/absence of `org_ids`) is what distinguishes "no
    override, use the session's full org set" from "the user deliberately
    narrowed to a subset, possibly the empty subset" (unchecking every
    box is a valid, meaningful selection: show nothing)."""
    if request.args.get("narrow") != "1":
        return None
    return request.args.getlist("org_ids")


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


def load_report(filename: str, org_scope: Optional[Dict] = None) -> Optional[Dict]:
    """Load and parse a JSON report file, filtered to the caller's org
    scope when the session is org-scoped (docs/org-scoped-access-plan.md
    §3.2/§3.3). Admin sessions are unaffected — this returns exactly what
    it always has for them.

    org_scope, when given, is used instead of fetching it here — callers
    that also need the same scope resolution for something else (U1's
    narrowed re-fetch, U3's "also used by" note) fetch it once themselves
    and pass it in, rather than this function silently re-fetching (and
    re-hitting the backend/session cache) a second time. Defaults to None,
    which preserves this function's original behavior exactly: resolve the
    session's own (unnarrowed) scope itself via _get_org_scope().
    """
    try:
        file_path = REPORTS_DIR / filename
        if not file_path.exists():
            return None
        with open(file_path, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading report {filename}: {e}")
        return None

    if session.get("access_scope") == SCOPE_ORG:
        filter_fn = get_filter_for_filename(filename)
        if filter_fn is None:
            # Not a known user-facing report type (e.g. final-report.json,
            # mongodb_usage_report.json) — org-scoped sessions never see
            # these, whether by browsing or by requesting the filename
            # directly. Treat identically to "file not found".
            return None
        if org_scope is None:
            org_scope = _get_org_scope()
        if org_scope is None:
            # Backend unreachable — fail closed, same philosophy as every
            # other network call this feature makes (§2.1/§3.1).
            return None
        data = filter_fn(data, set(org_scope["tags"]))

    return data


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


@app.route("/api/org-scope-view")
def api_org_scope_view():
    """Frontend-facing view of the current session's resolved org scope,
    optionally narrowed via `?narrow=1&org_ids=...` (U1's multi-org
    selector). Distinct from the backend's own GET /api/org-scope (which
    this proxies via _get_org_scope) — this is what report.html's JS polls
    on every checkbox change, both for U1's re-filtering and for U3's
    "also used by" note (the `other_owners` field), since narrowing the
    org selection changes both.

    Admin sessions get the empty scope shape rather than a real call —
    meaningless for them, and matches every other org-scope code path in
    this file: admins never hit the backend's /api/org-scope at all.
    """
    if session.get("access_scope") != SCOPE_ORG:
        return jsonify(dict(_EMPTY_ORG_SCOPE))
    scope = _get_org_scope(_org_ids_override())
    if scope is None:
        return jsonify({"error": "Backend unavailable"}), 503
    return jsonify(scope)


@app.route("/api/reports/<filename>")
def api_report_detail(filename):
    """API endpoint to get report content"""
    org_scope = _get_org_scope(_org_ids_override()) if session.get("access_scope") == SCOPE_ORG else None
    report_data = load_report(filename, org_scope=org_scope)
    if report_data is None:
        return jsonify({"error": "Report not found"}), 404
    return jsonify(report_data)


@app.route("/reports/<filename>")
def view_report(filename):
    """View a specific report"""
    is_org_scoped_session = session.get("access_scope") == SCOPE_ORG
    org_scope = _get_org_scope(_org_ids_override()) if is_org_scoped_session else None
    report_data = load_report(filename, org_scope=org_scope)
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
    other_owners = (org_scope or {}).get("other_owners", {})
    return render_template(
        "report.html",
        filename=filename,
        report_type=report_type,
        report_data=json.dumps(report_data, indent=2),
        other_owners=json.dumps(other_owners),
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
