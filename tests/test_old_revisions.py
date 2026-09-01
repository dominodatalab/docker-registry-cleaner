"""
Tests for OldRevisionCleaner.generate_report() (python/scripts/delete_old_revisions.py).

No existing tests covered this generator before — these focus on the
standardized entries/summary fields introduced by
docs/report-schema-standardization-plan.md, plus the legacy
grouped_by_parent shape they sit alongside.
"""

import pytest


@pytest.fixture
def mock_old_revisions_deps(mocker):
    mock_config_manager = mocker.patch("scripts.delete_old_revisions.config_manager")
    mock_config_manager.get_registry_url.return_value = "registry:5000"
    mock_config_manager.get_repository.return_value = "repo"
    mocker.patch("utils.deletion_base.config_manager", mock_config_manager)
    mocker.patch("utils.deletion_base.SkopeoClient")
    mocker.patch("utils.deletion_base.HealthChecker")
    mocker.patch("utils.deletion_base.CheckpointManager")
    return mock_config_manager


def _cleaner(mock_old_revisions_deps):
    from scripts.delete_old_revisions import OldRevisionCleaner

    return OldRevisionCleaner(registry_url="registry:5000", repository="repo", keep_revisions=5)


def _revision(env_id, tag_suffix, size_bytes=100, image_type="environment", env_name="My Env"):
    from scripts.delete_old_revisions import OldRevisionInfo

    return OldRevisionInfo(
        revision_id=f"rev-{env_id}-{tag_suffix}",
        environment_id=env_id,
        environment_name=env_name,
        docker_tag=f"{env_id}-{tag_suffix}",
        full_image=f"registry:5000/repo/environment:{env_id}-{tag_suffix}",
        image_type=image_type,
        size_bytes=size_bytes,
    )


class TestGenerateReportLegacyShape:
    def test_groups_by_parent_environment_id(self, mock_old_revisions_deps):
        cleaner = _cleaner(mock_old_revisions_deps)
        revisions = [_revision("env1", "1", size_bytes=100), _revision("env1", "2", size_bytes=200)]
        report = cleaner.generate_report(revisions, total_freed_bytes=250)

        assert list(report["grouped_by_parent"].keys()) == ["env1"]
        assert len(report["grouped_by_parent"]["env1"]) == 2

    def test_dedup_aware_total_renamed_not_dropped(self, mock_old_revisions_deps):
        """total_size_bytes/total_size_gb are now reserved for the
        standardized naive sum (see docs/report-schema-standardization-plan.md)
        — the pre-existing dedup-aware total moves to
        total_freed_size_bytes/total_freed_size_gb rather than disappearing
        or being silently redefined."""
        cleaner = _cleaner(mock_old_revisions_deps)
        revisions = [_revision("env1", "1", size_bytes=100), _revision("env1", "2", size_bytes=200)]
        report = cleaner.generate_report(revisions, total_freed_bytes=250)

        assert report["summary"]["total_freed_size_bytes"] == 250
        assert report["summary"]["total_freed_size_gb"] == round(250 / (1024**3), 2)

    def test_environments_and_models_affected_counts(self, mock_old_revisions_deps):
        cleaner = _cleaner(mock_old_revisions_deps)
        revisions = [
            _revision("env1", "1", image_type="environment"),
            _revision("model1", "1", image_type="model"),
        ]
        report = cleaner.generate_report(revisions, total_freed_bytes=0)

        assert report["summary"]["environments_affected"] == 1
        assert report["summary"]["models_affected"] == 1


class TestGenerateReportStandardSchema:
    def test_entries_flattens_grouped_by_parent(self, mock_old_revisions_deps):
        cleaner = _cleaner(mock_old_revisions_deps)
        revisions = [_revision("env1", "1"), _revision("env2", "1")]
        report = cleaner.generate_report(revisions, total_freed_bytes=0)

        assert len(report["entries"]) == 2
        assert {e["parent_id"] for e in report["entries"]} == {"env1", "env2"}

    def test_entries_carry_both_tag_and_docker_tag(self, mock_old_revisions_deps):
        """entries use the standardized `tag` field name; `docker_tag` stays
        present too since it's the field grouped_by_parent's existing
        consumers already read."""
        cleaner = _cleaner(mock_old_revisions_deps)
        report = cleaner.generate_report([_revision("env1", "1")], total_freed_bytes=0)

        entry = report["entries"][0]
        assert entry["tag"] == "env1-1"
        assert entry["docker_tag"] == "env1-1"

    def test_schema_version_present(self, mock_old_revisions_deps):
        from utils.report_utils import REPORT_SCHEMA_VERSION

        cleaner = _cleaner(mock_old_revisions_deps)
        report = cleaner.generate_report([], total_freed_bytes=0)
        assert report["schema_version"] == REPORT_SCHEMA_VERSION

    def test_standardized_summary_is_naive_sum_not_dedup_aware(self, mock_old_revisions_deps):
        cleaner = _cleaner(mock_old_revisions_deps)
        revisions = [_revision("env1", "1", size_bytes=100), _revision("env1", "2", size_bytes=200)]
        # total_freed_bytes (dedup-aware) is deliberately different from the
        # naive per-entry sum (300), to prove the two aren't conflated.
        report = cleaner.generate_report(revisions, total_freed_bytes=250)

        assert report["summary"]["count"] == 2
        assert report["summary"]["total_size_bytes"] == 300  # naive sum, not 250
        assert report["summary"]["total_freed_size_bytes"] == 250  # dedup-aware, unchanged

    def test_empty_report_has_valid_standard_fields(self, mock_old_revisions_deps):
        cleaner = _cleaner(mock_old_revisions_deps)
        report = cleaner.generate_report([], total_freed_bytes=0)
        assert report["entries"] == []
        assert report["summary"]["count"] == 0
