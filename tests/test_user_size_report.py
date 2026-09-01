"""
Tests for generate_user_size_report() (python/scripts/user_size_report.py).

No existing tests covered this generator before — these focus on the
standardized entries/summary fields introduced by
docs/report-schema-standardization-plan.md, in particular the
flattening of its per-user nested `images` lists into one `entries` list.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_analyzer_and_reports(mocker):
    """Two environment images owned by two different users, no shared layers."""
    mocker.patch(
        "scripts.user_size_report.build_tag_to_owners_mapping",
        return_value={
            "tag1": {("user1", "Alice")},
            "tag2": {("user2", "Bob")},
        },
    )
    mocker.patch(
        "scripts.user_size_report.build_user_login_id_mapping",
        return_value={"user1": "alice", "user2": "bob"},
    )
    analyzer = MagicMock()
    analyzer.images = {
        "environment:tag1": {"tag": "tag1"},
        "environment:tag2": {"tag": "tag2"},
    }
    sizes = {"environment:tag1": 100, "environment:tag2": 200}
    analyzer.get_image_total_size.side_effect = lambda image_id: sizes[image_id]
    analyzer.freed_space_if_deleted.side_effect = lambda ids: sum(sizes[i] for i in ids)
    return analyzer, {}


class TestGenerateUserSizeReportStandardSchema:
    def test_entries_flattens_per_user_images(self, mock_analyzer_and_reports):
        from scripts.user_size_report import generate_user_size_report

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])

        assert len(report["entries"]) == 2
        assert {e["tag"] for e in report["entries"]} == {"tag1", "tag2"}

    def test_entries_carry_user_id_pushed_down_from_parent(self, mock_analyzer_and_reports):
        from scripts.user_size_report import generate_user_size_report

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])

        by_tag = {e["tag"]: e for e in report["entries"]}
        assert by_tag["tag1"]["user_id"] == "user1"
        assert by_tag["tag1"]["login_id"] == "alice"
        assert by_tag["tag2"]["user_id"] == "user2"

    def test_entries_also_reachable_through_legacy_users_structure(self, mock_analyzer_and_reports):
        """entries and users[].images share the same dict objects — a field
        added to one is visible through the other."""
        from scripts.user_size_report import generate_user_size_report

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])

        for user in report["users"]:
            for image in user["images"]:
                assert image in report["entries"]
                assert image["user_id"] == user["user_id"]

    def test_size_bytes_alias_present(self, mock_analyzer_and_reports):
        from scripts.user_size_report import generate_user_size_report

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])

        for entry in report["entries"]:
            assert entry["size_bytes"] == entry["total_size_bytes"]

    def test_schema_version_present(self, mock_analyzer_and_reports):
        from scripts.user_size_report import generate_user_size_report
        from utils.report_utils import REPORT_SCHEMA_VERSION

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])
        assert report["schema_version"] == REPORT_SCHEMA_VERSION

    def test_standardized_summary_matches_legacy_totals(self, mock_analyzer_and_reports):
        from scripts.user_size_report import generate_user_size_report

        analyzer, mongodb_reports = mock_analyzer_and_reports
        report = generate_user_size_report(analyzer, mongodb_reports, image_types=["environment"])

        assert report["summary"]["count"] == report["summary"]["total_images"] == 2
        assert report["summary"]["total_size_bytes"] == 300

    def test_empty_report_has_valid_standard_fields(self, mocker):
        from scripts.user_size_report import generate_user_size_report

        mocker.patch("scripts.user_size_report.build_tag_to_owners_mapping", return_value={})
        mocker.patch("scripts.user_size_report.build_user_login_id_mapping", return_value={})
        analyzer = MagicMock()
        analyzer.images = {}

        report = generate_user_size_report(analyzer, {}, image_types=["environment"])
        assert report["entries"] == []
        assert report["summary"]["count"] == 0
