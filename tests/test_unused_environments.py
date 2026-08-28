"""
Tests for UnusedEnvironmentsFinder in delete_unused_environments.py.

These tests verify:
- Domino-shipped system environments (revisions authored by the SYSTEM_AUTHOR_ID sentinel)
  are automatically protected from deletion, with no admin-maintained allowlist required.
- Ordinary user-authored environments are unaffected by that protection.
- generate_report() surfaces protected tags visibly (protected=True) rather than silently
  omitting them, without counting them toward deletable totals.
- load_unused_tags_from_file() refuses to load protected=True entries even if present in
  a saved/edited report file, as a safety net against replaying them into deletion.
"""

import json
from unittest.mock import MagicMock

import pytest
from bson import ObjectId


@pytest.fixture
def mock_config_manager(mocker):
    """Fixture providing mocked config_manager."""
    mock = mocker.patch("utils.deletion_base.config_manager")
    mock.get_registry_url.return_value = "docker-registry.domino-platform.svc.cluster.local:5000"
    mock.get_repository.return_value = "dominodatalab"
    mock.get_domino_platform_namespace.return_value = "domino-platform"
    mock.get_mongo_db.return_value = "domino"
    mock.get_max_workers.return_value = 4
    return mock


@pytest.fixture
def mock_finder_deps(mocker, mock_config_manager):
    """Set up mocks for UnusedEnvironmentsFinder dependencies."""
    mocker.patch("scripts.delete_unused_environments.config_manager", mock_config_manager)

    mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
    mocker.patch("utils.deletion_base.SkopeoClient")
    mocker.patch("utils.deletion_base.HealthChecker")
    mocker.patch("utils.deletion_base.CheckpointManager")

    return mock_config_manager


def _mock_db_with_collections(collections: dict) -> MagicMock:
    """Build a mock Mongo db handle whose __getitem__ dispatches to the given collection mocks
    and whose list_collection_names() reports only those collections exist."""
    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: collections.get(key, MagicMock())
    mock_db.list_collection_names.return_value = list(collections.keys())
    return mock_db


