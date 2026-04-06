"""
Tests for the Flask frontend (frontend/app.py).

Run with:
    pytest tests/test_frontend.py

Requires the [frontend] extras:
    pip install -e ".[dev,frontend]"
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add frontend/ to path so we can import app without a package structure
_frontend_dir = Path(__file__).parent.parent / "frontend"
if str(_frontend_dir) not in sys.path:
    sys.path.insert(0, str(_frontend_dir))

import app as frontend_app
from app import app, get_report_files, load_report

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend_app, "REPORTS_DIR", tmp_path)
    return tmp_path


def _mock_httpx_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


# ── get_report_files ───────────────────────────────────────────────────────────


class TestGetReportFiles:
    def test_returns_empty_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(frontend_app, "REPORTS_DIR", tmp_path / "nonexistent")
        assert get_report_files() == []

    def test_returns_json_files(self, reports_dir):
        (reports_dir / "deletion-analysis.json").write_text("{}")
        files = get_report_files()
        assert len(files) == 1
        assert files[0]["name"] == "deletion-analysis.json"

    def test_auth_file_not_in_reports_dir(self, reports_dir):
        # .registry-auth.json lives one directory above reports; glob("*.json")
        # should never encounter it here.
        (reports_dir / "deletion-analysis.json").write_text("{}")
        names = [f["name"] for f in get_report_files()]
        assert "deletion-analysis.json" in names
        assert not any(n.startswith(".") for n in names)

    def test_sorted_newest_first(self, reports_dir):
        a = reports_dir / "archived-tags.json"
        a.write_text("{}")
        time.sleep(0.02)
        b = reports_dir / "deletion-analysis.json"
        b.write_text("{}")
        files = get_report_files()
        assert files[0]["name"] == "deletion-analysis.json"
        assert files[1]["name"] == "archived-tags.json"

    def test_metadata_fields_present(self, reports_dir):
        (reports_dir / "deletion-analysis.json").write_text("{}")
        f = get_report_files()[0]
        assert {"name", "size", "modified", "timestamp"} <= f.keys()

    def test_ignores_non_json_files(self, reports_dir):
        (reports_dir / "data.txt").write_text("text")
        (reports_dir / "deletion-analysis.json").write_text("{}")
        files = get_report_files()
        assert all(f["name"].endswith(".json") for f in files)
        assert len(files) == 1

    def test_filters_backend_only_files(self, reports_dir):
        (reports_dir / "final-report.json").write_text("{}")
        (reports_dir / "layers-and-sizes.json").write_text("{}")
        (reports_dir / "mongodb_usage_report.json").write_text("{}")
        (reports_dir / "deletion-analysis.json").write_text("{}")
        files = get_report_files()
        names = [f["name"] for f in files]
        assert names == ["deletion-analysis.json"]


# ── load_report ────────────────────────────────────────────────────────────────


class TestLoadReport:
    def test_loads_valid_json(self, reports_dir):
        data = {"summary": {"total": 5}, "items": []}
        (reports_dir / "report.json").write_text(json.dumps(data))
        assert load_report("report.json") == data

    def test_returns_none_for_missing_file(self, reports_dir):
        assert load_report("nonexistent.json") is None

    def test_returns_none_for_invalid_json(self, reports_dir):
        (reports_dir / "broken.json").write_text("not valid json {{{")
        assert load_report("broken.json") is None

    def test_rejects_path_traversal(self, reports_dir):
        # Filenames containing ../ must not escape REPORTS_DIR
        assert load_report("../etc/passwd") is None


# ── Flask routes ───────────────────────────────────────────────────────────────


class TestRoutes:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "healthy"
        assert "timestamp" in body

    def test_index_returns_200(self, client, reports_dir):
        r = client.get("/")
        assert r.status_code == 200

    def test_operations_page_returns_200(self, client):
        r = client.get("/operations")
        assert r.status_code == 200

    def test_view_report_404_for_missing(self, client, reports_dir):
        r = client.get("/reports/nonexistent.json")
        assert r.status_code == 404

    def test_view_report_200_for_existing(self, client, reports_dir):
        (reports_dir / "unused-environments.json").write_text('{"summary": {}}')
        r = client.get("/reports/unused-environments.json")
        assert r.status_code == 200

    def test_api_reports_list(self, client, reports_dir):
        (reports_dir / "deletion-analysis.json").write_text("{}")
        (reports_dir / "archived-tags.json").write_text("{}")
        r = client.get("/api/reports")
        assert r.status_code == 200
        assert len(r.get_json()) == 2

    def test_api_report_detail_200(self, client, reports_dir):
        data = {"summary": {"total": 3}}
        (reports_dir / "test.json").write_text(json.dumps(data))
        r = client.get("/api/reports/test.json")
        assert r.status_code == 200
        assert r.get_json() == data

    def test_api_report_detail_404(self, client, reports_dir):
        r = client.get("/api/reports/nonexistent.json")
        assert r.status_code == 404


# ── Backend proxy routes ───────────────────────────────────────────────────────


class TestBackendProxy:
    def test_operations_proxies_response(self, client, mocker):
        ops = {"health_check": {"destructive": False, "params": []}}
        mocker.patch("app.httpx.get", return_value=_mock_httpx_response(ops))
        r = client.get("/api/operations")
        assert r.status_code == 200
        assert "health_check" in r.get_json()

    def test_operations_forwards_api_key(self, client, mocker):
        mock_get = mocker.patch("app.httpx.get", return_value=_mock_httpx_response({}))
        monkeypatch_obj = pytest.MonkeyPatch()
        monkeypatch_obj.setattr(frontend_app, "BACKEND_API_KEY", "test-secret")
        client.get("/api/operations")
        monkeypatch_obj.undo()
        headers = mock_get.call_args.kwargs.get("headers", {})
        assert headers.get("X-API-Key") == "test-secret"

    def test_operations_returns_503_when_backend_down(self, client, mocker):
        import httpx as _httpx

        mocker.patch("app.httpx.get", side_effect=_httpx.ConnectError("refused"))
        r = client.get("/api/operations")
        assert r.status_code == 503
        assert "error" in r.get_json()

    def test_jobs_list(self, client, mocker):
        mocker.patch("app.httpx.get", return_value=_mock_httpx_response([]))
        r = client.get("/api/jobs")
        assert r.status_code == 200

    def test_jobs_create(self, client, mocker):
        mocker.patch("app.httpx.post", return_value=_mock_httpx_response({"job_id": "abc123"}))
        r = client.post("/api/jobs", json={"operation": "health_check", "params": {}})
        assert r.status_code == 200
        assert r.get_json()["job_id"] == "abc123"

    def test_jobs_create_503_when_backend_down(self, client, mocker):
        import httpx as _httpx

        mocker.patch("app.httpx.post", side_effect=_httpx.ConnectError("refused"))
        r = client.post("/api/jobs", json={"operation": "health_check", "params": {}})
        assert r.status_code == 503

    def test_get_job_by_id(self, client, mocker):
        job = {"job_id": "abc", "status": "completed", "logs": ["done"]}
        mocker.patch("app.httpx.get", return_value=_mock_httpx_response(job))
        r = client.get("/api/jobs/abc")
        assert r.status_code == 200
        assert r.get_json()["status"] == "completed"

    def test_cancel_job(self, client, mocker):
        mocker.patch("app.httpx.delete", return_value=_mock_httpx_response({"cancelled": True}))
        r = client.delete("/api/jobs/abc")
        assert r.status_code == 200

    def test_cancel_job_503_when_backend_down(self, client, mocker):
        import httpx as _httpx

        mocker.patch("app.httpx.delete", side_effect=_httpx.ConnectError("refused"))
        r = client.delete("/api/jobs/abc")
        assert r.status_code == 503
