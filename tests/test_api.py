"""
Tests for python/api.py — FastAPI backend.

Run with:
    pytest tests/test_api.py
"""

import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Must be set before api.py is imported so config_manager doesn't blow up outside
# the pod environment.
os.environ.setdefault("SKIP_CONFIG_VALIDATION", "true")

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

import api as api_module
from api import _jobs, _jobs_lock, _write_validated_input, app

# ── Helpers ────────────────────────────────────────────────────────────────────


def _mock_popen(lines=("output line\n",), returncode=0):
    """Return a Popen mock that completes immediately with given output."""
    proc = MagicMock()
    proc.pid = 42
    proc.returncode = returncode
    proc.stdout = iter(lines)
    proc.wait.return_value = None
    return proc


def _make_job_record(job_id, status="pending"):
    return {
        "job_id": job_id,
        "operation": "health_check",
        "params": {},
        "cli_args": ["health_check"],
        "status": status,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "returncode": None,
        "pid": None,
        "logs": [],
        "input_tmp_path": None,
    }


def _wait_for_terminal(job_id, timeout=5.0):
    """Poll _jobs until the job reaches a terminal status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job and job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    return None


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_jobs():
    """Reset the job store before and after every test."""
    with _jobs_lock:
        _jobs.clear()
    yield
    with _jobs_lock:
        _jobs.clear()


@pytest.fixture
def client():
    """TestClient with auth disabled (BACKEND_API_KEY unset)."""
    with patch.object(api_module, "BACKEND_API_KEY", ""):
        yield TestClient(app)


@pytest.fixture
def authed_client():
    """TestClient with BACKEND_API_KEY='secret' enforced."""
    with patch.object(api_module, "BACKEND_API_KEY", "secret"):
        yield TestClient(app)


# ── GET /health ────────────────────────────────────────────────────────────────


class TestHealth:
    def test_returns_healthy(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_no_auth_required_even_when_key_configured(self, authed_client):
        resp = authed_client.get("/health")
        assert resp.status_code == 200


# ── Auth enforcement ───────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_key_returns_403(self, authed_client):
        resp = authed_client.get("/api/operations")
        assert resp.status_code == 403

    def test_wrong_key_returns_403(self, authed_client):
        resp = authed_client.get("/api/operations", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 403

    def test_correct_key_passes(self, authed_client):
        resp = authed_client.get("/api/operations", headers={"X-API-Key": "secret"})
        assert resp.status_code == 200

    def test_no_key_configured_skips_auth(self, client):
        """When BACKEND_API_KEY is empty every request is accepted without a key."""
        resp = client.get("/api/operations")
        assert resp.status_code == 200


# ── GET /metrics ───────────────────────────────────────────────────────────────


class TestMetrics:
    def test_returns_200(self, client):
        assert client.get("/metrics").status_code == 200

    def test_prometheus_content_type(self, client):
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_contains_expected_metric_names(self, client):
        body = client.get("/metrics").text
        assert "registry_cleaner_tags_pending_deletion" in body
        assert "registry_cleaner_space_recoverable_bytes" in body
        assert "registry_cleaner_jobs_total" in body

    def test_no_auth_required(self, authed_client):
        assert authed_client.get("/metrics").status_code == 200


# ── GET /api/operations ────────────────────────────────────────────────────────


class TestOperations:
    def test_returns_known_operations(self, client):
        data = client.get("/api/operations").json()
        for op in ("health_check", "delete_archived_tags", "delete_unused_environments"):
            assert op in data, f"expected '{op}' in operations"

    def test_each_operation_has_required_fields(self, client):
        for name, op in client.get("/api/operations").json().items():
            assert "description" in op, f"{name}: missing description"
            assert "destructive" in op, f"{name}: missing destructive"
            assert "params" in op, f"{name}: missing params"

    def test_run_registry_gc_hidden_out_of_cluster(self, client):
        with patch.object(api_module, "_REGISTRY_IN_CLUSTER", False):
            data = client.get("/api/operations").json()
        assert "run_registry_gc" not in data

    def test_run_registry_gc_visible_in_cluster(self, client):
        with patch.object(api_module, "_REGISTRY_IN_CLUSTER", True):
            data = client.get("/api/operations").json()
        assert "run_registry_gc" in data


# ── GET /api/org-scope ─────────────────────────────────────────────────────────


class TestOrgScope:
    def test_no_org_ids_returns_empty_scope_without_querying_mongo(self, client, mocker):
        mock_resolve = mocker.patch("api.resolve_org_scope")
        resp = client.get("/api/org-scope")
        assert resp.status_code == 200
        assert resp.json() == {"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}}
        mock_resolve.assert_not_called()

    def test_returns_resolved_scope(self, client, mocker):
        mocker.patch(
            "api.resolve_org_scope",
            return_value={"org_ids": ["org1"], "project_ids": ["p1"], "tags": ["t1"], "other_owners": {}},
        )
        resp = client.get("/api/org-scope", params={"org_id": ["org1"]})
        assert resp.status_code == 200
        assert resp.json()["tags"] == ["t1"]

    def test_passes_through_multiple_org_ids(self, client, mocker):
        mock_resolve = mocker.patch(
            "api.resolve_org_scope",
            return_value={"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}},
        )
        client.get("/api/org-scope", params={"org_id": ["org1", "org2"]})
        mock_resolve.assert_called_once_with(["org1", "org2"])

    def test_mongo_error_returns_503(self, client, mocker):
        from pymongo.errors import PyMongoError

        mocker.patch("api.resolve_org_scope", side_effect=PyMongoError("connection refused"))
        resp = client.get("/api/org-scope", params={"org_id": ["org1"]})
        assert resp.status_code == 503

    def test_requires_api_key_when_configured(self, authed_client):
        resp = authed_client.get("/api/org-scope", params={"org_id": ["org1"]})
        assert resp.status_code == 403

    def test_correct_key_passes(self, authed_client, mocker):
        mocker.patch(
            "api.resolve_org_scope",
            return_value={"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}},
        )
        resp = authed_client.get("/api/org-scope", params={"org_id": ["org1"]}, headers={"X-API-Key": "secret"})
        assert resp.status_code == 200


# ── _write_validated_input ─────────────────────────────────────────────────────


class TestWriteValidatedInput:
    def test_valid_lines_written_to_file(self):
        raw = "environment:507f1f77bcf86cd799439011\nmodel:507f1f77bcf86cd799439012"
        path = _write_validated_input(raw)
        try:
            content = Path(path).read_text()
            assert "environment:507f1f77bcf86cd799439011" in content
            assert "model:507f1f77bcf86cd799439012" in content
        finally:
            Path(path).unlink(missing_ok=True)

    def test_blank_and_comment_lines_are_skipped(self):
        raw = "\n# comment\nenvironment:507f1f77bcf86cd799439011\n\n"
        path = _write_validated_input(raw)
        try:
            assert Path(path).read_text().splitlines() == ["environment:507f1f77bcf86cd799439011"]
        finally:
            Path(path).unlink(missing_ok=True)

    def test_invalid_line_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid"):
            _write_validated_input("not-a-valid-id")

    def test_bare_object_id_missing_type_prefix_rejected(self):
        with pytest.raises(ValueError):
            _write_validated_input("507f1f77bcf86cd799439011")

    def test_too_short_hex_rejected(self):
        with pytest.raises(ValueError):
            _write_validated_input("environment:abc123")


# ── POST /api/jobs ─────────────────────────────────────────────────────────────


class TestCreateJob:
    def test_unknown_operation_returns_400(self, client):
        resp = client.post("/api/jobs", json={"operation": "nonexistent_op"})
        assert resp.status_code == 400

    def test_missing_required_param_returns_400(self, client):
        resp = client.post("/api/jobs", json={"operation": "find_environment_usage", "params": {}})
        assert resp.status_code == 400
        assert "environment_id" in resp.json()["detail"]

    def test_valid_job_accepted_with_202_and_job_id(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            resp = client.post("/api/jobs", json={"operation": "health_check"})
        assert resp.status_code == 202
        assert "job_id" in resp.json()

    def test_job_appears_in_store_immediately(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        with _jobs_lock:
            assert job_id in _jobs

    def test_invalid_id_list_payload_returns_400(self, client):
        resp = client.post(
            "/api/jobs",
            json={"operation": "delete_image", "params": {"input_ids": "not-an-objectid"}},
        )
        assert resp.status_code == 400


# ── GET /api/jobs and GET /api/jobs/{job_id} ───────────────────────────────────


class TestListAndGetJobs:
    def test_list_empty_initially(self, client):
        assert client.get("/api/jobs").json() == []

    def test_submitted_job_appears_in_list(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        ids = [j["job_id"] for j in client.get("/api/jobs").json()]
        assert job_id in ids

    def test_list_entries_have_required_fields(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            client.post("/api/jobs", json={"operation": "health_check"})
        for field in ("job_id", "operation", "status", "started_at"):
            assert field in client.get("/api/jobs").json()[0]

    def test_get_unknown_job_returns_404(self, client):
        assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404

    def test_get_job_returns_detail_with_logs(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen(lines=["hello\n"])):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        _wait_for_terminal(job_id)
        data = client.get(f"/api/jobs/{job_id}").json()
        assert data["job_id"] == job_id
        assert "logs" in data

    def test_completed_job_has_zero_returncode_and_completed_status(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen(returncode=0)):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        _wait_for_terminal(job_id)
        data = client.get(f"/api/jobs/{job_id}").json()
        assert data["status"] == "completed"
        assert data["returncode"] == 0

    def test_nonzero_returncode_gives_failed_status(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen(returncode=1)):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        _wait_for_terminal(job_id)
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"


# ── DELETE /api/jobs/{job_id} ──────────────────────────────────────────────────


class TestCancelJob:
    def test_unknown_id_returns_404(self, client):
        assert client.delete(f"/api/jobs/{uuid.uuid4()}").status_code == 404

    def test_cancelling_completed_job_returns_already_message(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        _wait_for_terminal(job_id)
        resp = client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert "already" in resp.json()["message"]

    def test_cancelling_pending_job_sets_cancelled_status(self, client):
        job_id = str(uuid.uuid4())
        with _jobs_lock:
            _jobs[job_id] = _make_job_record(job_id, status="pending")
        resp = client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        with _jobs_lock:
            assert _jobs[job_id]["status"] == "cancelled"


# ── _run_job (direct thread tests with mocked Popen) ──────────────────────────


class TestRunJob:
    def _seed_job(self, job_id):
        with _jobs_lock:
            _jobs[job_id] = _make_job_record(job_id)

    def _run(self, job_id, cli_args=("health_check",)):
        t = threading.Thread(target=api_module._run_job, args=(job_id, list(cli_args)))
        t.start()
        t.join(timeout=5)

    def test_successful_subprocess_sets_completed(self):
        job_id = str(uuid.uuid4())
        self._seed_job(job_id)
        with patch("api.subprocess.Popen", return_value=_mock_popen(lines=["done\n"], returncode=0)):
            self._run(job_id)
        with _jobs_lock:
            job = _jobs[job_id]
        assert job["status"] == "completed"
        assert "done" in job["logs"]

    def test_nonzero_returncode_sets_failed(self):
        job_id = str(uuid.uuid4())
        self._seed_job(job_id)
        with patch("api.subprocess.Popen", return_value=_mock_popen(returncode=1)):
            self._run(job_id)
        with _jobs_lock:
            assert _jobs[job_id]["status"] == "failed"

    def test_popen_exception_sets_failed_with_error_in_logs(self):
        job_id = str(uuid.uuid4())
        self._seed_job(job_id)
        with patch("api.subprocess.Popen", side_effect=OSError("no such file")):
            self._run(job_id)
        with _jobs_lock:
            job = _jobs[job_id]
        assert job["status"] == "failed"
        assert any("no such file" in log for log in job["logs"])

    def test_timeout_kills_process_and_appends_timeout_log(self):
        job_id = str(uuid.uuid4())
        self._seed_job(job_id)

        unblock = threading.Event()

        def blocking_stdout():
            yield "start\n"
            unblock.wait(timeout=10)

        proc = MagicMock()
        proc.pid = 9999
        proc.returncode = -9
        proc.stdout = blocking_stdout()
        proc.kill.side_effect = unblock.set
        proc.wait.return_value = None

        with patch("api.subprocess.Popen", return_value=proc):
            with patch.object(api_module, "JOB_TIMEOUT_SECONDS", 0.1):
                self._run(job_id)

        with _jobs_lock:
            job = _jobs[job_id]
        assert job["status"] == "failed"
        assert any("timeout" in log.lower() for log in job["logs"])


# ── Concurrent job submission ──────────────────────────────────────────────────


class TestConcurrentJobSubmission:
    def test_concurrent_submissions_produce_unique_jobs_without_corruption(self):
        """Simulate 20 threads inserting jobs simultaneously; _jobs_lock must prevent corruption."""
        ids = []
        errors = []
        lock = threading.Lock()

        def add_job():
            try:
                job_id = str(uuid.uuid4())
                with _jobs_lock:
                    _jobs[job_id] = _make_job_record(job_id)
                    api_module._trim_jobs()
                with lock:
                    ids.append(job_id)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=add_job) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent submission: {errors}"
        assert len(set(ids)) == 20, "Expected 20 unique job IDs"
        with _jobs_lock:
            # MAX_JOBS default is 50, so none should be trimmed
            assert len(_jobs) == 20


# ── MAX_JOBS trimming ──────────────────────────────────────────────────────────


class TestJobTrimming:
    def test_store_never_exceeds_max_jobs(self, client):
        original = api_module.MAX_JOBS
        try:
            api_module.MAX_JOBS = 3
            for _ in range(5):
                with patch("api.subprocess.Popen", return_value=_mock_popen()):
                    client.post("/api/jobs", json={"operation": "health_check"})
            with _jobs_lock:
                assert len(_jobs) <= 3
        finally:
            api_module.MAX_JOBS = original

    def test_oldest_jobs_are_evicted_first(self, client):
        original = api_module.MAX_JOBS
        try:
            api_module.MAX_JOBS = 2
            submitted = []
            for _ in range(4):
                with patch("api.subprocess.Popen", return_value=_mock_popen()):
                    job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
                    submitted.append(job_id)
            with _jobs_lock:
                present = set(_jobs.keys())
            # The two oldest should have been evicted; the two newest should remain.
            assert submitted[0] not in present
            assert submitted[1] not in present
            assert submitted[2] in present
            assert submitted[3] in present
        finally:
            api_module.MAX_JOBS = original