class TestSystemEnvironmentProtection:
    """Tests for automatic protection of Domino-shipped system environments."""

    def test_system_authored_environment_is_protected(self, mocker, mock_finder_deps):
        """An environment whose revision has the SYSTEM_AUTHOR_ID sentinel authorId is protected."""
        from scripts.delete_unused_environments import SYSTEM_AUTHOR_ID, UnusedEnvironmentsFinder

        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_unused_environments.get_mongo_client", return_value=mock_mongo_client)

        spark_env_id = ObjectId()
        spark_rev_id = ObjectId()

        mock_env_collection = MagicMock()
        mock_env_collection.find.return_value = [{"_id": spark_env_id, "name": "Spark Compute Environment"}]

        mock_rev_collection = MagicMock()
        mock_rev_collection.find.return_value = [
            {
                "_id": spark_rev_id,
                "environmentId": spark_env_id,
                "metadata": {"authorId": SYSTEM_AUTHOR_ID},
            }
        ]

        mock_projects_collection = MagicMock()
        mock_projects_collection.find.return_value = []

        mock_scheduler_collection = MagicMock()
        mock_scheduler_collection.find.return_value = []

        mock_db = _mock_db_with_collections(
            {
                "environments_v2": mock_env_collection,
                "environment_revisions": mock_rev_collection,
                "projects": mock_projects_collection,
                "scheduler_jobs": mock_scheduler_collection,
            }
        )
        mock_mongo_client.__getitem__.return_value = mock_db

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")
        (
            all_env_ids,
            all_revision_ids,
            default_env_ids,
            scheduler_env_ids,
            app_version_env_ids,
            org_default_env_ids,
            user_pref_env_ids,
            system_protected_ids,
        ) = finder.fetch_all_environments_and_defaults()

        assert str(spark_env_id) in system_protected_ids
        assert str(spark_rev_id) in system_protected_ids

    def test_user_authored_environment_is_not_protected(self, mocker, mock_finder_deps):
        """An environment whose revision has a real user's authorId is not protected."""
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_unused_environments.get_mongo_client", return_value=mock_mongo_client)

        user_env_id = ObjectId()
        user_rev_id = ObjectId()
        real_user_id = ObjectId()

        mock_env_collection = MagicMock()
        mock_env_collection.find.return_value = [{"_id": user_env_id, "name": "My Custom Environment"}]

        mock_rev_collection = MagicMock()
        mock_rev_collection.find.return_value = [
            {
                "_id": user_rev_id,
                "environmentId": user_env_id,
                "metadata": {"authorId": real_user_id},
            }
        ]

        mock_projects_collection = MagicMock()
        mock_projects_collection.find.return_value = []

        mock_scheduler_collection = MagicMock()
        mock_scheduler_collection.find.return_value = []

        mock_db = _mock_db_with_collections(
            {
                "environments_v2": mock_env_collection,
                "environment_revisions": mock_rev_collection,
                "projects": mock_projects_collection,
                "scheduler_jobs": mock_scheduler_collection,
            }
        )
        mock_mongo_client.__getitem__.return_value = mock_db

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")
        (
            all_env_ids,
            all_revision_ids,
            default_env_ids,
            scheduler_env_ids,
            app_version_env_ids,
            org_default_env_ids,
            user_pref_env_ids,
            system_protected_ids,
        ) = finder.fetch_all_environments_and_defaults()

        assert str(user_env_id) not in system_protected_ids
        assert str(user_rev_id) not in system_protected_ids

    def test_revision_with_missing_metadata_is_not_protected(self, mocker, mock_finder_deps):
        """A revision with no metadata at all must not raise and must not be treated as protected."""
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_unused_environments.get_mongo_client", return_value=mock_mongo_client)

        env_id = ObjectId()
        rev_id = ObjectId()

        mock_env_collection = MagicMock()
        mock_env_collection.find.return_value = [{"_id": env_id, "name": "Some Environment"}]

        mock_rev_collection = MagicMock()
        # No "metadata" key at all, and a second doc with metadata explicitly set to None.
        mock_rev_collection.find.return_value = [
            {"_id": rev_id, "environmentId": env_id},
            {"_id": ObjectId(), "environmentId": env_id, "metadata": None},
        ]

        mock_projects_collection = MagicMock()
        mock_projects_collection.find.return_value = []

        mock_scheduler_collection = MagicMock()
        mock_scheduler_collection.find.return_value = []

        mock_db = _mock_db_with_collections(
            {
                "environments_v2": mock_env_collection,
                "environment_revisions": mock_rev_collection,
                "projects": mock_projects_collection,
                "scheduler_jobs": mock_scheduler_collection,
            }
        )
        mock_mongo_client.__getitem__.return_value = mock_db

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        # Should not raise despite missing/null metadata.
        result = finder.fetch_all_environments_and_defaults()
        system_protected_ids = result[-1]

        assert str(env_id) not in system_protected_ids
        assert str(rev_id) not in system_protected_ids

    def test_find_unused_environments_excludes_system_environment(self, mocker, mock_finder_deps):
        """End-to-end: a system-authored environment with zero usage is not returned as unused."""
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        mocker.patch.object(finder, "load_metadata_files", return_value=([], [], []))
        mocker.patch.object(finder, "extract_used_environment_ids", return_value=set())

        spark_env_id = ObjectId()
        spark_rev_id = ObjectId()
        user_env_id = ObjectId()
        user_rev_id = ObjectId()

        mocker.patch.object(
            finder,
            "fetch_all_environments_and_defaults",
            return_value=(
                {str(spark_env_id): "Spark Compute Environment", str(user_env_id): "My Custom Environment"},
                {str(spark_rev_id): "Spark Compute Environment", str(user_rev_id): "My Custom Environment"},
                set(),  # default_env_ids
                set(),  # scheduler_env_ids
                set(),  # app_version_env_ids
                set(),  # org_default_env_ids
                set(),  # user_pref_env_ids
                {  # system_protected_ids: obj_id -> env_name
                    str(spark_env_id): "Spark Compute Environment",
                    str(spark_rev_id): "Spark Compute Environment",
                },
            ),
        )

        unused, protected = finder.find_unused_environments()
        unused_ids = {e.object_id for e in unused}
        protected_ids = {e.object_id for e in protected}

        # Neither the Spark environment nor its revision should show up as "unused" —
        # they were protected even though nothing referenced them in usage reports.
        assert str(spark_env_id) not in unused_ids
        assert str(spark_rev_id) not in unused_ids

        # Instead, they should be surfaced separately as protected, with the reason traceable.
        assert str(spark_env_id) in protected_ids
        assert str(spark_rev_id) in protected_ids

        # The unprotected, unused custom environment should still be flagged.
        assert str(user_env_id) in unused_ids
        assert str(user_rev_id) in unused_ids


