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
from flask import Flask, jsonify, render_template, request, send_file

# Configuration
REPORTS_DIR = Path("/app/reports")  # In container
HOST = "0.0.0.0"
PORT = 8080
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "http://localhost:8081")
BACKEND_API_KEY = os.environ.get("BACKEND_API_KEY", "")

# Flask app setup
app = Flask(__name__, static_url_path="/static", static_folder="templates/static")
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)


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
    for file_path in sorted(REPORTS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = file_path.stat()
        reports.append({
            "name": file_path.name,
            "size": format_bytes(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": stat.st_mtime
        })
    return reports


def format_bytes(bytes_size: int) -> str:
    """Format bytes to human-readable size"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
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
        with open(file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading report {filename}: {e}")
        return None


# ── Report routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Main page - list all available reports"""
    reports = get_report_files()
    return render_template('index.html', reports=reports)


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

    return render_template('report.html',
                          filename=filename,
                          report_type=report_type,
                          report_data=json.dumps(report_data, indent=2))


# ── Operations page ────────────────────────────────────────────────────────────


@app.route("/operations")
def operations():
    """Operations page — trigger backend jobs from the UI"""
    return render_template('operations.html')


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
