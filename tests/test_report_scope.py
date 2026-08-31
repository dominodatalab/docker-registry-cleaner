"""
Tests for frontend/report_scope.py — per-report-type org-scope filtering.

Sample shapes below are modeled on the real generators
(python/scripts/delete_archived_tags.py, delete_unused_environments.py,
delete_old_revisions.py, image_size_report.py, user_size_report.py,
integrity_check.py, delete_image.py) rather than invented — see
docs/org-scoped-access-plan.md's report-schema investigation.
"""

import sys
from pathlib import Path

_frontend_dir = Path(__file__).parent.parent / "frontend"
if str(_frontend_dir) not in sys.path:
    sys.path.insert(0, str(_frontend_dir))

from report_scope import (  # noqa: E402
    REPORT_FILTERS,
    filter_archived_tags,
    filter_deletion_analysis,
    filter_image_size_report,
    filter_integrity_check,
    filter_old_revisions,
    filter_unused_environments,
    filter_user_size_report,
    get_filter_for_filename,
)

ORG_TAGS = {"my-tag-1", "my-tag-2"}


class TestGetFilterForFilename:
    def test_matches_timestamped_filename(self):
        assert get_filter_for_filename("archived-tags-2026-08-29-14-30-00.json") is filter_archived_tags

    def test_unknown_prefix_returns_none(self):
        assert get_filter_for_filename("final-report.json") is None
        assert get_filter_for_filename("mongodb_usage_report.json") is None

    def test_every_registered_prefix_is_a_prefix_match(self):
        for prefix in REPORT_FILTERS:
            assert get_filter_for_filename(f"{prefix}-2026-01-01-00-00-00.json") is REPORT_FILTERS[prefix]


class TestFilterArchivedTags:
    def test_keeps_only_org_tags_and_recomputes_counts(self):
        data = {
            "summary": {"total_archived_object_ids": 2, "total_size_bytes": 300, "total_size_gb": 0.0},
            "archived_tags": [
                {"object_id": "a1", "tag": "my-tag-1", "size_bytes": 100, "status": "unused"},
                {"object_id": "a2", "tag": "not-mine", "size_bytes": 200, "status": "unused"},
            ],
            "metadata": {"registry_url": "registry:5000"},
        }
        result = filter_archived_tags(data, ORG_TAGS)
        assert [e["tag"] for e in result["archived_tags"]] == ["my-tag-1"]
        assert result["summary"]["total_archived_object_ids"] == 1
        assert result["summary"]["total_size_bytes"] == 100
        assert result["metadata"] == {"registry_url": "registry:5000"}  # untouched

    def test_original_data_not_mutated(self):
        data = {"summary": {}, "archived_tags": [{"tag": "not-mine", "size_bytes": 1}]}
        filter_archived_tags(data, ORG_TAGS)
        assert data["archived_tags"] == [{"tag": "not-mine", "size_bytes": 1}]


class TestFilterUnusedEnvironments:
    def test_grouped_dict_filters_and_drops_empty_keys(self):
        data = {
            "summary": {"total_unused_environment_ids": 2, "total_size_bytes": 0},
            "grouped_by_object_id": {
                "env1": [{"tag": "my-tag-1", "size_bytes": 50, "status": "unused"}],
                "env2": [{"tag": "not-mine", "size_bytes": 50, "status": "unused"}],
            },
        }
        result = filter_unused_environments(data, ORG_TAGS)
        assert list(result["grouped_by_object_id"].keys()) == ["env1"]
        assert result["summary"]["total_unused_environment_ids"] == 1
        assert result["summary"]["total_size_bytes"] == 50