class TestGenerateReportProtectedVisibility:
    """Tests that generate_report() surfaces protected tags visibly rather than omitting them."""

    def test_protected_tags_are_flagged_and_excluded_from_deletable_totals(self, mocker, mock_finder_deps):
        from scripts.delete_unused_environments import UnusedEnvInfo, UnusedEnvironmentsFinder

        mock_service = MagicMock()
        mock_service.load_mongodb_usage_reports.return_value = {}
        mock_service.extract_docker_tags_with_usage_info.return_value = ({}, {})
        mock_service.generate_usage_summary.return_value = "no usage"
        mocker.patch("scripts.delete_unused_environments.ImageUsageService", return_value=mock_service)

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        spark_env_id = str(ObjectId())
        custom_env_id = str(ObjectId())

        unused_envs = [UnusedEnvInfo(object_id=custom_env_id, env_name="My Custom Environment")]
        unused_tags = [
            UnusedEnvInfo(
                object_id=custom_env_id,
                env_name="My Custom Environment",
                image_type="environment",
                tag=f"{custom_env_id}-1",
                full_image=f"registry:5000/repo/environment:{custom_env_id}-1",
                size_bytes=100,
            )
        ]
        protected_tags = [
            UnusedEnvInfo(
                object_id=spark_env_id,
                env_name="Spark Compute Environment",
                image_type="environment",
                tag=f"{spark_env_id}-1",
                full_image=f"registry:5000/repo/environment:{spark_env_id}-1",
                size_bytes=500,
            )
        ]

        report = finder.generate_report(unused_envs, unused_tags, freed_space_bytes=100, protected_tags=protected_tags)

        # The protected tag shows up, clearly flagged with a reason — not silently absent.
        protected_entry = report["grouped_by_object_id"][spark_env_id][0]
        assert protected_entry["protected"] is True
        assert protected_entry["status"] == "protected"
        assert protected_entry["protected_reason"]

        # Deletable totals must not count the protected tag.
        assert report["summary"]["total_matching_tags"] == 1
        assert report["summary"]["protected_matching_tags"] == 1
        assert report["summary"]["protected_environment_count"] == 1

        # The genuinely-unused custom environment tag is untouched and correctly unmarked.
        custom_entry = report["grouped_by_object_id"][custom_env_id][0]
        assert custom_entry["protected"] is False

    def test_no_protected_tags_reports_zero(self, mocker, mock_finder_deps):
        """Backward-compatible: omitting protected_tags entirely still produces valid summary counts."""
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        mock_service = MagicMock()
        mock_service.load_mongodb_usage_reports.return_value = {}
        mock_service.extract_docker_tags_with_usage_info.return_value = ({}, {})
        mocker.patch("scripts.delete_unused_environments.ImageUsageService", return_value=mock_service)

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")
        report = finder.generate_report([], [], freed_space_bytes=0)

        assert report["summary"]["protected_matching_tags"] == 0
        assert report["summary"]["protected_environment_count"] == 0


