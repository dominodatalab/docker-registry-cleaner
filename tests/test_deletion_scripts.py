"""
Tests for deletion scripts and base deletion functionality.

These tests verify:
- BaseDeletionScript initialization and common methods
- ArchivedTagsFinder from delete_archived_tags.py
- Confirmation prompts and deletion workflows
- Health check integration
"""

from unittest.mock import MagicMock, Mock

import pytest
from bson import ObjectId

# ============================================================================
# Fixtures
# ============================================================================


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
def mock_skopeo_client(mocker):
    """Fixture providing mocked SkopeoClient."""
    mock_class = mocker.patch("utils.deletion_base.SkopeoClient")
    mock_instance = MagicMock()
    mock_class.return_value = mock_instance
    mock_instance.enable_registry_deletion.return_value = True
    mock_instance.disable_registry_deletion.return_value = True
    return mock_instance


@pytest.fixture
def mock_health_checker(mocker):
    """Fixture providing mocked HealthChecker."""
    mock_class = mocker.patch("utils.deletion_base.HealthChecker")
    mock_instance = MagicMock()
    mock_class.return_value = mock_instance
    return mock_instance


@pytest.fixture
def mock_checkpoint_manager(mocker):
    """Fixture providing mocked CheckpointManager."""
    mock_class = mocker.patch("utils.deletion_base.CheckpointManager")
    mock_instance = MagicMock()
    mock_class.return_value = mock_instance
    return mock_instance


# ============================================================================
# Tests: BaseDeletionScript
# ============================================================================


