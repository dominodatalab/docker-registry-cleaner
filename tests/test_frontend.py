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
from flask import session

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
        # .registry-auth.json lives in a hidden .auth/ subdirectory of the reports
        # dir; glob("*.json") is non-recursive and skips dot-prefixed names, so it
        # should never surface here.
        (reports_dir / "deletion-analysis.json").write_text("{}")
        auth_dir = reports_dir / ".auth"
        auth_dir.mkdir()
        (auth_dir / ".registry-auth.json").write_text("{}")
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
    # load_report() reads Flask's `session` (to check org-scope access),
    # so it needs an active request context — app.test_request_context()
    # gives it one without spinning up a full test-client request.

    def test_loads_valid_json(self, reports_dir):
        data = {"summary": {"total": 5}, "items": []}
        (reports_dir / "report.json").write_text(json.dumps(data))
        with app.test_request_context():
            assert load_report("report.json") == data

    def test_returns_none_for_missing_file(self, reports_dir):
        with app.test_request_context():
            assert load_report("nonexistent.json") is None

    def test_returns_none_for_invalid_json(self, reports_dir):
        (reports_dir / "broken.json").write_text("not valid json {{{")
        with app.test_request_context():
            assert load_report("broken.json") is None

    def test_rejects_path_traversal(self, reports_dir):
        # Filenames containing ../ must not escape REPORTS_DIR
        with app.test_request_context():
            assert load_report("../etc/passwd") is None


class TestLoadReportOrgScope:
    """load_report()'s org-scope filtering (docs/org-scoped-access-plan.md
    §3.2/§3.3) — admin sessions are unaffected; org-scoped sessions get
    filtered content for known report types and nothing at all for
    backend-only ones."""

    def test_admin_session_gets_unfiltered_data(self, reports_dir):
        data = {"summary": {"total_images": 2}, "images": [{"tag": "a"}, {"tag": "b"}]}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        with app.test_request_context():
            session["access_scope"] = "admin"
            assert load_report("image-size-report.json") == data

    def test_no_access_scope_gets_unfiltered_data(self, reports_dir):
        """Local dev / auth-disabled mode: no access_scope is ever set."""
        data = {"summary": {}, "images": [{"tag": "a"}]}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        with app.test_request_context():
            assert load_report("image-size-report.json") == data

    def test_org_scoped_session_gets_filtered_data(self, reports_dir, mocker):
        # entries mirrors `images` — the real generator (image_size_report.py)
        # builds both from the same list; report_scope.py's filter functions
        # read from `entries` as their source of truth (see
        # docs/report-schema-standardization-plan.md), so a fixture without
        # it would filter down to nothing regardless of what `images` has.
        images = [
            {"tag": "my-tag", "total_size_bytes": 10, "size_bytes": 10},
            {"tag": "not-mine", "total_size_bytes": 20, "size_bytes": 20},
        ]
        data = {
            "summary": {"total_images": 2, "total_size_bytes": 30},
            "images": images,
            "entries": images,
        }
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        mocker.patch(
            "app._get_org_scope",
            return_value={"org_ids": ["org1"], "project_ids": [], "tags": ["my-tag"], "other_owners": {}},
        )
        with app.test_request_context():
            session["access_scope"] = "org_scoped"
            session["org_ids"] = ["org1"]
            result = load_report("image-size-report.json")
        assert [i["tag"] for i in result["images"]] == ["my-tag"]

    def test_org_scoped_session_cannot_load_backend_only_report(self, reports_dir):
        (reports_dir / "final-report.json").write_text(json.dumps({"sha256:abc": {"tags": ["x"]}}))
        with app.test_request_context():
            session["access_scope"] = "org_scoped"
            session["org_ids"] = ["org1"]
            assert load_report("final-report.json") is None

    def test_org_scoped_session_fails_closed_when_backend_unreachable(self, reports_dir, mocker):
        (reports_dir / "image-size-report.json").write_text(json.dumps({"summary": {}, "images": []}))
        mocker.patch("app._get_org_scope", return_value=None)
        with app.test_request_context():
            session["access_scope"] = "org_scoped"
            session["org_ids"] = ["org1"]
            assert load_report("image-size-report.json") is None


