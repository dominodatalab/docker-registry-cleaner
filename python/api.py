"""
FastAPI backend API server for Docker Registry Cleaner.

Runs on 0.0.0.0:8081 inside the pod. No Kubernetes Service exposes this port,
so it is only reachable from within the pod (i.e. by the frontend sidecar via
localhost:8081).

Authentication: every request (except GET /health) must include an
  X-API-Key: <value>
header matching the BACKEND_API_KEY environment variable. If the variable is
unset, auth is skipped (useful for local development).

Jobs are tracked in memory; the last MAX_JOBS entries are kept. Restarting the
container clears the history — that is intentional for a single-replica
StatefulSet.
"""

import os
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

_API_KEY_HEADER: Optional[str] = Header(default=None)

# ── Configuration ─────────────────────────────────────────────────────────────

BACKEND_API_KEY: str = os.environ.get("BACKEND_API_KEY", "")
_MAIN_PY: Path = Path(__file__).parent / "main.py"
MAX_JOBS: int = 50

# ── Operation catalogue ────────────────────────────────────────────────────────
# Each entry declares:
#   description  – shown in the UI
#   destructive  – True if the op can delete/mutate data (UI shows a warning)
#   params       – list of param specs; see _build_args() for how they are used

OPERATIONS: Dict[str, Dict[str, Any]] = {
    "health_check": {
        "description": "Run health checks and verify system connectivity (registry, MongoDB, Kubernetes, S3)",
        "destructive": False,
        "params": [],
    },
    "reports": {
        "description": "Generate tag usage reports from analysis data",
        "destructive": False,
        "params": [
            {
                "name": "generate_reports",
                "flag": "--generate-reports",
                "type": "bool",
                "default": False,
                "help": "Force regeneration of metadata reports",
            },
        ],
    },
    "image_size_report": {
        "description": "Generate a report of the largest images sorted by total size",
        "destructive": False,
        "params": [
            {
                "name": "generate_reports",
                "flag": "--generate-reports",
                "type": "bool",
                "default": False,
                "help": "Force regeneration of image analysis",
            },
        ],
    },
    "user_size_report": {
        "description": "Generate a report of image sizes grouped by user/owner",
        "destructive": False,
        "params": [
            {
                "name": "generate_reports",
                "flag": "--generate-reports",
                "type": "bool",
                "default": False,
                "help": "Force regeneration of reports",
            },
        ],
    },
    "find_environment_usage": {
        "description": "Find where a specific environment ID is used across projects, jobs, workspaces, and runs",
        "destructive": False,
        "params": [
            {
                "name": "environment_id",
                "flag": "--environment-id",
                "type": "str",
                "required": True,
                "help": "Environment ObjectId (24-character hex string)",
            },
        ],
    },
    "delete_archived_tags": {
        "description": "Find (or delete) Docker tags associated with archived environments and/or models",
        "destructive": True,
        "params": [
            {
                "name": "environment",
                "flag": "--environment",
                "type": "bool",
                "default": False,
                "help": "Process archived environments",
            },
            {
                "name": "model",
                "flag": "--model",
                "type": "bool",
                "default": False,
                "help": "Process archived models",
            },
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete tags (default is dry-run — no deletions occur without this)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider tags unused if not used in the last N days",
            },
            {
                "name": "s3_bucket",
                "flag": "--s3-bucket",
                "type": "str",
                "default": None,
                "help": "S3 bucket to back up images into before deletion",
            },
        ],
    },
    "archive_unused_environments": {
        "description": "Mark unused environments as archived in MongoDB",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually archive environments (default is dry-run)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider environments unused if not used in the last N days",
            },
            {
                "name": "generate_reports",
                "flag": "--generate-reports",
                "type": "bool",
                "default": False,
                "help": "Force regeneration of metadata reports before analysis",
            },
        ],
    },
    "delete_unused_environments": {
        "description": "Find (or delete) environments not used in workspaces, models, or project defaults",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete tags (default is dry-run)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider environments unused if not used in the last N days",
            },
            {
                "name": "generate_reports",
                "flag": "--generate-reports",
                "type": "bool",
                "default": False,
                "help": "Force regeneration of metadata reports before analysis",
            },
            {
                "name": "s3_bucket",
                "flag": "--s3-bucket",
                "type": "str",
                "default": None,
                "help": "S3 bucket to back up images into before deletion",
            },
            {
                "name": "mongo_cleanup",
                "flag": "--mongo-cleanup",
                "type": "bool",
                "default": False,
                "help": "Also clean up MongoDB records after Docker deletion",
            },
            {
                "name": "run_registry_gc",
                "flag": "--run-registry-gc",
                "type": "bool",
                "default": False,
                "help": "Run Docker registry garbage collection after deletion",
            },
        ],
    },
    "delete_unused_private_environments": {
        "description": "Find (or delete) private environments owned by deactivated Keycloak users",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete environments (default is dry-run)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider environments unused if not used in the last N days",
            },
            {
                "name": "s3_bucket",
                "flag": "--s3-bucket",
                "type": "str",
                "default": None,
                "help": "S3 bucket to back up images into before deletion",
            },
            {
                "name": "mongo_cleanup",
                "flag": "--mongo-cleanup",
                "type": "bool",
                "default": False,
                "help": "Also clean up MongoDB records after Docker deletion",
            },
        ],
    },
    "delete_all_unused_environments": {
        "description": "Comprehensive cleanup: unused environments + private environments of deactivated users",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete environments (default is dry-run)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider environments unused if not used in the last N days",
            },
            {
                "name": "s3_bucket",
                "flag": "--s3-bucket",
                "type": "str",
                "default": None,
                "help": "S3 bucket to back up images into before deletion",
            },
        ],
    },
    "delete_unused_references": {
        "description": "Find (or delete) MongoDB references to non-existent Docker images",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete records (default is dry-run)",
            },
        ],
    },
    "delete_image": {
        "description": "Analyze unused Docker images — or delete a specific image by ID",
        "destructive": True,
        "params": [
            {
                "name": "apply",
                "flag": "--apply",
                "type": "bool",
                "default": False,
                "help": "Actually delete images (default is dry-run)",
            },
            {
                "name": "unused_since_days",
                "flag": "--unused-since-days",
                "type": "int",
                "default": None,
                "help": "Only consider images unused if not used in the last N days",
            },
            {
                "name": "s3_bucket",
                "flag": "--s3-bucket",
                "type": "str",
                "default": None,
                "help": "S3 bucket to back up images into before deletion",
            },
            {
                "name": "mongo_cleanup",
                "flag": "--mongo-cleanup",
                "type": "bool",
                "default": False,
                "help": "Also clean up MongoDB records after Docker deletion",
            },
            {
                "name": "run_registry_gc",
                "flag": "--run-registry-gc",
                "type": "bool",
                "default": False,
                "help": "Run Docker registry garbage collection after deletion",
            },
        ],
    },
    "run_registry_gc": {
        "description": "Run Docker registry garbage collection inside the registry pod",
        "destructive": True,
        "params": [],
    },
}

