"""
Tests for generate_image_size_report() (python/scripts/image_size_report.py).

No existing tests covered this generator before — these focus on the
standardized entries/summary fields introduced by
docs/report-schema-standardization-plan.md.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_analyzer(mocker):
    """A minimal ImageAnalyzer stand-in: two environment images, sized 100
    and 200 bytes, with no shared layers (freed_space == total_size)."""
    mocker.patch("scripts.image_size_report.build_image_metadata_mapping", return_value={})
    analyzer = MagicMock()
    analyzer.images = {
        "environment:tag1": {"tag": "tag1", "repository": "dominodatalab/environment", "digest": "sha256:aaa"},
        "environment:tag2": {"tag": "tag2", "repository": "dominodatalab/environment", "digest": "sha256:bbb"},
    }
    sizes = {"environment:tag1": 100, "environment:tag2": 200}
    analyzer.get_image_total_size.side_effect = lambda image_id: sizes[image_id]
    analyzer.freed_space_if_deleted.side_effect = lambda ids: sum(sizes[i] for i in ids)
    return analyzer


class TestGenerateImageSizeReportStandardSchema:
    def test_entries_mirrors_images_list(self, mock_analyzer):
        from scripts.image_size_report import generate_image_size_report

        report = generate_image_size_report(mock_analyzer, image_types=["environment"])
        assert report["entries"] == report["images"]
        assert {e["tag"] for e in report["entries"]} == {"tag1", "tag2"}

    def test_entries_carry_size_bytes_alias(self, mock_analyzer):
        from scripts.image_size_report import generate_image_size_report

        report = generate_image_size_report(mock_analyzer, image_types=["environment"])
        for entry in report["entries"]:
            assert entry["size_bytes"] == entry["total_size_bytes"]

    def test_schema_version_present(self, mock_analyzer):
        from scripts.image_size_report import generate_image_size_report
        from utils.report_utils import REPORT_SCHEMA_VERSION

        report = generate_image_size_report(mock_analyzer, image_types=["environment"])
        assert report["schema_version"] == REPORT_SCHEMA_VERSION

    def test_standardized_count_matches_legacy_total_images(self, mock_analyzer):
        from scripts.image_size_report import generate_image_size_report

        report = generate_image_size_report(mock_analyzer, image_types=["environment"])
        assert report["summary"]["count"] == report["summary"]["total_images"] == 2

    def test_standardized_size_matches_existing_naive_sum(self, mock_analyzer):
        """Unlike delete_old_revisions.py, no rename was needed here — this
        report's existing total_size_bytes was already a naive per-image
        sum, the same thing the standardized field means."""
        from scripts.image_size_report import generate_image_size_report

        report = generate_image_size_report(mock_analyzer, image_types=["environment"])
        assert report["summary"]["total_size_bytes"] == report["summary"]["total_size_bytes"] == 300

    def test_empty_report_has_valid_standard_fields(self, mocker):
        from scripts.image_size_report import generate_image_size_report

        mocker.patch("scripts.image_size_report.build_image_metadata_mapping", return_value={})
        analyzer = MagicMock()
        analyzer.images = {}
        analyzer.freed_space_if_deleted.return_value = 0

        report = generate_image_size_report(analyzer, image_types=["environment"])
        assert report["entries"] == []
        assert report["summary"]["count"] == 0
