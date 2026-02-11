"""Unit tests for scripts/migrate_registry.py and SkopeoClient.copy_image"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))


@pytest.fixture(autouse=True)
def patch_environment():
    """Patch environment for all tests"""
    with patch.dict(os.environ, {"SKIP_CONFIG_VALIDATION": "true"}):
        yield


# ---------------------------------------------------------------------------
# SkopeoClient.copy_image tests
# ---------------------------------------------------------------------------


class TestCopyImage:
    """Tests for SkopeoClient.copy_image method"""

    @pytest.fixture
    def skopeo_client(self):
        """Create a SkopeoClient with mocked dependencies"""
        from utils.skopeo_client import SkopeoClient

        mock_config = MagicMock()
        mock_config.get_registry_url.return_value = "registry.example.com:5000"
        mock_config.get_repository.return_value = "myrepo"
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_output_dir.return_value = "/tmp/output"
        mock_config.get_skopeo_rate_limit_enabled.return_value = False
        mock_config.get_max_retries.return_value = 0
        mock_config.get_retry_initial_delay.return_value = 0.1
        mock_config.get_retry_max_delay.return_value = 1.0
        mock_config.get_retry_exponential_base.return_value = 2.0
        mock_config.get_retry_jitter.return_value = False
        mock_config.get_retry_timeout.return_value = 300

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                with patch.object(SkopeoClient, "_login_to_registry"):
                    client = SkopeoClient(mock_config)
                    client._logged_in = True
                    client.auth_file = "/tmp/output/.registry-auth.json"
                    return client

    def test_copy_image_success(self, skopeo_client):
        """Test successful image copy"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = skopeo_client.copy_image(
                src_ref="docker://registry.example.com:5000/repo:tag1",
                dest_ref="docker://ecr.example.com/repo:tag1",
                dest_creds="user:pass",
            )
            assert result is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "skopeo"
            assert cmd[1] == "copy"
            assert "--src-tls-verify=false" in cmd
            assert "--dest-tls-verify=false" in cmd
            assert "--dest-creds" in cmd

    def test_copy_image_with_token_auth(self, skopeo_client):
        """Test image copy with destination token authentication"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = skopeo_client.copy_image(
                src_ref="docker://registry.example.com:5000/repo:tag1",
                dest_ref="docker://gcr.io/project/repo:tag1",
                dest_registry_token="my-gcloud-token",
            )
            assert result is True
            cmd = mock_run.call_args[0][0]
            assert "--dest-registry-token" in cmd
            idx = cmd.index("--dest-registry-token")
            assert cmd[idx + 1] == "my-gcloud-token"

    def test_copy_image_with_dest_tls_verify(self, skopeo_client):
        """Test image copy with TLS verification for destination"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            skopeo_client.copy_image(
                src_ref="docker://src/repo:tag",
                dest_ref="docker://dest/repo:tag",
                dest_tls_verify=True,
            )
            cmd = mock_run.call_args[0][0]
            assert "--dest-tls-verify=true" in cmd

    def test_copy_image_uses_src_authfile(self, skopeo_client):
        """Test that copy uses the source auth file"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            skopeo_client.copy_image(
                src_ref="docker://src/repo:tag",
                dest_ref="docker://dest/repo:tag",
            )
            cmd = mock_run.call_args[0][0]
            assert "--src-authfile" in cmd
            idx = cmd.index("--src-authfile")
            assert cmd[idx + 1] == "/tmp/output/.registry-auth.json"

    def test_copy_image_failure(self, skopeo_client):
        """Test image copy failure returns False"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "skopeo", stderr="connection refused")
            result = skopeo_client.copy_image(
                src_ref="docker://src/repo:tag",
                dest_ref="docker://dest/repo:tag",
            )
            assert result is False

    def test_copy_image_timeout(self, skopeo_client):
        """Test image copy timeout returns False"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("skopeo", 300)
            result = skopeo_client.copy_image(
                src_ref="docker://src/repo:tag",
                dest_ref="docker://dest/repo:tag",
            )
            assert result is False

    def test_copy_image_redacts_credentials_in_logs(self, skopeo_client):
        """Test that dest-creds are redacted when logging"""
        from utils.skopeo_client import SkopeoClient

        cmd = [
            "skopeo",
            "copy",
            "--dest-creds",
            "user:secretpassword",
            "docker://src/repo:tag",
            "docker://dest/repo:tag",
        ]
        redacted = SkopeoClient._redact_command_for_logging(cmd)
        assert "secretpassword" not in " ".join(redacted)
        assert "user:****" in redacted[3]


# ---------------------------------------------------------------------------
# RegistryMigrator tests
# ---------------------------------------------------------------------------


class TestRegistryMigrator:
    """Tests for RegistryMigrator class"""

    @pytest.fixture
    def migrator(self):
        """Create a RegistryMigrator with mocked dependencies"""
        with patch("utils.deletion_base.SkopeoClient") as MockSkopeo:
            with patch("utils.deletion_base.HealthChecker"):
                mock_skopeo = MagicMock()
                MockSkopeo.return_value = mock_skopeo

                from scripts.migrate_registry import RegistryMigrator

                m = RegistryMigrator(
                    registry_url="source-registry:5000",
                    repository="dominodatalab",
                    dest_registry_url="ecr.example.com/my-repo",
                    dest_creds="user:pass",
                )
                return m

    def test_discover_repos_with_filter(self, migrator):
        """Test discover_repositories with explicit repo list"""
        migrator.skopeo_client.list_tags.side_effect = [
            ["tag1", "tag2"],
            ["tag3"],
        ]

        result = migrator.discover_repositories(filter_repos=["repo-a", "repo-b"])

        assert "repo-a" in result
        assert "repo-b" in result
        assert result["repo-a"] == ["tag1", "tag2"]
        assert result["repo-b"] == ["tag3"]

    def test_discover_repos_skips_empty(self, migrator):
        """Test discover_repositories skips repos with no tags"""
        migrator.skopeo_client.list_tags.side_effect = [
            ["tag1"],
            [],  # empty repo
        ]

        result = migrator.discover_repositories(filter_repos=["repo-a", "repo-b"])

        assert "repo-a" in result
        assert "repo-b" not in result

    def test_copy_repo_tags_dry_run(self, migrator):
        """Test copy_repo_tags in dry-run mode doesn't call skopeo"""
        results = migrator.copy_repo_tags("repo-a", ["tag1", "tag2"], dry_run=True)

        assert results["copied"] == 2
        assert results["failed"] == 0
        migrator.skopeo_client.copy_image.assert_not_called()

    def test_copy_repo_tags_apply(self, migrator):
        """Test copy_repo_tags in apply mode calls copy_image"""
        migrator.skopeo_client.copy_image.return_value = True

        results = migrator.copy_repo_tags("repo-a", ["tag1", "tag2"], dry_run=False)

        assert results["copied"] == 2
        assert results["failed"] == 0
        assert migrator.skopeo_client.copy_image.call_count == 2

    def test_copy_repo_tags_partial_failure(self, migrator):
        """Test copy_repo_tags tracks failures"""
        migrator.skopeo_client.copy_image.side_effect = [True, False, True]

        results = migrator.copy_repo_tags("repo-a", ["tag1", "tag2", "tag3"], dry_run=False)

        assert results["copied"] == 2
        assert results["failed"] == 1

    def test_copy_repo_tags_builds_correct_refs(self, migrator):
        """Test that source and dest refs are built correctly"""
        migrator.skopeo_client.copy_image.return_value = True

        migrator.copy_repo_tags("domino-abc123", ["v1"], dry_run=False)

        migrator.skopeo_client.copy_image.assert_called_once_with(
            src_ref="docker://source-registry:5000/domino-abc123:v1",
            dest_ref="docker://ecr.example.com/my-repo/domino-abc123:v1",
            dest_creds="user:pass",
            dest_registry_token=None,
            dest_tls_verify=False,
        )