# ── In-memory job store ────────────────────────────────────────────────────────

_jobs: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_jobs_lock = threading.Lock()


def _trim_jobs() -> None:
    """Drop oldest jobs when the store exceeds MAX_JOBS. Must be called with _jobs_lock held."""
    while len(_jobs) > MAX_JOBS:
        _jobs.popitem(last=False)


# ── Argument builder ───────────────────────────────────────────────────────────


def _build_args(operation: str, params: Dict[str, Any]) -> List[str]:
    """Translate an HTTP params dict into a CLI args list for main.py."""
    op_def = OPERATIONS[operation]
    args: List[str] = [operation]

    for spec in op_def["params"]:
        name = spec["name"]
        flag = spec["flag"]
        param_type = spec["type"]
        value = params.get(name, spec.get("default"))

        if param_type == "bool":
            if value:
                args.append(flag)
        elif param_type in ("int", "str"):
            if value is not None and str(value).strip() != "":
                args.extend([flag, str(value)])

    return args


# ── Background job runner ──────────────────────────────────────────────────────


def _run_job(job_id: str, cli_args: List[str]) -> None:
    """Execute a job in a background thread, streaming output into the job store."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return

    try:
        process = subprocess.Popen(
            [sys.executable, str(_MAIN_PY)] + cli_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with _jobs_lock:
            _jobs[job_id]["pid"] = process.pid
            _jobs[job_id]["status"] = "running"

        for line in process.stdout:  # type: ignore[union-attr]
            with _jobs_lock:
                _jobs[job_id]["logs"].append(line.rstrip("\n"))

        process.wait()
        return_code = process.returncode

        with _jobs_lock:
            _jobs[job_id]["returncode"] = return_code
            _jobs[job_id]["status"] = "completed" if return_code == 0 else "failed"
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["logs"].append(f"[api] Error launching job: {exc}")
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Docker Registry Cleaner API",
    version="1.0.0",
    description="Internal API — only accessible from within the pod via localhost:8081.",
    docs_url="/docs",
)


# ── Auth dependency ────────────────────────────────────────────────────────────


def _check_api_key(x_api_key: Optional[str] = _API_KEY_HEADER) -> None:
    if not BACKEND_API_KEY:
        return  # Auth disabled (BACKEND_API_KEY not configured)
    if x_api_key != BACKEND_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-API-Key header",
        )


# ── Request / Response models ──────────────────────────────────────────────────


class JobRequest(BaseModel):
    operation: str
    params: Dict[str, Any] = {}


class JobSummary(BaseModel):
    job_id: str
    operation: str
    status: str
    started_at: str
    finished_at: Optional[str]
    returncode: Optional[int]


class JobDetail(JobSummary):
    logs: List[str]
    pid: Optional[int]


# ── Routes ─────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> Dict[str, str]:
    """Health check — no auth required."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/operations", dependencies=[Depends(_check_api_key)])