class TestGetOrgScope:
    def test_no_org_ids_returns_empty_scope_without_backend_call(self, mocker):
        mock_get = mocker.patch("app.httpx.get")
        with app.test_request_context():
            session["org_ids"] = []
            result = frontend_app._get_org_scope()
        assert result == {"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}}
        mock_get.assert_not_called()

    def test_fetches_and_caches_scope(self, mocker):
        scope = {"org_ids": ["org1"], "project_ids": [], "tags": ["t1"], "other_owners": {}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = scope
        mock_resp.raise_for_status.return_value = None
        mock_get = mocker.patch("app.httpx.get", return_value=mock_resp)
        with app.test_request_context():
            session["org_ids"] = ["org1"]
            first = frontend_app._get_org_scope()
            second = frontend_app._get_org_scope()
        assert first == scope
        assert second == scope
        mock_get.assert_called_once()  # second call served from session cache

    def test_backend_error_returns_none(self, mocker):
        import httpx as _httpx

        mocker.patch("app.httpx.get", side_effect=_httpx.ConnectError("refused"))
        with app.test_request_context():
            session["org_ids"] = ["org1"]
            assert frontend_app._get_org_scope() is None


# ── resolve_access_scope ─────────────────────────────────────────────────────────


class TestResolveAccessScope:
    def test_disabled_when_no_domino_api_url(self, client, reports_dir, monkeypatch):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "")
        r = client.get("/")
        assert r.status_code == 200

    def test_admin_allowed(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        mocker.patch(
            "app.httpx.get",
            return_value=_mock_httpx_response({"isAdmin": True, "canonicalName": "alice"}),
        )
        client.set_cookie("dominoAuth", "abc")
        r = client.get("/")
        assert r.status_code == 200
        with client.session_transaction() as sess:
            assert sess["access_scope"] == "admin"
            assert sess["domino_username"] == "alice"

    def test_org_scoped_allowed(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        principal_resp = _mock_httpx_response({"isAdmin": False, "canonicalName": "bob"})
        orgs_resp = _mock_httpx_response({"orgs": [{"id": "org1", "name": "Team A"}]})
        mocker.patch("app.httpx.get", side_effect=[principal_resp, orgs_resp])
        client.set_cookie("dominoAuth", "abc")
        r = client.get("/")
        assert r.status_code == 200
        with client.session_transaction() as sess:
            assert sess["access_scope"] == "org_scoped"
            assert sess["org_ids"] == ["org1"]

    def test_denied_when_no_org_membership(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        principal_resp = _mock_httpx_response({"isAdmin": False})
        orgs_resp = _mock_httpx_response({"orgs": []})
        mocker.patch("app.httpx.get", side_effect=[principal_resp, orgs_resp])
        client.set_cookie("dominoAuth", "abc")
        r = client.get("/")
        assert r.status_code == 403
        with client.session_transaction() as sess:
            assert sess["access_scope"] == "denied"

    def test_denied_when_not_authenticated(self, client, reports_dir, monkeypatch):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        r = client.get("/")
        assert r.status_code == 302

    def test_503_when_principal_unreachable(self, client, reports_dir, monkeypatch, mocker):
        import httpx as _httpx

        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        mocker.patch("app.httpx.get", side_effect=_httpx.RequestError("boom"))
        client.set_cookie("dominoAuth", "abc")
        r = client.get("/")
        assert r.status_code == 503

    def test_503_when_org_lookup_unreachable(self, client, reports_dir, monkeypatch, mocker):
        import httpx as _httpx

        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        principal_resp = _mock_httpx_response({"isAdmin": False})
        mocker.patch("app.httpx.get", side_effect=[principal_resp, _httpx.RequestError("boom")])
        client.set_cookie("dominoAuth", "abc")
        r = client.get("/")
        assert r.status_code == 503

    def test_cache_skips_second_nucleus_round_trip(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        mock_get = mocker.patch(
            "app.httpx.get",
            return_value=_mock_httpx_response({"isAdmin": True, "canonicalName": "alice"}),
        )
        client.set_cookie("dominoAuth", "abc")
        client.get("/")
        assert mock_get.call_count == 1
        r2 = client.get("/")
        assert r2.status_code == 200
        assert mock_get.call_count == 1  # served from the cached scope, no second call

    def test_cache_expires_after_ttl(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        monkeypatch.setattr(frontend_app, "_AUTH_CACHE_TTL", 0)
        mock_get = mocker.patch(
            "app.httpx.get",
            return_value=_mock_httpx_response({"isAdmin": True, "canonicalName": "alice"}),
        )
        client.set_cookie("dominoAuth", "abc")
        client.get("/")
        client.get("/")
        assert mock_get.call_count == 2  # TTL of 0 forces a re-check every time

    def test_cached_denial_still_denies(self, client, reports_dir, monkeypatch, mocker):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        principal_resp = _mock_httpx_response({"isAdmin": False})
        orgs_resp = _mock_httpx_response({"orgs": []})
        mock_get = mocker.patch("app.httpx.get", side_effect=[principal_resp, orgs_resp])
        client.set_cookie("dominoAuth", "abc")
        client.get("/")
        assert mock_get.call_count == 2
        r2 = client.get("/")
        assert r2.status_code == 403
        assert mock_get.call_count == 2  # denial itself is cached too


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
