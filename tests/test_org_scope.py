"""
Tests for OrgScopeResolver / resolve_org_scope (python/utils/org_scope.py).

Follows the existing Mongo-mocking pattern used in tests/test_unused_environments.py:
a mock db whose __getitem__ dispatches to per-collection mocks, with .find()/.aggregate()
return values set per test.
"""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from utils.org_scope import OrgScopeResolver, _to_object_ids, resolve_org_scope


def _mock_db_with_collections(collections: dict) -> MagicMock:
    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: collections.get(key, MagicMock())
    mock_db.list_collection_names.return_value = list(collections.keys())
    return mock_db


def _empty_collection() -> MagicMock:
    c = MagicMock()
    c.find.return_value = []
    c.aggregate.return_value = []
    return c


@pytest.fixture
def collections():
    """A full set of empty collection mocks, so a test only needs to override
    the ones it actually cares about."""
    return {
        "projects": _empty_collection(),
        "organizations": _empty_collection(),
        "runs": _empty_collection(),
        "scheduler_jobs": _empty_collection(),
        "workspace": _empty_collection(),
        "environments_v2": _empty_collection(),
        "environment_revisions": _empty_collection(),
        "users": _empty_collection(),
        "model_versions": _empty_collection(),
        "model_products": _empty_collection(),
        "app_versions": _empty_collection(),
    }


@pytest.fixture
def make_resolver(collections):
    """Build an OrgScopeResolver wired to the given (mutable) collections dict."""

    def _make():
        mock_client = MagicMock()
        mock_db = _mock_db_with_collections(collections)
        mock_client.__getitem__.return_value = mock_db
        return OrgScopeResolver(client=mock_client)

    return _make


# ── _to_object_ids ──────────────────────────────────────────────────────────


def test_to_object_ids_skips_invalid_entries():
    valid = ObjectId()
    result = _to_object_ids([str(valid), "not-an-object-id", ""])
    assert result == [valid]


# ── owned_project_ids ────────────────────────────────────────────────────────


class TestOwnedProjectIds:
    def test_returns_matching_projects(self, make_resolver, collections):
        org_id = ObjectId()
        project_id = ObjectId()
        collections["projects"].find.return_value = [{"_id": project_id}]
        resolver = make_resolver()

        result = resolver.owned_project_ids([org_id])

        assert result == [project_id]
        find_args = collections["projects"].find.call_args[0][0]
        assert find_args["ownerId"] == {"$in": [org_id]}
        assert find_args["isArchived"] == {"$ne": True}

    def test_empty_org_ids_short_circuits(self, make_resolver, collections):
        resolver = make_resolver()
        assert resolver.owned_project_ids([]) == []
        collections["projects"].find.assert_not_called()


# ── owned_environment_tags ───────────────────────────────────────────────────


class TestOwnedEnvironmentTags:
    def test_resolves_tag_via_active_revision(self, make_resolver, collections):
        org_id = ObjectId()
        revision_id = ObjectId()
        collections["environments_v2"].find.return_value = [{"activeRevisionId": revision_id}]
        collections["environment_revisions"].find.return_value = [
            {"metadata": {"dockerImageName": {"tag": "org-env-tag-1"}}}
        ]

        resolver = make_resolver()
        tags = resolver.owned_environment_tags([org_id])

        assert tags == ["org-env-tag-1"]
        env_query = collections["environments_v2"].find.call_args[0][0]
        assert env_query["ownerId"] == {"$in": [org_id]}
        assert env_query["visibility"] == "Organization"

    def test_no_environments_returns_empty(self, make_resolver, collections):
        collections["environments_v2"].find.return_value = []
        resolver = make_resolver()
        assert resolver.owned_environment_tags([ObjectId()]) == []
        collections["environment_revisions"].find.assert_not_called()


# ── resolve() ────────────────────────────────────────────────────────────────