def list_operations() -> Dict[str, Any]:
    """Return all available operations with their param schemas."""
    return {
        name: {
            "description": op["description"],
            "destructive": op["destructive"],
            "params": op["params"],
        }
        for name, op in OPERATIONS.items()
    }


@app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_check_api_key)])
def create_job(req: JobRequest) -> Dict[str, str]:
    """Start a new job and return its ID. The job runs asynchronously."""
    if req.operation not in OPERATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown operation '{req.operation}'. Valid operations: {list(OPERATIONS.keys())}",
        )

    # Validate required params
    for spec in OPERATIONS[req.operation]["params"]:
        if spec.get("required") and req.params.get(spec["name"]) in (None, ""):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required parameter '{spec['name']}'",
            )

    cli_args = _build_args(req.operation, req.params)
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "operation": req.operation,
            "params": req.params,
            "cli_args": cli_args,
            "status": "pending",
            "started_at": now,
            "finished_at": None,
            "returncode": None,
            "pid": None,
            "logs": [],
        }
        _trim_jobs()

    thread = threading.Thread(target=_run_job, args=(job_id, cli_args), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/api/jobs", dependencies=[Depends(_check_api_key)])
def list_jobs() -> List[JobSummary]:
    """Return summaries of all tracked jobs, newest first."""
    with _jobs_lock:
        jobs_snapshot = list(_jobs.values())

    return [
        JobSummary(
            job_id=j["job_id"],
            operation=j["operation"],
            status=j["status"],
            started_at=j["started_at"],
            finished_at=j["finished_at"],
            returncode=j["returncode"],
        )
        for j in reversed(jobs_snapshot)
    ]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(_check_api_key)])
def get_job(job_id: str) -> JobDetail:
    """Return the full detail (including logs) for a single job."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return JobDetail(
        job_id=job["job_id"],
        operation=job["operation"],
        status=job["status"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        returncode=job["returncode"],
        pid=job["pid"],
        logs=list(job["logs"]),  # snapshot to avoid race
    )


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(_check_api_key)])
def cancel_job(job_id: str) -> Dict[str, str]:
    """Attempt to cancel a running job by terminating its subprocess."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job["status"] not in ("pending", "running"):
        return {"message": f"Job is already {job['status']} — nothing to cancel"}

    pid = job.get("pid")
    if pid:
        try:
            import signal

            os.kill(pid, signal.SIGTERM)
            with _jobs_lock:
                _jobs[job_id]["status"] = "cancelled"
                _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
                _jobs[job_id]["logs"].append("[api] Job cancelled by user")
            return {"message": "Job cancelled"}
        except ProcessLookupError:
            pass  # Process already finished

    with _jobs_lock:
        _jobs[job_id]["status"] = "cancelled"
        _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

    return {"message": "Job marked as cancelled"}
