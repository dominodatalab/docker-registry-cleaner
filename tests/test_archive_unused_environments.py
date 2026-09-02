"""
Tests for find_unused_environment_docs() in archive_unused_environments.py.

Regression coverage for a crash reported against this script: it captured
UnusedEnvironmentsFinder.find_unused_environments()'s return value (a 2-tuple
of (unused_env_list, protected_env_list) — see its docstring in
delete_unused_environments.py) into a single variable instead of unpacking it,
so the very next line's dict-comprehension iterated the 2-tuple itself,
handing each of its two *list* elements to `env.object_id` and raising
`AttributeError: 'list' object has no attribute 'object_id'`.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_finder_deps(mocker):
    """Mock UnusedEnvironmentsFinder's own construction dependencies (same
    pattern as tests/test_unused_environments.py) plus the config_manager/
    get_mongo_client names archive_unused_environments.py itself calls."""
    mock_config = mocker.patch("utils.deletion_base.config_manager")
    mock_config.get_registry_url.return_value = "registry:5000"
    mock_config.get_repository.return_value = "dominodatalab"
    mock_config.get_domino_platform_namespace.return_value = "domino-platform"
    mock_config.get_mongo_db.return_value = "domino"
    mock_config.get_max_workers.return_value = 4

    mocker.patch("utils.deletion_base.SkopeoClient")
    mocker.patch("utils.deletion_base.HealthChecker")
    mocker.patch("utils.deletion_base.CheckpointManager")

    mock_archive_config = mocker.patch("scripts.archive_unused_environments.config_manager")
    mock_archive_config.get_registry_url.return_value = "registry:5000"
    mock_archive_config.get_repository.return_value = "dominodatalab"
    mock_archive_config.get_mongo_db.return_value = "domino"
    mock_archive_config.get_mongodb_usage_path.return_value = "/tmp/does-not-matter.json"

    return mock_config, mock_archive_config


@pytest.fixture
def mock_reports_exist(mocker):
    """Short-circuit the "generate reports if missing" branch so tests don't
    need a real mongodb-usage-report file on disk."""
    mocker.patch("scripts.archive_unused_environments.Path").return_value.exists.return_value = True


def _mock_mongo_client(mocker, docs):
    """Patch get_mongo_client() (as imported into archive_unused_environments)
    to return a client whose environments_v2.find() yields the given docs."""
    mock_envs_collection = MagicMock()
    mock_envs_collection.find.return_value = docs
    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: {"environments_v2": mock_envs_collection}[name]
    mock_client = MagicMock()
    mock_client.__getitem__.side_effect = lambda name: mock_db
    mocker.patch("scripts.archive_unused_environments.get_mongo_client", return_value=mock_client)
    return mock_client


class TestFindUnusedEnvironmentDocs:
    def test_unpacks_the_tuple_without_crashing(self, mocker, mock_finder_deps, mock_reports_exist):
        """The exact reported crash: find_unused_environments() returns
        (unused_env_list, protected_env_list), not a flat list."""
        from scripts.archive_unused_environments import find_unused_environment_docs
        from scripts.delete_unused_environments import UnusedEnvInfo, UnusedEnvironmentsFinder

        unused = [UnusedEnvInfo(object_id="507f1f77bcf86cd799439011", env_name="env-a")]
        protected = [UnusedEnvInfo(object_id="507f1f77bcf86cd799439099", env_name="system-env")]
        mocker.patch.object(UnusedEnvironmentsFinder, "find_unused_environments", return_value=(unused, protected))
        mocker.patch.object(UnusedEnvironmentsFinder, "generate_required_reports")

        _mock_mongo_client(
            mocker,
            docs=[{"_id": "507f1f77bcf86cd799439011", "name": "env-a", "isArchived": False}],
        )

        finder, result = find_unused_environment_docs(recent_days=None, generate_reports=False)

        assert [e.object_id for e in result] == ["507f1f77bcf86cd799439011"]
        assert finder is not None

    def test_excludes_already_archived_environments(self, mocker, mock_finder_deps, mock_reports_exist):
        from scripts.archive_unused_environments import find_unused_environment_docs
        from scripts.delete_unused_environments import UnusedEnvInfo, UnusedEnvironmentsFinder

        unused = [
            UnusedEnvInfo(object_id="507f1f77bcf86cd799439011", env_name="env-a"),
            UnusedEnvInfo(object_id="507f1f77bcf86cd799439022", env_name="env-b"),
        ]
        mocker.patch.object(UnusedEnvironmentsFinder, "find_unused_environments", return_value=(unused, []))
        mocker.patch.object(UnusedEnvironmentsFinder, "generate_required_reports")

        _mock_mongo_client(
            mocker,
            docs=[
                {"_id": "507f1f77bcf86cd799439011", "name": "env-a", "isArchived": False},
                {"_id": "507f1f77bcf86cd799439022", "name": "env-b", "isArchived": True},
            ],
        )

        _, result = find_unused_environment_docs(recent_days=None, generate_reports=False)

        assert [e.object_id for e in result] == ["507f1f77bcf86cd799439011"]

    def test_returns_empty_when_nothing_unused(self, mocker, mock_finder_deps, mock_reports_exist):
        """Also a regression case: the tuple-truthiness bug this fixes meant
        `if not unused_envs` (checking a non-empty 2-tuple) never triggered
        even when both find_unused_environments() lists were empty."""
        from scripts.archive_unused_environments import find_unused_environment_docs
        from scripts.delete_unused_environments import UnusedEnvironmentsFinder

        mocker.patch.object(UnusedEnvironmentsFinder, "find_unused_environments", return_value=([], []))
        mocker.patch.object(UnusedEnvironmentsFinder, "generate_required_reports")
        mock_get_client = mocker.patch("scripts.archive_unused_environments.get_mongo_client")

        finder, result = find_unused_environment_docs(recent_days=None, generate_reports=False)

        assert result == []
        assert finder is not None
        mock_get_client.assert_not_called()  # short-circuits before ever touching Mongo again

    def test_protected_environments_are_not_archived_but_are_counted(
        self, mocker, mock_finder_deps, mock_reports_exist, caplog
    ):
        """protected_env_list is surfaced (logged) but never treated as a
        candidate to archive — it's a disjoint list from unused_env_list."""
        from scripts.archive_unused_environments import find_unused_environment_docs
        from scripts.delete_unused_environments import UnusedEnvInfo, UnusedEnvironmentsFinder

        unused = [UnusedEnvInfo(object_id="507f1f77bcf86cd799439011", env_name="env-a")]
        protected = [
            UnusedEnvInfo(object_id="507f1f77bcf86cd799439098", env_name="system-env-1"),
            UnusedEnvInfo(object_id="507f1f77bcf86cd799439099", env_name="system-env-2"),
        ]
        mocker.patch.object(UnusedEnvironmentsFinder, "find_unused_environments", return_value=(unused, protected))
        mocker.patch.object(UnusedEnvironmentsFinder, "generate_required_reports")

        _mock_mongo_client(
            mocker,
            docs=[{"_id": "507f1f77bcf86cd799439011", "name": "env-a", "isArchived": False}],
        )

        with caplog.at_level("INFO"):
            _, result = find_unused_environment_docs(recent_days=None, generate_reports=False)

        assert [e.object_id for e in result] == ["507f1f77bcf86cd799439011"]
        assert any("2 Domino-shipped system environments" in message for message in caplog.messages)