class TestFilterOldRevisions:
    def test_grouped_by_parent_and_type_breakdown(self):
        data = {
            "summary": {
                "total_old_revisions": 3,
                "environments_affected": 2,
                "models_affected": 1,
                "total_size_bytes": 0,
            },
            "grouped_by_parent": {
                "env-parent": [{"docker_tag": "my-tag-1", "image_type": "environment", "size_bytes": 10}],
                "other-env-parent": [{"docker_tag": "not-mine", "image_type": "environment", "size_bytes": 10}],
                "model-parent": [{"docker_tag": "my-tag-2", "image_type": "model", "size_bytes": 20}],
            },
        }
        result = filter_old_revisions(data, ORG_TAGS)
        assert set(result["grouped_by_parent"].keys()) == {"env-parent", "model-parent"}
        assert result["summary"]["total_old_revisions"] == 2
        assert result["summary"]["environments_affected"] == 1
        assert result["summary"]["models_affected"] == 1
        assert result["summary"]["total_size_bytes"] == 30

    def test_entries_join_on_docker_tag_field_not_tag(self):
        # old-revisions.json uses "docker_tag", not "tag" — confirm the
        # generic _entry_tag() helper checks both field names.
        data = {"summary": {}, "grouped_by_parent": {"p": [{"docker_tag": "my-tag-1"}]}}
        result = filter_old_revisions(data, ORG_TAGS)
        assert result["grouped_by_parent"] == {"p": [{"docker_tag": "my-tag-1"}]}


class TestFilterImageSizeReport:
    def test_filters_flat_images_list(self):
        data = {
            "summary": {"total_images": 2, "total_size_bytes": 300},
            "images": [
                {"tag": "my-tag-1", "total_size_bytes": 100},
                {"tag": "not-mine", "total_size_bytes": 200},
            ],
        }
        result = filter_image_size_report(data, ORG_TAGS)
        assert len(result["images"]) == 1
        assert result["summary"]["total_images"] == 1
        assert result["summary"]["total_size_bytes"] == 100


class TestFilterUserSizeReport:
    def test_keeps_users_with_at_least_one_matching_image(self):
        data = {
            "summary": {"total_users": 2, "total_images": 3, "total_size_bytes": 0},
            "users": [
                {
                    "user_id": "u1",
                    "image_count": 2,
                    "total_size_bytes": 300,
                    "images": [
                        {"tag": "my-tag-1", "total_size_bytes": 100},
                        {"tag": "not-mine", "total_size_bytes": 200},
                    ],
                },
                {
                    "user_id": "u2",
                    "image_count": 1,
                    "total_size_bytes": 50,
                    "images": [{"tag": "not-mine", "total_size_bytes": 50}],
                },
            ],
        }
        result = filter_user_size_report(data, ORG_TAGS)
        assert len(result["users"]) == 1
        assert result["users"][0]["user_id"] == "u1"
        assert result["users"][0]["image_count"] == 1  # only my-tag-1 survives
        assert result["users"][0]["total_size_bytes"] == 100
        assert result["summary"]["total_users"] == 1
        assert result["summary"]["total_images"] == 1

    def test_user_with_no_matching_images_is_dropped(self):
        data = {"summary": {}, "users": [{"user_id": "u1", "images": [{"tag": "not-mine"}]}]}
        result = filter_user_size_report(data, ORG_TAGS)
        assert result["users"] == []


class TestFilterIntegrityCheck:
    def test_only_issues_with_matching_image_tag_survive(self):
        data = {
            "summary": {"total_issues": 2},
            "issues": [
                {"collection": "environment_revisions", "issue_type": "orphaned_revision", "image_tag": "my-tag-1"},
                {"collection": "environment_revisions", "issue_type": "missing_environment_id"},  # no image_tag
            ],
        }
        result = filter_integrity_check(data, ORG_TAGS)
        assert len(result["issues"]) == 1
        assert result["summary"]["total_issues"] == 1

    def test_unattributable_issue_without_tag_is_dropped_not_shown(self):
        data = {"summary": {}, "issues": [{"issue_type": "broken_clone_reference"}]}
        result = filter_integrity_check(data, ORG_TAGS)
        assert result["issues"] == []


class TestFilterDeletionAnalysis:
    def test_filters_both_unused_and_used_lists(self):
        data = {
            "summary": {"unused_images": 1, "used_images": 1, "total_images_analyzed": 2},
            "unused_images": [
                {"tag": "my-tag-1", "status": "unused"},
                {"tag": "not-mine", "status": "unused"},
            ],
            "used_images": [
                {"tag": "my-tag-2", "status": "used"},
                {"tag": "not-mine-2", "status": "used"},
            ],
        }
        result = filter_deletion_analysis(data, ORG_TAGS)
        assert len(result["unused_images"]) == 1
        assert len(result["used_images"]) == 1
        assert result["summary"]["unused_images"] == 1
        assert result["summary"]["used_images"] == 1
        assert result["summary"]["total_images_analyzed"] == 2
