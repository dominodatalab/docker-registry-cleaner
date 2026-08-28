"""
Tests for DeactivatedUserEnvFinder in delete_unused_private_environments.py.

These tests verify _generate_usage_summary() gives an honest answer for a genuinely-unused
tag, instead of the shared ImageUsageService.generate_usage_summary()'s misleading
"Referenced in system (source unknown)" fallback — mirroring the fix already applied in
delete_image.py, delete_archived_tags.py, and delete_unused_environments.py.
"""

import pytest


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
    """Set up mocks for DeactivatedUserEnvFinder dependencies."""
    mocker.patch("scripts.delete_unused_private_environments.config_manager", mock_config_manager)

    mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
    mocker.patch("utils.deletion_base.SkopeoClient")
    mocker.patch("utils.deletion_base.HealthChecker")
    mocker.patch("utils.deletion_base.CheckpointManager")

    return mock_config_manager


class TestGenerateUsageSummaryHonesty:
    """Tests that a genuinely-unused tag never gets reported as 'referenced, source unknown'."""

    def test_no_usage_reports_honestly_instead_of_referenced_in_system(self, mock_finder_deps):
        from scripts.delete_unused_private_environments import DeactivatedUserEnvFinder

        finder = DeactivatedUserEnvFinder(registry_url="registry:5000", repository="repo")

        usage = {"runs": [], "workspaces": [], "models": [], "scheduler_jobs": [], "projects": []}
        summary = finder._generate_usage_summary(usage)

        assert "Referenced in system" not in summary
        assert "No usage found" in summary

    def test_real_usage_is_still_reported(self, mock_finder_deps):
        from scripts.delete_unused_private_environments import DeactivatedUserEnvFinder

        finder = DeactivatedUserEnvFinder(registry_url="registry:5000", repository="repo")

        usage = {"runs": [{"id": "1"}], "workspaces": [], "models": []}
        summary = finder._generate_usage_summary(usage)

        assert "1 execution in MongoDB" in summary