# ---------------------------------------------------------------------------
# MongoDB update tests
# ---------------------------------------------------------------------------


class TestMongoDBUpdate:
    """Tests for RegistryMigrator.update_mongodb_metadata"""

    @pytest.fixture
    def migrator_with_mongo(self):
        """Create a RegistryMigrator with mocked MongoDB"""
        with patch("utils.deletion_base.SkopeoClient") as MockSkopeo:
            with patch("utils.deletion_base.HealthChecker"):
                MockSkopeo.return_value = MagicMock()

                from scripts.migrate_registry import RegistryMigrator

                m = RegistryMigrator(
                    registry_url="source-registry:5000",
                    repository="dominodatalab",
                    dest_registry_url="ecr.example.com/my-repo",
                )
                return m

    def test_replace_prefix_standard(self):
        """Test _replace_prefix with standard Domino patterns"""
        from scripts.migrate_registry import RegistryMigrator

        # Environment pattern
        result = RegistryMigrator._replace_prefix("dominodatalab/environment", "dominodatalab", "my-ecr/dominodatalab")
        assert result == "my-ecr/dominodatalab/dominodatalab/environment"

        # Model pattern
        result = RegistryMigrator._replace_prefix("dominodatalab/model", "dominodatalab", "my-ecr/dominodatalab")
        assert result == "my-ecr/dominodatalab/dominodatalab/model"

    def test_replace_prefix_already_updated(self):
        """Test _replace_prefix is idempotent"""
        from scripts.migrate_registry import RegistryMigrator

        result = RegistryMigrator._replace_prefix(
            "my-ecr/dominodatalab/environment",
            "dominodatalab",
            "my-ecr/dominodatalab",
        )
        assert result == "my-ecr/dominodatalab/environment"

    def test_replace_prefix_domino_hash_pattern(self):
        """Test _replace_prefix with domino-<hash> pattern"""
        from scripts.migrate_registry import RegistryMigrator

        result = RegistryMigrator._replace_prefix("domino-abc123", "dominodatalab", "my-ecr/dominodatalab")
        assert result == "my-ecr/dominodatalab/domino-abc123"

    def test_replace_prefix_dom_mdl_pattern(self):
        """Test _replace_prefix with dom-mdl-<hash> pattern"""
        from scripts.migrate_registry import RegistryMigrator

        result = RegistryMigrator._replace_prefix("dom-mdl-abc123", "dominodatalab", "my-ecr/dominodatalab")
        assert result == "my-ecr/dominodatalab/dom-mdl-abc123"

    def test_get_nested_field(self):
        """Test _get_nested_field utility"""
        from scripts.migrate_registry import RegistryMigrator

        doc = {"image": {"repository": "my-repo"}}
        assert RegistryMigrator._get_nested_field(doc, "image.repository") == "my-repo"

        doc = {"metadata": {"dockerImageName": {"repository": "my-repo"}}}
        assert RegistryMigrator._get_nested_field(doc, "metadata.dockerImageName.repository") == "my-repo"

        # Missing field
        assert RegistryMigrator._get_nested_field(doc, "nonexistent.field") is None

    def test_update_mongodb_dry_run(self, migrator_with_mongo):
        """Test MongoDB update in dry-run mode counts but doesn't modify"""
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_builds = MagicMock()
        mock_builds.count_documents.return_value = 5

        mock_env_revisions = MagicMock()
        mock_env_revisions.count_documents.return_value = 3

        mock_model_versions = MagicMock()
        mock_model_versions.count_documents.return_value = 2

        mock_db.__getitem__ = MagicMock(
            side_effect=lambda name: {
                "builds": mock_builds,
                "environment_revisions": mock_env_revisions,
                "model_versions": mock_model_versions,
            }[name]
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"

                results = migrator_with_mongo.update_mongodb_metadata(
                    old_prefix="dominodatalab",
                    new_prefix="my-ecr/dominodatalab",
                    dry_run=True,
                )

        assert results["builds"]["matched"] == 5
        assert results["builds"]["modified"] == 0
        assert results["environment_revisions"]["matched"] == 3
        assert results["environment_revisions"]["modified"] == 0
        # No actual updates should have been called
        mock_builds.update_one.assert_not_called()
        mock_env_revisions.update_one.assert_not_called()


# ---------------------------------------------------------------------------
# Checkpoint / resume tests
# ---------------------------------------------------------------------------


class TestMigrationCheckpoint:
    """Tests for checkpoint/resume behavior in migration"""

    @pytest.fixture
    def migrator(self):
        with patch("utils.deletion_base.SkopeoClient") as MockSkopeo:
            with patch("utils.deletion_base.HealthChecker"):
                MockSkopeo.return_value = MagicMock()

                from scripts.migrate_registry import RegistryMigrator

                m = RegistryMigrator(
                    registry_url="source-registry:5000",
                    repository="dominodatalab",
                    dest_registry_url="ecr.example.com/my-repo",
                    dest_creds="user:pass",
                )
                return m

    def test_copy_repo_tags_returns_counts_for_checkpointing(self, migrator):
        """Test that copy_repo_tags returns counts that main() uses for checkpointing"""
        migrator.skopeo_client.copy_image.return_value = True

        results = migrator.copy_repo_tags("repo-a", ["tag1", "tag2"], dry_run=False)

        # copy_repo_tags returns counts; the main() function handles saving checkpoints
        assert results["copied"] == 2
        assert results["failed"] == 0
        assert results["skipped"] == 0


# ---------------------------------------------------------------------------
# Unarchived filter tests
# ---------------------------------------------------------------------------


class TestUnarchivedFilter:
    """Tests for --unarchived filtering (get_unarchived_tags, filter_to_unarchived)"""

    @pytest.fixture
    def migrator(self):
        with patch("utils.deletion_base.SkopeoClient") as MockSkopeo:
            with patch("utils.deletion_base.HealthChecker"):
                MockSkopeo.return_value = MagicMock()

                from scripts.migrate_registry import RegistryMigrator

                m = RegistryMigrator(
                    registry_url="source-registry:5000",
                    repository="dominodatalab",
                    dest_registry_url="ecr.example.com/my-repo",
                    dest_creds="user:pass",
                )
                return m

    def _make_mock_db(self, env_docs, env_revision_docs, model_docs, model_version_docs):
        """Helper to build a mock MongoDB database with the given documents."""
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_envs = MagicMock()
        mock_envs.find.return_value = env_docs

        mock_revisions = MagicMock()
        mock_revisions.find.return_value = env_revision_docs

        mock_models = MagicMock()
        mock_models.find.return_value = model_docs

        mock_versions = MagicMock()
        mock_versions.find.return_value = model_version_docs

        mock_db.__getitem__ = MagicMock(
            side_effect=lambda name: {
                "environments_v2": mock_envs,
                "environment_revisions": mock_revisions,
                "models": mock_models,
                "model_versions": mock_versions,
            }[name]
        )

        return mock_client

    def test_get_unarchived_tags_returns_env_and_model_tags(self, migrator):
        """Test that get_unarchived_tags collects tags from both environments and models"""
        mock_client = self._make_mock_db(
            env_docs=[{"_id": "env-1"}, {"_id": "env-2"}],
            env_revision_docs=[
                {"metadata": {"dockerImageName": {"tag": "tag-env-a"}}},
                {"metadata": {"dockerImageName": {"tag": "tag-env-b"}}},
            ],
            model_docs=[{"_id": "model-1"}],
            model_version_docs=[
                {
                    "metadata": {
                        "builds": [
                            {"slug": {"image": {"tag": "tag-model-a"}}},
                        ]
                    }
                },
            ],
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"
                tags = migrator.get_unarchived_tags()

        assert tags == {"tag-env-a", "tag-env-b", "tag-model-a"}

    def test_get_unarchived_tags_empty_when_no_records(self, migrator):
        """Test that get_unarchived_tags returns empty set when nothing exists"""
        mock_client = self._make_mock_db(
            env_docs=[],
            env_revision_docs=[],
            model_docs=[],
            model_version_docs=[],
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"
                tags = migrator.get_unarchived_tags()

        assert tags == set()

    def test_get_unarchived_tags_handles_multiple_builds(self, migrator):
        """Test that model versions with multiple builds have all tags collected"""
        mock_client = self._make_mock_db(
            env_docs=[],
            env_revision_docs=[],
            model_docs=[{"_id": "model-1"}],
            model_version_docs=[
                {
                    "metadata": {
                        "builds": [
                            {"slug": {"image": {"tag": "build-tag-1"}}},
                            {"slug": {"image": {"tag": "build-tag-2"}}},
                        ]
                    }
                },
            ],
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"
                tags = migrator.get_unarchived_tags()

        assert tags == {"build-tag-1", "build-tag-2"}

    def test_filter_to_unarchived_keeps_matching_tags(self, migrator):
        """Test that filter_to_unarchived keeps only tags in the unarchived set"""
        repo_tags = {
            "repo-a": ["tag1", "tag2", "tag3"],
            "repo-b": ["tag4", "tag5"],
        }

        with patch.object(migrator, "_get_tags_by_archive_status", return_value={"tag1", "tag3", "tag4"}):
            result = migrator.filter_to_unarchived(repo_tags)

        assert result == {
            "repo-a": ["tag1", "tag3"],
            "repo-b": ["tag4"],
        }

    def test_filter_to_unarchived_removes_empty_repos(self, migrator):
        """Test that repos with no remaining tags are omitted"""
        repo_tags = {
            "repo-a": ["tag1", "tag2"],
            "repo-b": ["tag3"],
        }

        with patch.object(migrator, "_get_tags_by_archive_status", return_value={"tag1", "tag2"}):
            result = migrator.filter_to_unarchived(repo_tags)

        assert "repo-a" in result
        assert "repo-b" not in result

    def test_filter_to_unarchived_returns_empty_when_all_archived(self, migrator):
        """Test that an empty dict is returned when all tags are archived"""
        repo_tags = {
            "repo-a": ["tag1", "tag2"],
        }

        with patch.object(migrator, "_get_tags_by_archive_status", return_value=set()):
            result = migrator.filter_to_unarchived(repo_tags)

        assert result == {}


# ---------------------------------------------------------------------------
# Archived filter tests
# ---------------------------------------------------------------------------


class TestArchivedFilter:
    """Tests for --archived filtering (get_archived_tags, filter_to_archived)"""

    @pytest.fixture
    def migrator(self):
        with patch("utils.deletion_base.SkopeoClient") as MockSkopeo:
            with patch("utils.deletion_base.HealthChecker"):
                MockSkopeo.return_value = MagicMock()

                from scripts.migrate_registry import RegistryMigrator

                m = RegistryMigrator(
                    registry_url="source-registry:5000",
                    repository="dominodatalab",
                    dest_registry_url="ecr.example.com/my-repo",
                    dest_creds="user:pass",
                )
                return m

    def _make_mock_db(self, env_docs, env_revision_docs, model_docs, model_version_docs):
        """Helper to build a mock MongoDB database with the given documents."""
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_envs = MagicMock()
        mock_envs.find.return_value = env_docs

        mock_revisions = MagicMock()
        mock_revisions.find.return_value = env_revision_docs

        mock_models = MagicMock()
        mock_models.find.return_value = model_docs

        mock_versions = MagicMock()
        mock_versions.find.return_value = model_version_docs

        mock_db.__getitem__ = MagicMock(
            side_effect=lambda name: {
                "environments_v2": mock_envs,
                "environment_revisions": mock_revisions,
                "models": mock_models,
                "model_versions": mock_versions,
            }[name]
        )

        return mock_client

    def test_get_archived_tags_returns_archived_env_and_model_tags(self, migrator):
        """Test that get_archived_tags collects tags from archived environments and models"""
        mock_client = self._make_mock_db(
            env_docs=[{"_id": "env-archived-1"}],
            env_revision_docs=[
                {"metadata": {"dockerImageName": {"tag": "tag-archived-env"}}},
            ],
            model_docs=[{"_id": "model-archived-1"}],
            model_version_docs=[
                {
                    "metadata": {
                        "builds": [
                            {"slug": {"image": {"tag": "tag-archived-model"}}},
                        ]
                    }
                },
            ],
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"
                tags = migrator.get_archived_tags()

        assert tags == {"tag-archived-env", "tag-archived-model"}

        # Verify the MongoDB query used isArchived: True
        mock_db = mock_client.__getitem__.return_value
        envs_collection = mock_db.__getitem__("environments_v2")
        envs_collection.find.assert_called_once_with({"isArchived": True}, {"_id": 1})

    def test_get_archived_tags_empty_when_nothing_archived(self, migrator):
        """Test that get_archived_tags returns empty set when nothing is archived"""
        mock_client = self._make_mock_db(
            env_docs=[],
            env_revision_docs=[],
            model_docs=[],
            model_version_docs=[],
        )

        with patch("scripts.migrate_registry.get_mongo_client", return_value=mock_client):
            with patch("scripts.migrate_registry.config_manager") as mock_config:
                mock_config.get_mongo_db.return_value = "domino"
                tags = migrator.get_archived_tags()

        assert tags == set()

    def test_filter_to_archived_keeps_only_archived_tags(self, migrator):
        """Test that filter_to_archived keeps only tags belonging to archived records"""
        repo_tags = {
            "repo-a": ["active-tag", "archived-tag1", "archived-tag2"],
            "repo-b": ["active-tag2"],
        }

        with patch.object(migrator, "_get_tags_by_archive_status", return_value={"archived-tag1", "archived-tag2"}):
            result = migrator.filter_to_archived(repo_tags)

        assert result == {"repo-a": ["archived-tag1", "archived-tag2"]}
        assert "repo-b" not in result

    def test_filter_to_archived_returns_empty_when_nothing_archived(self, migrator):
        """Test that an empty dict is returned when no tags are archived"""
        repo_tags = {
            "repo-a": ["tag1", "tag2"],
        }

        with patch.object(migrator, "_get_tags_by_archive_status", return_value=set()):
            result = migrator.filter_to_archived(repo_tags)

        assert result == {}
