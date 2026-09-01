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

    def test_uses_passed_org_scope_without_refetching(self, reports_dir, mocker):
        """load_report(filename, org_scope=...) — U1/U3's callers (view_report,
        api_report_detail) resolve scope themselves (possibly narrowed) and
        pass it in, so this must not silently re-fetch its own (unnarrowed)
        copy via _get_org_scope()."""
        images = [{"tag": "my-tag", "size_bytes": 10}]
        data = {"summary": {"total_images": 1}, "images": images, "entries": images}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        mock_get_scope = mocker.patch("app._get_org_scope")
        passed_scope = {"org_ids": ["org1"], "project_ids": [], "tags": ["my-tag"], "other_owners": {}}
        with app.test_request_context():
            session["access_scope"] = "org_scoped"
            session["org_ids"] = ["org1"]
            result = load_report("image-size-report.json", org_scope=passed_scope)
        assert [i["tag"] for i in result["images"]] == ["my-tag"]
        mock_get_scope.assert_not_called()


# ── U1: multi-org selector — narrowing helpers ──────────────────────────────────


class TestOrgIdsOverride:
    """_org_ids_override() reads ?narrow=1&org_ids=... off the query string.
    The explicit `narrow` marker (not just org_ids' presence/absence) is
    what distinguishes "no override" from "the user explicitly narrowed to
    a subset, possibly the empty subset"."""

    def test_returns_none_without_narrow_param(self):
        with app.test_request_context("/reports/x.json"):
            assert frontend_app._org_ids_override() is None

    def test_returns_none_when_org_ids_present_but_not_narrowed(self):
        # org_ids alone (no narrow=1) must not be mistaken for an override —
        # otherwise a stray/unexpected query param could silently narrow scope.
        with app.test_request_context("/reports/x.json?org_ids=org1"):
            assert frontend_app._org_ids_override() is None

    def test_returns_selected_org_ids_when_narrowed(self):
        with app.test_request_context("/reports/x.json?narrow=1&org_ids=org1&org_ids=org2"):
            assert frontend_app._org_ids_override() == ["org1", "org2"]

    def test_returns_empty_list_for_explicit_empty_selection(self):
        # Unchecking every org is a valid, meaningful choice: show nothing —
        # distinct from "no override" (which falls back to the full set).
        with app.test_request_context("/reports/x.json?narrow=1"):
            assert frontend_app._org_ids_override() == []


