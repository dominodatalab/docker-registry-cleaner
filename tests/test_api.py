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


# ── CronJob helpers ────────────────────────────────────────────────────────────


def _make_mock_cj(name, schedule, last_schedule=None, last_successful=None, active=0, suspend=False):
    cj = MagicMock()
    cj.metadata.name = name
    cj.spec.schedule = schedule
    cj.spec.suspend = suspend
    cj.status.active = [MagicMock()] * active
    cj.status.last_schedule_time = datetime.fromisoformat(last_schedule) if last_schedule else None
    cj.status.last_successful_time = datetime.fromisoformat(last_successful) if last_successful else None
    return cj


def _make_mock_job(name, start=None, end=None, complete=False, failed=False):
    job = MagicMock()
    job.metadata.name = name
    job.status.start_time = datetime.fromisoformat(start) if start else None
    job.status.completion_time = datetime.fromisoformat(end) if end else None
    conditions = []
    if complete:
        c = MagicMock()
        c.type = "Complete"
        c.status = "True"
        conditions.append(c)
    if failed:
        c = MagicMock()
        c.type = "Failed"
        c.status = "True"
        conditions.append(c)
    job.status.conditions = conditions or None
    return job


# ── GET /api/cronjobs ──────────────────────────────────────────────────────────


class TestListCronjobs:
    @pytest.fixture
    def mock_batch(self, mocker):
        batch = MagicMock()
        mocker.patch("api._get_k8s_batch_client", return_value=batch)
        return batch

    def test_returns_empty_list_on_k8s_error(self, client, mocker):
        mocker.patch("api._get_k8s_batch_client", side_effect=Exception("k8s unavailable"))
        resp = client.get("/api/cronjobs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_cronjob_fields(self, client, mock_batch):
        cj = _make_mock_cj(
            "docker-registry-cleaner-reports",
            "0 2 * * *",
            last_schedule="2024-01-15T02:00:00+00:00",
            last_successful="2024-01-15T02:05:00+00:00",
        )
        mock_batch.list_namespaced_cron_job.return_value.items = [cj]
        data = client.get("/api/cronjobs").json()
        assert len(data) == 1
        assert data[0]["name"] == "docker-registry-cleaner-reports"
        assert data[0]["schedule"] == "0 2 * * *"
        assert data[0]["active"] == 0
        assert data[0]["last_schedule_time"] is not None
        assert data[0]["last_successful_time"] is not None

    def test_null_timestamps_returned_as_none(self, client, mock_batch):
        mock_batch.list_namespaced_cron_job.return_value.items = [
            _make_mock_cj("docker-registry-cleaner-reports", "0 2 * * *")
        ]
        data = client.get("/api/cronjobs").json()
        assert data[0]["last_schedule_time"] is None
        assert data[0]["last_successful_time"] is None

    def test_active_count_reflects_running_jobs(self, client, mock_batch):
        mock_batch.list_namespaced_cron_job.return_value.items = [
            _make_mock_cj("docker-registry-cleaner-reports", "0 2 * * *", active=2)
        ]
        assert client.get("/api/cronjobs").json()[0]["active"] == 2

    def test_suspend_flag_forwarded(self, client, mock_batch):
        mock_batch.list_namespaced_cron_job.return_value.items = [
            _make_mock_cj("docker-registry-cleaner-reports", "0 2 * * *", suspend=True)
        ]
        assert client.get("/api/cronjobs").json()[0]["suspend"] is True

    def test_results_sorted_by_name(self, client, mock_batch):
        mock_batch.list_namespaced_cron_job.return_value.items = [
            _make_mock_cj("docker-registry-cleaner-z", "0 3 * * *"),
            _make_mock_cj("docker-registry-cleaner-a", "0 2 * * *"),
        ]
        names = [d["name"] for d in client.get("/api/cronjobs").json()]
        assert names == ["docker-registry-cleaner-a", "docker-registry-cleaner-z"]

    def test_uses_correct_label_selector(self, client, mock_batch):
        mock_batch.list_namespaced_cron_job.return_value.items = []
        client.get("/api/cronjobs")
        call_kwargs = mock_batch.list_namespaced_cron_job.call_args
        assert "app.kubernetes.io/name=docker-registry-cleaner" in str(call_kwargs)


# ── GET /api/cronjobs/{name}/runs ──────────────────────────────────────────────


class TestListCronjobRuns:
    @pytest.fixture
    def mock_batch(self, mocker):
        batch = MagicMock()
        mocker.patch("api._get_k8s_batch_client", return_value=batch)
        return batch

    def test_returns_empty_list_on_k8s_error(self, client, mocker):
        mocker.patch("api._get_k8s_batch_client", side_effect=Exception("k8s unavailable"))
        resp = client.get("/api/cronjobs/some-cj/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_succeeded_job_status(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job("run-abc", start="2024-01-15T02:00:00+00:00", complete=True)
        ]
        data = client.get("/api/cronjobs/my-cj/runs").json()
        assert data[0]["status"] == "succeeded"

    def test_failed_job_status(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job("run-abc", start="2024-01-15T02:00:00+00:00", failed=True)
        ]
        assert client.get("/api/cronjobs/my-cj/runs").json()[0]["status"] == "failed"

    def test_running_job_has_no_condition(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job("run-abc", start="2024-01-15T02:00:00+00:00")
        ]
        assert client.get("/api/cronjobs/my-cj/runs").json()[0]["status"] == "running"

    def test_duration_calculated_correctly(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job(
                "run-abc",
                start="2024-01-15T02:00:00+00:00",
                end="2024-01-15T02:05:30+00:00",
                complete=True,
            )
        ]
        assert client.get("/api/cronjobs/my-cj/runs").json()[0]["duration_seconds"] == 330

    def test_null_start_gives_null_duration(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [_make_mock_job("run-abc")]
        assert client.get("/api/cronjobs/my-cj/runs").json()[0]["duration_seconds"] is None

    def test_sorted_newest_first(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job("run-old", start="2024-01-14T02:00:00+00:00", complete=True),
            _make_mock_job("run-new", start="2024-01-15T02:00:00+00:00", complete=True),
        ]
        names = [r["name"] for r in client.get("/api/cronjobs/my-cj/runs").json()]
        assert names == ["run-new", "run-old"]

    def test_limited_to_20_results(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = [
            _make_mock_job(f"run-{i}", start=f"2024-01-{i+1:02d}T00:00:00+00:00", complete=True) for i in range(25)
        ]
        assert len(client.get("/api/cronjobs/my-cj/runs").json()) == 20

    def test_label_selector_includes_cronjob_name(self, client, mock_batch):
        mock_batch.list_namespaced_job.return_value.items = []
        client.get("/api/cronjobs/my-specific-cj/runs")
        call_kwargs = mock_batch.list_namespaced_job.call_args
        assert "registry-cleaner-cronjob=my-specific-cj" in str(call_kwargs)


# ── Job persistence ────────────────────────────────────────────────────────────


class TestJobPersistence:
    @pytest.fixture(autouse=True)
    def jobs_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "jobs"
        d.mkdir()
        monkeypatch.setattr(api_module, "JOBS_DIR", d)
        return d

    def _submit(self, client):
        with patch("api.subprocess.Popen", return_value=_mock_popen()):
            r = client.post("/api/jobs", json={"operation": "health_check"})
        return r.json()["job_id"]

    def test_job_file_created_on_submission(self, client, jobs_dir):
        job_id = self._submit(client)
        assert (jobs_dir / f"{job_id}.json").exists()

    def test_persisted_file_has_no_input_tmp_path_key(self, client, jobs_dir):
        job_id = self._submit(client)
        import json as _json

        data = _json.loads((jobs_dir / f"{job_id}.json").read_text())
        assert "input_tmp_path" not in data

    def test_final_state_written_after_completion(self, client, jobs_dir):
        import json as _json

        job_id = self._submit(client)
        _wait_for_terminal(job_id)
        data = _json.loads((jobs_dir / f"{job_id}.json").read_text())
        assert data["status"] == "completed"
        assert data["finished_at"] is not None

    def test_failed_job_persisted_as_failed(self, client, jobs_dir):
        import json as _json

        with patch("api.subprocess.Popen", return_value=_mock_popen(returncode=1)):
            job_id = client.post("/api/jobs", json={"operation": "health_check"}).json()["job_id"]
        _wait_for_terminal(job_id)
        data = _json.loads((jobs_dir / f"{job_id}.json").read_text())
        assert data["status"] == "failed"

    def test_cancelled_job_persisted_as_cancelled(self, client, jobs_dir):
        import json as _json

        job_id = self._submit(client)
        _wait_for_terminal(job_id)  # let it finish first so pid is gone
        with _jobs_lock:
            _jobs[job_id]["status"] = "pending"  # force back to cancellable state
        client.delete(f"/api/jobs/{job_id}")
        data = _json.loads((jobs_dir / f"{job_id}.json").read_text())
        assert data["status"] == "cancelled"

    def test_load_restores_completed_jobs(self, client, jobs_dir):
        job_id = self._submit(client)
        _wait_for_terminal(job_id)

        # Simulate restart: clear memory, reload from disk
        with _jobs_lock:
            _jobs.clear()
        api_module._load_persisted_jobs()

        with _jobs_lock:
            assert job_id in _jobs
            assert _jobs[job_id]["status"] == "completed"

    def test_load_marks_dangling_running_jobs_as_failed(self, jobs_dir):
        import json as _json

        job_id = str(uuid.uuid4())
        record = _make_job_record(job_id, status="running")
        record["finished_at"] = None
        (jobs_dir / f"{job_id}.json").write_text(_json.dumps(record))

        with _jobs_lock:
            _jobs.clear()
        api_module._load_persisted_jobs()

        with _jobs_lock:
            job = _jobs[job_id]
        assert job["status"] == "failed"
        assert job["finished_at"] is not None
        assert any("restart" in line for line in job["logs"])

    def test_load_marks_dangling_pending_jobs_as_failed(self, jobs_dir):
        import json as _json

        job_id = str(uuid.uuid4())
        record = _make_job_record(job_id, status="pending")
        (jobs_dir / f"{job_id}.json").write_text(_json.dumps(record))

        with _jobs_lock:
            _jobs.clear()
        api_module._load_persisted_jobs()

        with _jobs_lock:
            assert _jobs[job_id]["status"] == "failed"

    def test_trimmed_job_file_is_deleted(self, client, jobs_dir):
        original = api_module.MAX_JOBS
        try:
            api_module.MAX_JOBS = 2
            ids = [self._submit(client) for _ in range(4)]
            assert not (jobs_dir / f"{ids[0]}.json").exists()
            assert not (jobs_dir / f"{ids[1]}.json").exists()
            assert (jobs_dir / f"{ids[2]}.json").exists()
            assert (jobs_dir / f"{ids[3]}.json").exists()
        finally:
            api_module.MAX_JOBS = original

    def test_load_skips_corrupt_file(self, jobs_dir):
        (jobs_dir / "bad.json").write_text("not valid json {{{")
        with _jobs_lock:
            _jobs.clear()
        api_module._load_persisted_jobs()  # must not raise
        with _jobs_lock:
            assert len(_jobs) == 0
