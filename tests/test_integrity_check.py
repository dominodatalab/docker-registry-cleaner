"""
Tests for IntegrityChecker.run() (python/scripts/integrity_check.py).

No existing tests covered this generator before — these focus on the
standardized entries/summary fields introduced by
docs/report-schema-standardization-plan.md, in particular that most
issues here genuinely have no tag (`tag: None`), since this report is
about referential integrity, not usage.
"""

from unittest.mock import MagicMock

from bson import ObjectId


def _mock_db(collections: dict) -> MagicMock:
    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: collections.get(key, MagicMock())
    return mock_db


def _empty_collection() -> MagicMock:
    c = MagicMock()
    c.find.return_value = []
    return c


class TestRunStandardSchema:
    def _checker_with_orphaned_revision(self, mocker):
        from scripts.integrity_check import IntegrityChecker

        env_id = ObjectId()  # exists in environments_v2
        dangling_env_id = ObjectId()  # referenced but does not exist
        rev_id = ObjectId()

        mock_client = MagicMock()
        mocker.patch("scripts.integrity_check.get_mongo_client", return_value=mock_client)
        mocker.patch("scripts.integrity_check.config_manager").get_mongo_db.return_value = "domino"

        collections = {
            "environments_v2": _empty_collection(),
            "environment_revisions": _empty_collection(),
            "models": _empty_collection(),
            "model_versions": _empty_collection(),
        }
        collections["environments_v2"].find.return_value = [{"_id": env_id}]
        collections["environment_revisions"].find.return_value = [
            {"_id": rev_id, "environmentId": dangling_env_id, "clonedEnvironmentRevisionId": None}
        ]
        mock_db = _mock_db(collections)
        mock_client.__getitem__.return_value = mock_db

        # Registry unreachable -> has_image is left None, no image_tag resolved.
        mocker.patch("scripts.integrity_check.SkopeoClient", side_effect=Exception("registry down"))

        return IntegrityChecker()

    def test_issue_without_resolvable_tag_gets_tag_none(self, mocker):
        checker = self._checker_with_orphaned_revision(mocker)
        report = checker.run()

        assert len(report["issues"]) == 1
        assert report["issues"][0]["tag"] is None
        assert report["issues"][0]["issue_type"] == "orphaned_revision"

    def test_entries_mirrors_issues(self, mocker):
        checker = self._checker_with_orphaned_revision(mocker)
        report = checker.run()
        assert report["entries"] == report["issues"]

    def test_schema_version_present(self, mocker):
        from utils.report_utils import REPORT_SCHEMA_VERSION

        checker = self._checker_with_orphaned_revision(mocker)
        report = checker.run()
        assert report["schema_version"] == REPORT_SCHEMA_VERSION

    def test_standardized_count_matches_legacy_total_issues(self, mocker):
        checker = self._checker_with_orphaned_revision(mocker)
        report = checker.run()
        assert report["summary"]["count"] == report["summary"]["total_issues"] == 1

    def test_total_size_bytes_is_zero_not_missing(self, mocker):
        """Integrity issues never carry a size — the standardized total is
        honestly 0, not absent, for a report that measures problems, not size."""
        checker = self._checker_with_orphaned_revision(mocker)
        report = checker.run()
        assert report["summary"]["total_size_bytes"] == 0

    def test_empty_report_has_valid_standard_fields(self, mocker):
        from scripts.integrity_check import IntegrityChecker

        mock_client = MagicMock()
        mocker.patch("scripts.integrity_check.get_mongo_client", return_value=mock_client)
        mocker.patch("scripts.integrity_check.config_manager").get_mongo_db.return_value = "domino"
        collections = {
            "environments_v2": _empty_collection(),
            "environment_revisions": _empty_collection(),
            "models": _empty_collection(),
            "model_versions": _empty_collection(),
        }
        mock_client.__getitem__.return_value = _mock_db(collections)

        report = IntegrityChecker().run()
        assert report["entries"] == []
        assert report["summary"]["count"] == 0