class TestBaseDeletionScript:
    """Tests for the BaseDeletionScript base class."""

    def test_initialization_with_defaults(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test BaseDeletionScript initializes with config defaults."""
        from utils.deletion_base import BaseDeletionScript

        # Create a concrete subclass since BaseDeletionScript is abstract
        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        script = ConcreteDeletionScript()

        assert script.registry_url == "docker-registry.domino-platform.svc.cluster.local:5000"
        assert script.repository == "dominodatalab"
        assert script.namespace == "domino-platform"

    def test_initialization_with_overrides(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test BaseDeletionScript accepts parameter overrides."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        script = ConcreteDeletionScript(
            registry_url="custom-registry:5000", repository="custom-repo", namespace="custom-namespace"
        )

        assert script.registry_url == "custom-registry:5000"
        assert script.repository == "custom-repo"
        assert script.namespace == "custom-namespace"

    def test_confirm_deletion_force_mode(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test confirm_deletion returns True when force=True."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        script = ConcreteDeletionScript()
        result = script.confirm_deletion(count=10, item_type="images", force=True)

        assert result is True

    def test_confirm_deletion_user_accepts(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test confirm_deletion returns True when user types 'yes'."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        mocker.patch("builtins.input", return_value="yes")

        script = ConcreteDeletionScript()
        result = script.confirm_deletion(count=10, item_type="images", force=False)

        assert result is True

    def test_confirm_deletion_user_rejects(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test confirm_deletion returns False when user types 'no'."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        mocker.patch("builtins.input", return_value="no")

        script = ConcreteDeletionScript()
        result = script.confirm_deletion(count=10, item_type="images", force=False)

        assert result is False

    def test_enable_registry_deletion_success(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test enable_registry_deletion returns True on success."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        script = ConcreteDeletionScript()
        result = script.enable_registry_deletion()

        assert result is True
        mock_skopeo_client.enable_registry_deletion.assert_called_once()

    def test_disable_registry_deletion_success(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test disable_registry_deletion returns True on success."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        script = ConcreteDeletionScript()
        result = script.disable_registry_deletion()

        assert result is True
        mock_skopeo_client.disable_registry_deletion.assert_called_once()

    def test_run_health_checks_all_pass(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test run_health_checks returns True when all required checks pass."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        # Mock health check results - all pass
        mock_health_checker.run_all_checks.return_value = [
            Mock(name="configuration", status=True),
            Mock(name="registry_connectivity", status=True),
            Mock(name="mongodb_connectivity", status=True),
        ]

        script = ConcreteDeletionScript()
        result = script.run_health_checks()

        assert result is True

    def test_run_health_checks_required_fails(
        self, mocker, mock_config_manager, mock_skopeo_client, mock_health_checker, mock_checkpoint_manager
    ):
        """Test run_health_checks returns False when a required check fails."""
        from utils.deletion_base import BaseDeletionScript

        class ConcreteDeletionScript(BaseDeletionScript):
            pass

        # Mock health check results - registry fails
        mock_health_checker.run_all_checks.return_value = [
            Mock(name="configuration", status=True),
            Mock(name="registry_connectivity", status=False),  # Failed
            Mock(name="mongodb_connectivity", status=True),
        ]

        script = ConcreteDeletionScript()
        result = script.run_health_checks()

        assert result is False


# ============================================================================
# Tests: ArchivedTagsFinder
# ============================================================================


class TestArchivedTagsFinder:
    """Tests for the ArchivedTagsFinder class from delete_archived_tags.py."""

    @pytest.fixture
    def mock_archived_tags_deps(self, mocker, mock_config_manager):
        """Set up mocks for ArchivedTagsFinder dependencies."""
        # Patch at the script level where they're imported
        mocker.patch("scripts.delete_archived_tags.config_manager", mock_config_manager)
        mocker.patch("scripts.delete_archived_tags.ImageAnalyzer")
        mocker.patch("scripts.delete_archived_tags.ImageUsageService")

        # Patch at deletion_base level (HealthChecker, CheckpointManager, SkopeoClient are only imported here)
        mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
        mocker.patch("utils.deletion_base.SkopeoClient")
        mocker.patch("utils.deletion_base.HealthChecker")
        mocker.patch("utils.deletion_base.CheckpointManager")

        return mock_config_manager

    def test_initialization_requires_type(self, mock_archived_tags_deps):
        """Test ArchivedTagsFinder requires at least one of --environment or --model."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        with pytest.raises(ValueError, match="Must specify at least one of --environment or --model"):
            ArchivedTagsFinder(
                registry_url="registry:5000", repository="repo", process_environments=False, process_models=False
            )

    def test_initialization_with_environment(self, mock_archived_tags_deps):
        """Test ArchivedTagsFinder initializes with environment processing."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(
            registry_url="registry:5000", repository="repo", process_environments=True, process_models=False
        )

        assert finder.process_environments is True
        assert finder.process_models is False
        assert finder.image_types == ["environment"]

    def test_initialization_with_model(self, mock_archived_tags_deps):
        """Test ArchivedTagsFinder initializes with model processing."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(
            registry_url="registry:5000", repository="repo", process_environments=False, process_models=True
        )

        assert finder.process_environments is False
        assert finder.process_models is True
        assert finder.image_types == ["model"]

    def test_initialization_with_both(self, mock_archived_tags_deps):
        """Test ArchivedTagsFinder initializes with both environment and model processing."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(
            registry_url="registry:5000", repository="repo", process_environments=True, process_models=True
        )

        assert finder.process_environments is True
        assert finder.process_models is True
        assert finder.image_types == ["environment", "model"]

    def test_generate_usage_summary_with_runs(self, mock_archived_tags_deps):
        """Test _generate_usage_summary handles runs correctly."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        usage = {"runs": [{"id": "1"}, {"id": "2"}], "workspaces": [], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "2 executions in MongoDB" in summary

    def test_generate_usage_summary_with_workspaces(self, mock_archived_tags_deps):
        """Test _generate_usage_summary handles workspaces correctly."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        usage = {"runs": [], "workspaces": [{"id": "1"}], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "1 workspace" in summary

    def test_generate_usage_summary_with_count_fields(self, mock_archived_tags_deps):
        """Test _generate_usage_summary prefers count fields over list length."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        # count field should take precedence over list
        usage = {"runs": [{"id": "1"}], "runs_count": 5, "workspaces": [], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "5 executions in MongoDB" in summary

    def test_generate_usage_summary_no_usage(self, mock_archived_tags_deps):
        """Test _generate_usage_summary handles no usage."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        usage = {"runs": [], "workspaces": [], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "No usage found" in summary

    def test_fetch_archived_object_ids_environments(self, mocker, mock_archived_tags_deps):
        """Test fetch_archived_object_ids finds archived environments."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        # Mock MongoDB client and collections
        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_archived_tags.get_mongo_client", return_value=mock_mongo_client)

        mock_db = MagicMock()
        mock_mongo_client.__getitem__.return_value = mock_db

        # Mock environments_v2 collection
        env_id = ObjectId()
        mock_db["environments_v2"].find.return_value = [{"_id": env_id}]

        # Mock environment_revisions collection
        rev_id = ObjectId()
        mock_db["environment_revisions"].find.return_value = [{"_id": rev_id, "environmentId": env_id}]

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        archived_ids, id_to_type, env_to_revs, model_to_vers, model_tag_to_ver = finder.fetch_archived_object_ids()

        assert str(env_id) in archived_ids
        assert str(rev_id) in archived_ids
        assert id_to_type[str(env_id)] == "environment"
        assert id_to_type[str(rev_id)] == "revision"

    def test_fetch_archived_object_ids_models(self, mocker, mock_archived_tags_deps):
        """Test fetch_archived_object_ids finds archived models."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        # Mock MongoDB client and collections
        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_archived_tags.get_mongo_client", return_value=mock_mongo_client)

        mock_db = MagicMock()
        mock_mongo_client.__getitem__.return_value = mock_db

        # Mock models collection
        model_id = ObjectId()
        mock_db["models"].find.return_value = [{"_id": model_id}]

        # Mock model_versions collection
        version_id = ObjectId()
        mock_db["model_versions"].find.return_value = [
            {"_id": version_id, "modelId": {"value": model_id}, "metadata": {}}
        ]

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_models=True)

        archived_ids, id_to_type, env_to_revs, model_to_vers, model_tag_to_ver = finder.fetch_archived_object_ids()

        assert str(model_id) in archived_ids
        assert str(version_id) in archived_ids
        assert id_to_type[str(model_id)] == "model"
        assert id_to_type[str(version_id)] == "version"


# ============================================================================
# Tests: Filter cloned dependencies
# ============================================================================


class TestFilterClonedDependencies:
    """Tests for the _filter_cloned_dependencies logic."""

    @pytest.fixture
    def mock_archived_tags_deps(self, mocker, mock_config_manager):
        """Set up mocks for ArchivedTagsFinder dependencies."""
        mocker.patch("scripts.delete_archived_tags.config_manager", mock_config_manager)
        mocker.patch("scripts.delete_archived_tags.ImageAnalyzer")
        mocker.patch("scripts.delete_archived_tags.ImageUsageService")

        mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
        mocker.patch("utils.deletion_base.SkopeoClient")
        mocker.patch("utils.deletion_base.HealthChecker")
        mocker.patch("utils.deletion_base.CheckpointManager")

        return mock_config_manager

    def test_filter_cloned_no_clones(self, mocker, mock_archived_tags_deps):
        """Test filter returns unchanged when no cloned revisions exist."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        archived_ids = ["id1", "id2", "id3"]
        id_to_type_map = {"id1": "environment", "id2": "revision", "id3": "revision"}
        revision_to_cloned = {}  # No cloned revisions
        revision_to_environment = {"id2": "id1", "id3": "id1"}

        filtered_ids, filtered_map = finder._filter_cloned_dependencies(
            archived_ids, id_to_type_map, revision_to_cloned, revision_to_environment
        )

        assert filtered_ids == archived_ids
        assert filtered_map == id_to_type_map

    def test_filter_cloned_dependency_in_set(self, mocker, mock_archived_tags_deps):
        """Test filter keeps revisions when cloned dependency is in deletion set."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        # Mock MongoDB for checking cloned revision chain
        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_archived_tags.get_mongo_client", return_value=mock_mongo_client)

        mock_db = MagicMock()
        mock_mongo_client.__getitem__.return_value = mock_db

        # Cloned revision exists and its environment is in the set
        cloned_rev_id = ObjectId()
        cloned_env_id = ObjectId()
        mock_db["environment_revisions"].find_one.return_value = {"_id": cloned_rev_id, "environmentId": cloned_env_id}

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        env_id = str(ObjectId())
        rev_id = str(ObjectId())
        archived_ids = [env_id, rev_id, str(cloned_rev_id), str(cloned_env_id)]
        id_to_type_map = {
            env_id: "environment",
            rev_id: "revision",
            str(cloned_rev_id): "revision",
            str(cloned_env_id): "environment",
        }
        revision_to_cloned = {rev_id: str(cloned_rev_id)}
        revision_to_environment = {rev_id: env_id, str(cloned_rev_id): str(cloned_env_id)}

        filtered_ids, filtered_map = finder._filter_cloned_dependencies(
            archived_ids, id_to_type_map, revision_to_cloned, revision_to_environment
        )

        # All IDs should be kept since cloned dependency is in set
        assert len(filtered_ids) == 4


# ============================================================================
# Tests: In-use environment detection
# ============================================================================


class TestInUseEnvironmentDetection:
    """Tests for get_in_use_environment_ids method."""

    @pytest.fixture
    def mock_archived_tags_deps(self, mocker, mock_config_manager):
        """Set up mocks for ArchivedTagsFinder dependencies."""
        mocker.patch("scripts.delete_archived_tags.config_manager", mock_config_manager)
        mocker.patch("scripts.delete_archived_tags.ImageAnalyzer")
        mocker.patch("scripts.delete_archived_tags.ImageUsageService")

        mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
        mocker.patch("utils.deletion_base.SkopeoClient")
        mocker.patch("utils.deletion_base.HealthChecker")
        mocker.patch("utils.deletion_base.CheckpointManager")

        return mock_config_manager

    def test_empty_input_returns_empty(self, mock_archived_tags_deps):
        """Test get_in_use_environment_ids returns empty for empty input."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        result = finder.get_in_use_environment_ids([], [])

        assert result == {}

    def test_finds_workspace_references(self, mocker, mock_archived_tags_deps):
        """Test get_in_use_environment_ids finds workspace references."""
        from scripts.delete_archived_tags import ArchivedTagsFinder

        # Mock MongoDB client
        mock_mongo_client = MagicMock()
        mocker.patch("scripts.delete_archived_tags.get_mongo_client", return_value=mock_mongo_client)

        mock_db = MagicMock()
        mock_mongo_client.__getitem__.return_value = mock_db

        env_id = ObjectId()

        # Mock workspace collection finding a reference
        mock_db["workspace"].find.return_value = [{"_id": ObjectId(), "configTemplate": {"environmentId": env_id}}]
        mock_db["workspace_session"].find.return_value = []

        finder = ArchivedTagsFinder(registry_url="registry:5000", repository="repo", process_environments=True)

        result = finder.get_in_use_environment_ids([str(env_id)], [])

        assert str(env_id) in result
        assert result[str(env_id)] is True