class TestGetOrgScopeNarrowing:
    """_get_org_scope(org_ids_override=...) — U1's narrowed re-resolution.
    Clamped to the session's own org_ids and never cached, unlike the
    default (no-override) path exercised by TestGetOrgScope above."""

    def _mock_scope_response(self, mocker, scope):
        resp = MagicMock()
        resp.json.return_value = scope
        resp.raise_for_status.return_value = None
        return mocker.patch("app.httpx.get", return_value=resp)

    def test_clamps_override_to_session_org_ids(self, mocker):
        mock_get = self._mock_scope_response(
            mocker, {"org_ids": ["org1"], "project_ids": [], "tags": [], "other_owners": {}}
        )
        with app.test_request_context():
            session["org_ids"] = ["org1", "org2"]
            frontend_app._get_org_scope(["org1", "not-mine"])
        requested = [oid for _, oid in mock_get.call_args.kwargs["params"]]
        assert requested == ["org1"]  # "not-mine" dropped; "org2" never asked for (not selected)

    def test_narrowed_request_not_cached(self, mocker):
        mock_get = self._mock_scope_response(
            mocker, {"org_ids": ["org1"], "project_ids": [], "tags": ["t1"], "other_owners": {}}
        )
        with app.test_request_context():
            session["org_ids"] = ["org1"]
            frontend_app._get_org_scope(["org1"])
            frontend_app._get_org_scope(["org1"])
            assert mock_get.call_count == 2  # no caching for narrowed requests
            assert "org_scope_checked_at" not in session

    def test_narrowed_call_does_not_clobber_the_unnarrowed_cache(self, mocker):
        scope_full = {"org_ids": ["org1"], "project_ids": [], "tags": ["t1", "t2"], "other_owners": {}}
        scope_narrow = {"org_ids": ["org1"], "project_ids": [], "tags": ["t1"], "other_owners": {}}
        resp_full, resp_narrow = MagicMock(), MagicMock()
        resp_full.json.return_value, resp_full.raise_for_status.return_value = scope_full, None
        resp_narrow.json.return_value, resp_narrow.raise_for_status.return_value = scope_narrow, None
        mocker.patch("app.httpx.get", side_effect=[resp_full, resp_narrow])
        with app.test_request_context():
            session["org_ids"] = ["org1"]
            first = frontend_app._get_org_scope()  # caches scope_full
            frontend_app._get_org_scope(["org1"])  # narrowed — must not overwrite the cache
            second = frontend_app._get_org_scope()  # served from cache, still scope_full
        assert first == scope_full
        assert second == scope_full

    def test_empty_override_selection_returns_empty_scope_without_backend_call(self, mocker):
        mock_get = mocker.patch("app.httpx.get")
        with app.test_request_context():
            session["org_ids"] = ["org1"]
            result = frontend_app._get_org_scope([])
        assert result == {"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}}
        mock_get.assert_not_called()


class TestOrgScopeViewRoute:
    """GET /api/org-scope-view — the frontend-facing view report.html's JS
    polls on every org-filter checkbox change (U1), and the source of
    U3's `other_owners`."""

    def test_admin_session_gets_empty_scope_without_backend_call(self, client, mocker):
        mock_get = mocker.patch("app.httpx.get")
        with client.session_transaction() as sess:
            sess["access_scope"] = "admin"
        r = client.get("/api/org-scope-view")
        assert r.status_code == 200
        assert r.get_json() == {"org_ids": [], "project_ids": [], "tags": [], "other_owners": {}}
        mock_get.assert_not_called()

    def test_org_scoped_session_gets_resolved_scope(self, client, mocker):
        scope = {
            "org_ids": ["org1"],
            "project_ids": [],
            "tags": ["t1"],
            "other_owners": {"t1": [{"type": "user", "id": "u1", "name": "Alice"}]},
        }
        resp = MagicMock()
        resp.json.return_value = scope
        resp.raise_for_status.return_value = None
        mocker.patch("app.httpx.get", return_value=resp)
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1"]
        r = client.get("/api/org-scope-view")
        assert r.status_code == 200
        assert r.get_json() == scope

    def test_narrowing_via_query_params(self, client, mocker):
        resp = MagicMock()
        resp.json.return_value = {"org_ids": ["org1"], "project_ids": [], "tags": [], "other_owners": {}}
        resp.raise_for_status.return_value = None
        mock_get = mocker.patch("app.httpx.get", return_value=resp)
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1", "org2"]
        client.get("/api/org-scope-view?narrow=1&org_ids=org1")
        requested = [oid for _, oid in mock_get.call_args.kwargs["params"]]
        assert requested == ["org1"]

    def test_backend_unreachable_returns_503(self, client, mocker):
        import httpx as _httpx

        mocker.patch("app.httpx.get", side_effect=_httpx.ConnectError("refused"))
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1"]
        r = client.get("/api/org-scope-view")
        assert r.status_code == 503


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
            assert sess["org_names"] == {"org1": "Team A"}  # kept for U1's selector labels

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


# ── U2: org-scoped sessions are read-only — no job-triggering ──────────────────


class TestOperationsBlockedForOrgScoped:
    """Org-scoped members get read-only reporting only — no job-triggering,
    no destructive actions (docs/org-scoped-access-plan.md §3.2, decided).
    base.html hides the Operations nav link and buttons for this scope
    (not asserted here — no JS/HTML-rendering test harness in this repo),
    but that's not enforcement: resolve_access_scope() must reject a direct
    request to any of these routes too, admin sessions unaffected."""

    def _seed_org_scoped_session(self, client, monkeypatch):
        # DOMINO_API_URL must be set for resolve_access_scope() to run at
        # all; seeding auth_checked_at recent enough puts every request
        # through the cache-hit branch (the common case, and the one that
        # silently regressed if only the fresh-resolution branch rejects).
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["domino_username"] = "bob"
            sess["org_ids"] = ["org1"]
            sess["org_names"] = {"org1": "Team A"}
            sess["auth_checked_at"] = time.time()

    def test_operations_page_rejected(self, client, monkeypatch):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.get("/operations")
        assert r.status_code == 403

    def test_api_operations_catalogue_rejected(self, client, monkeypatch):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.get("/api/operations")
        assert r.status_code == 403
        assert "error" in r.get_json()

    def test_api_jobs_list_rejected(self, client, monkeypatch):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.get("/api/jobs")
        assert r.status_code == 403

    def test_api_jobs_create_rejected_without_reaching_backend(self, client, monkeypatch, mocker):
        self._seed_org_scoped_session(client, monkeypatch)
        mock_post = mocker.patch("app.httpx.post")
        r = client.post("/api/jobs", json={"operation": "health_check", "params": {}})
        assert r.status_code == 403
        mock_post.assert_not_called()

    def test_api_job_status_rejected(self, client, monkeypatch):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.get("/api/jobs/abc123")
        assert r.status_code == 403

    def test_api_job_cancel_rejected(self, client, monkeypatch):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.delete("/api/jobs/abc123")
        assert r.status_code == 403

    def test_reports_routes_unaffected(self, client, monkeypatch, reports_dir):
        self._seed_org_scoped_session(client, monkeypatch)
        r = client.get("/")
        assert r.status_code == 200

    def test_admin_session_still_allowed(self, client, monkeypatch):
        monkeypatch.setattr(frontend_app, "DOMINO_API_URL", "https://domino.example.com")
        with client.session_transaction() as sess:
            sess["access_scope"] = "admin"
            sess["domino_username"] = "alice"
            sess["auth_checked_at"] = time.time()
        r = client.get("/operations")
        assert r.status_code == 200


# ── U1/U3: multi-org selector + shared-image note in report.html ───────────────


class TestOrgScopedTemplateRendering:
    def test_operations_link_hidden_for_org_scoped(self, client, reports_dir):
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1"]
        r = client.get("/")
        assert r.status_code == 200
        assert b'href="/operations"' not in r.data
        assert b"Read-only" in r.data

    def test_operations_link_shown_for_admin(self, client, reports_dir):
        with client.session_transaction() as sess:
            sess["access_scope"] = "admin"
        r = client.get("/")
        assert r.status_code == 200
        assert b'href="/operations"' in r.data

    def test_org_filter_card_shown_for_multi_org_session(self, client, reports_dir, mocker):
        data = {"summary": {}, "images": [], "entries": []}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        mocker.patch(
            "app._get_org_scope",
            return_value={"org_ids": ["org1", "org2"], "project_ids": [], "tags": [], "other_owners": {}},
        )
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1", "org2"]
            sess["org_names"] = {"org1": "Team A", "org2": "Team B"}
        r = client.get("/reports/image-size-report.json")
        assert r.status_code == 200
        assert b"Team A" in r.data
        assert b"Team B" in r.data
        assert b'class="org-filter-checkbox"' in r.data

    def test_org_filter_card_hidden_for_single_org_session(self, client, reports_dir, mocker):
        # No narrowing decision to make with only one org — matches U1's
        # acceptance criteria (the selector is meaningful for 2+ orgs).
        data = {"summary": {}, "images": [], "entries": []}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        mocker.patch(
            "app._get_org_scope",
            return_value={"org_ids": ["org1"], "project_ids": [], "tags": [], "other_owners": {}},
        )
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1"]
            sess["org_names"] = {"org1": "Team A"}
        r = client.get("/reports/image-size-report.json")
        assert r.status_code == 200
        assert b'class="org-filter-checkbox"' not in r.data

    def test_other_owners_injected_for_org_scoped_view(self, client, reports_dir, mocker):
        data = {"summary": {}, "images": [{"tag": "shared-tag"}], "entries": [{"tag": "shared-tag"}]}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        mocker.patch(
            "app._get_org_scope",
            return_value={
                "org_ids": ["org1"],
                "project_ids": [],
                "tags": ["shared-tag"],
                "other_owners": {"shared-tag": [{"type": "organization", "id": "org2", "name": "Team B"}]},
            },
        )
        with client.session_transaction() as sess:
            sess["access_scope"] = "org_scoped"
            sess["org_ids"] = ["org1"]
        r = client.get("/reports/image-size-report.json")
        assert r.status_code == 200
        assert b"Team B" in r.data  # embedded in the `otherOwners` JS global

    def test_other_owners_empty_for_admin_view(self, client, reports_dir):
        data = {"summary": {}, "images": []}
        (reports_dir / "image-size-report.json").write_text(json.dumps(data))
        with client.session_transaction() as sess:
            sess["access_scope"] = "admin"
        r = client.get("/reports/image-size-report.json")
        assert r.status_code == 200
        assert b"let otherOwners = {};" in r.data


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