class TestResolve:
    def test_org_owned_project_tag_is_visible(self, make_resolver, collections):
        org_id = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "proj-tag-1", "owner_id": org_id},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["org_ids"] == [str(org_id)]
        assert scope["tags"] == ["proj-tag-1"]
        assert scope["other_owners"] == {}

    def test_run_owned_by_org_project_is_visible(self, make_resolver, collections):
        org_id = ObjectId()
        collections["runs"].aggregate.return_value = [
            {"environment_docker_tag": "run-tag-1", "project_owner_id": org_id},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == ["run-tag-1"]

    def test_workspace_falls_back_to_project_owner_map(self, make_resolver, collections):
        org_id = ObjectId()
        project_id = ObjectId()
        # No owner_id directly on the workspace usage record itself, only project_id —
        # resolver must fall back to the project -> owner map.
        collections["projects"].find.return_value = [{"_id": project_id, "ownerId": org_id}]
        collections["workspace"].aggregate.return_value = [
            {"environment_docker_tag": "ws-tag-1", "project_id": project_id, "owner_id": None},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == ["ws-tag-1"]

    def test_directly_owned_environment_included_even_if_unused(self, make_resolver, collections):
        org_id = ObjectId()
        revision_id = ObjectId()
        collections["environments_v2"].find.return_value = [{"activeRevisionId": revision_id}]
        collections["environment_revisions"].find.return_value = [
            {"metadata": {"dockerImageName": {"tag": "unused-org-env-tag"}}}
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == ["unused-org-env-tag"]

    def test_shared_tag_names_other_org(self, make_resolver, collections):
        my_org = ObjectId()
        other_org = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "owner_id": my_org},
        ]
        collections["runs"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "project_owner_id": other_org},
        ]
        collections["organizations"].find.return_value = [{"_id": other_org, "name": "Other Org"}]
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        assert scope["tags"] == ["shared-tag"]
        assert scope["other_owners"] == {
            "shared-tag": [{"type": "organization", "id": str(other_org), "name": "Other Org"}]
        }

    def test_shared_tag_names_personal_user_not_anonymized(self, make_resolver, collections):
        """Per the decided behavior (docs/org-scoped-access-plan.md §3.3): a
        personal (non-org) co-owner of a shared image is named, not hidden
        behind a generic note."""
        my_org = ObjectId()
        other_user = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "owner_id": my_org},
        ]
        collections["runs"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "project_owner_id": other_user},
        ]
        # Not found in organizations -> falls back to users.
        collections["organizations"].find.return_value = []
        collections["users"].find.return_value = [{"_id": other_user, "fullName": "Alice Example"}]
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        assert scope["other_owners"] == {
            "shared-tag": [{"type": "user", "id": str(other_user), "name": "Alice Example"}]
        }

    def test_user_name_falls_back_to_login_id(self, make_resolver, collections):
        my_org = ObjectId()
        other_user = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "owner_id": my_org},
        ]
        collections["runs"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "project_owner_id": other_user},
        ]
        collections["organizations"].find.return_value = []
        collections["users"].find.return_value = [{"_id": other_user, "loginId": {"id": "alice"}}]
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        assert scope["other_owners"]["shared-tag"][0]["name"] == "alice"

    def test_tag_not_owned_by_org_is_not_visible(self, make_resolver, collections):
        my_org = ObjectId()
        other_org = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "not-mine", "owner_id": other_org},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        assert scope["tags"] == []
        assert scope["other_owners"] == {}

    def test_unresolvable_owner_is_skipped_not_fatal(self, make_resolver, collections):
        my_org = ObjectId()
        mystery_owner = ObjectId()
        collections["projects"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "owner_id": my_org},
        ]
        collections["runs"].aggregate.return_value = [
            {"environment_docker_tag": "shared-tag", "project_owner_id": mystery_owner},
        ]
        collections["organizations"].find.return_value = []
        collections["users"].find.return_value = []  # not found anywhere
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        # The tag is still visible; the unresolvable other-owner is simply
        # omitted rather than raising or crashing the whole request.
        assert scope["tags"] == ["shared-tag"]
        assert scope["other_owners"] == {}

    def test_project_ids_reflects_direct_ownership(self, make_resolver, collections):
        org_id = ObjectId()
        project_id = ObjectId()
        collections["projects"].find.return_value = [{"_id": project_id}]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["project_ids"] == [str(project_id)]

    def test_model_image_owned_by_org_project_is_visible(self, make_resolver, collections, mocker):
        """model_versions.projectId (confirmed live) is the org-attribution
        path for model images — not models.metadata.createdBy, which is
        always an individual user. See org_scope.py's module docstring."""
        org_id = ObjectId()
        project_id = ObjectId()
        version_id = ObjectId()
        collections["projects"].find.return_value = [{"_id": project_id, "ownerId": org_id}]
        collections["model_versions"].find.return_value = [{"_id": version_id, "projectId": project_id}]
        mock_service_cls = mocker.patch("utils.org_scope.ImageUsageService")
        mock_service_cls.return_value.collect_model_version_slugs.return_value = {str(version_id): "model-tag-1"}
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == ["model-tag-1"]
        # Only called with ids that actually resolved to an owner — never an
        # unfiltered "give me everything" call.
        mock_service_cls.return_value.collect_model_version_slugs.assert_called_once_with([str(version_id)])

    def test_model_version_with_no_owning_project_is_excluded(self, make_resolver, collections, mocker):
        """A model_version whose projectId doesn't match any known project
        (e.g. the project was deleted) contributes no tag — not an error,
        and collect_model_version_slugs is never called with an empty list."""
        version_id = ObjectId()
        collections["model_versions"].find.return_value = [{"_id": version_id, "projectId": ObjectId()}]
        mock_service_cls = mocker.patch("utils.org_scope.ImageUsageService")
        resolver = make_resolver()

        scope = resolver.resolve([str(ObjectId())])

        assert scope["tags"] == []
        mock_service_cls.return_value.collect_model_version_slugs.assert_not_called()

    def test_model_version_with_no_slug_tag_contributes_nothing(self, make_resolver, collections, mocker):
        """A model version that resolved to an owning project but has no
        buildable slug tag (e.g. every build failed) is silently excluded,
        not an error."""
        org_id = ObjectId()
        project_id = ObjectId()
        version_id = ObjectId()
        collections["projects"].find.return_value = [{"_id": project_id, "ownerId": org_id}]
        collections["model_versions"].find.return_value = [{"_id": version_id, "projectId": project_id}]
        mock_service_cls = mocker.patch("utils.org_scope.ImageUsageService")
        mock_service_cls.return_value.collect_model_version_slugs.return_value = {}
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == []

    def test_environment_used_by_org_app_is_visible(self, make_resolver, collections):
        """Apps have no image of their own — they run on a compute
        environment image (see org_scope.py's module docstring). This
        confirms that environment image is attributed to the org owning the
        app, via app_versions.appId -> model_products._id -> projectId
        (confirmed live, a two-hop join since app_versions carries no owner
        info of its own) — so it's recognized as still needed if the app
        were re-run after a registry cleanup."""
        org_id = ObjectId()
        project_id = ObjectId()
        product_id = ObjectId()
        collections["projects"].find.return_value = [{"_id": project_id, "ownerId": org_id}]
        collections["model_products"].find.return_value = [{"_id": product_id, "projectId": project_id}]
        collections["app_versions"].aggregate.return_value = [
            {"app_version_id": "av1", "app_id": product_id, "environment_docker_tag": "app-env-tag-1"},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(org_id)])

        assert scope["tags"] == ["app-env-tag-1"]

    def test_app_version_with_unowned_product_is_excluded(self, make_resolver, collections):
        collections["app_versions"].aggregate.return_value = [
            {"app_version_id": "av1", "app_id": ObjectId(), "environment_docker_tag": "app-env-tag-1"},
        ]
        resolver = make_resolver()

        scope = resolver.resolve([str(ObjectId())])

        assert scope["tags"] == []

    def test_model_image_shared_with_other_org_is_named(self, make_resolver, collections, mocker):
        my_org = ObjectId()
        other_org = ObjectId()
        my_project = ObjectId()
        other_project = ObjectId()
        v1, v2 = ObjectId(), ObjectId()
        collections["projects"].find.return_value = [
            {"_id": my_project, "ownerId": my_org},
            {"_id": other_project, "ownerId": other_org},
        ]
        collections["model_versions"].find.return_value = [
            {"_id": v1, "projectId": my_project},
            {"_id": v2, "projectId": other_project},
        ]
        mock_service_cls = mocker.patch("utils.org_scope.ImageUsageService")
        # Both model versions happen to resolve to the same shared slug tag.
        mock_service_cls.return_value.collect_model_version_slugs.return_value = {
            str(v1): "shared-model-tag",
            str(v2): "shared-model-tag",
        }
        collections["organizations"].find.return_value = [{"_id": other_org, "name": "Other Org"}]
        resolver = make_resolver()

        scope = resolver.resolve([str(my_org)])

        assert scope["tags"] == ["shared-model-tag"]
        assert scope["other_owners"] == {
            "shared-model-tag": [{"type": "organization", "id": str(other_org), "name": "Other Org"}]
        }


# ── resolve_org_scope (module-level convenience wrapper) ─────────────────────


def test_resolve_org_scope_opens_and_closes_client(mocker):
    mock_client = MagicMock()
    mock_db = _mock_db_with_collections(
        {
            "projects": _empty_collection(),
            "organizations": _empty_collection(),
            "runs": _empty_collection(),
            "scheduler_jobs": _empty_collection(),
            "workspace": _empty_collection(),
            "environments_v2": _empty_collection(),
        }
    )
    mock_client.__getitem__.return_value = mock_db
    mocker.patch("utils.org_scope.get_mongo_client", return_value=mock_client)

    result = resolve_org_scope([str(ObjectId())])

    assert result["tags"] == []
    mock_client.close.assert_called_once()