class TestLoadUnusedTagsFromFileSafetyNet:
    """Tests that a saved/edited report can never be replayed to delete a protected environment."""

    def test_protected_entries_are_never_loaded_for_deletion(self, mock_finder_deps, tmp_path):
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        report = {
            "grouped_by_object_id": {
                "env1": [
                    {
                        "object_id": "env1",
                        "env_name": "Spark Compute Environment",
                        "image_type": "environment",
                        "tag": "env1-1",
                        "full_image": "registry:5000/repo/environment:env1-1",
                        "size_bytes": 500,
                        "protected": True,
                    }
                ],
                "env2": [
                    {
                        "object_id": "env2",
                        "env_name": "My Custom Environment",
                        "image_type": "environment",
                        "tag": "env2-1",
                        "full_image": "registry:5000/repo/environment:env2-1",
                        "size_bytes": 100,
                        "protected": False,
                    }
                ],
            }
        }
        report_path = tmp_path / "report.json"
        report_path.write_text(json.dumps(report))

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")
        loaded = finder.load_unused_tags_from_file(str(report_path))

        loaded_ids = {t.object_id for t in loaded}
        assert "env1" not in loaded_ids  # protected entry must never be loaded for deletion
        assert "env2" in loaded_ids


class TestGenerateUsageSummaryHonesty:
    """Tests that a genuinely-unused tag never gets reported as 'referenced, source unknown'.

    ImageUsageService.generate_usage_summary() (the shared helper) assumes "no reasons found"
    still means "in use, source unknown" — correct for its real-time in-use-check callers, but
    misleading when reporting on a tag whose usage has already been independently evaluated as
    zero. UnusedEnvironmentsFinder._generate_usage_summary() exists specifically to give an
    honest answer in that situation, mirroring the fix already applied in delete_image.py and
    delete_archived_tags.py.
    """

    def test_no_usage_reports_honestly_instead_of_referenced_in_system(self, mock_finder_deps):
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        usage = {"runs": [], "workspaces": [], "models": [], "scheduler_jobs": [], "projects": []}
        summary = finder._generate_usage_summary(usage)

        assert "Referenced in system" not in summary
        assert "No usage found" in summary

    def test_no_usage_data_available_reports_honestly(self, mock_finder_deps):
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        summary = finder._generate_usage_summary({})

        assert "Referenced in system" not in summary
        assert "No usage found" in summary

    def test_real_usage_is_still_reported(self, mock_finder_deps):
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        usage = {"runs": [{"id": "1"}, {"id": "2"}], "workspaces": [], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "2 executions in MongoDB" in summary

    def test_generate_report_never_emits_referenced_in_system_for_zero_usage(self, mocker, mock_finder_deps):
        """End-to-end: a genuinely-unused tag's report entry must not say "Referenced in system"."""
        from scripts.delete_unused_environments import UnusedEnvInfo, UnusedEnvironmentsFinder

        mock_service = MagicMock()
        mock_service.load_mongodb_usage_reports.return_value = {}
        # No usage_info entry for this tag at all -> raw_usage falls back to the all-empty default.
        mock_service.extract_docker_tags_with_usage_info.return_value = ({}, {})
        mocker.patch("scripts.delete_unused_environments.ImageUsageService", return_value=mock_service)

        finder = UnusedEnvironmentsFinder(registry_url="registry:5000", repository="repo")

        custom_env_id = str(ObjectId())
        unused_envs = [UnusedEnvInfo(object_id=custom_env_id, env_name="My Custom Environment")]
        unused_tags = [
            UnusedEnvInfo(
                object_id=custom_env_id,
                env_name="My Custom Environment",
                image_type="environment",
                tag=f"{custom_env_id}-1",
                full_image=f"registry:5000/repo/environment:{custom_env_id}-1",
                size_bytes=100,
            )
        ]

        report = finder.generate_report(unused_envs, unused_tags, freed_space_bytes=0)

        entry = report["grouped_by_object_id"][custom_env_id][0]
        assert entry["status"] == "unused"
        assert "Referenced in system" not in entry["usage_summary"]
