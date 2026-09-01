"""
Tests for frontend/report_scope.py — per-report-type org-scope filtering.

Sample shapes below are modeled on the real generators
(python/scripts/delete_archived_tags.py, delete_unused_environments.py,
delete_old_revisions.py, image_size_report.py, user_size_report.py,
integrity_check.py, delete_image.py) rather than invented — see
docs/org-scoped-access-plan.md's report-schema investigation.

Every fixture below includes the standardized `entries`/`count` fields
(docs/report-schema-standardization-plan.md) alongside each report's
legacy structure, matching what every generator actually emits as of
that change — not just the legacy shape these filters originally
targeted. This is deliberate: these two fields were the ones that leaked
completely unfiltered when report_scope.py was first rebased onto the
standardized schema (every filter function returned `{**data, ...}`
without ever touching the new top-level `entries` key or `summary.count`,
so the *legacy* field came back correctly filtered while `entries` quietly
carried the full, unfiltered list through the exact same response) — a
real access-control bypass, not a hypothetical one. Every test class
below now asserts on `result["entries"]` and `result["summary"]["count"]`
explicitly, not just the legacy fields, so this can't silently reappear.

report_scope.py was later migrated so every filter function reads from
`entries` as its one source of truth and *reshapes* the result back into
the legacy structure, rather than filtering each legacy structure
independently. `TestFilteringReadsFromEntriesNotLegacyField` below locks
that in directly, by deliberately making a fixture's legacy field and
`entries` disagree and asserting the output follows `entries` — proof the
two aren't independently filtered (which would silently re-diverge if
someone reverted the read source for just one report type).
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
    def _data(self):
        entries = [
            {"object_id": "a1", "tag": "my-tag-1", "size_bytes": 100, "status": "unused"},
            {"object_id": "a2", "tag": "not-mine", "size_bytes": 200, "status": "unused"},
        ]
        return {
            "schema_version": 2,
            "summary": {
                "total_archived_object_ids": 2,
                "count": 2,
                "total_size_bytes": 300,
                "total_size_gb": 0.0,
            },
            "archived_tags": entries,
            "entries": entries,
            "metadata": {"registry_url": "registry:5000"},
        }

    def test_keeps_only_org_tags_and_recomputes_counts(self):
        result = filter_archived_tags(self._data(), ORG_TAGS)
        assert [e["tag"] for e in result["archived_tags"]] == ["my-tag-1"]
        assert result["summary"]["total_archived_object_ids"] == 1
        assert result["summary"]["total_size_bytes"] == 100
        assert result["metadata"] == {"registry_url": "registry:5000"}  # untouched

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        """Regression test: entries must never carry unfiltered data through
        even when the legacy field (archived_tags) is correctly filtered."""
        result = filter_archived_tags(self._data(), ORG_TAGS)
        assert [e["tag"] for e in result["entries"]] == ["my-tag-1"]
        assert result["entries"] == result["archived_tags"]

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_archived_tags(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 1

    def test_original_data_not_mutated(self):
        data = self._data()
        filter_archived_tags(data, ORG_TAGS)
        assert [e["tag"] for e in data["archived_tags"]] == ["my-tag-1", "not-mine"]


class TestFilterUnusedEnvironments:
    def _data(self):
        # The real generator (delete_unused_environments.py) puts `object_id`
        # directly on every entry, not just as the grouped_by_object_id dict
        # key — needed since filtering now groups the flat `entries` list by
        # that field rather than filtering a pre-grouped dict.
        grouped = {
            "env1": [{"object_id": "env1", "tag": "my-tag-1", "size_bytes": 50, "status": "unused"}],
            "env2": [{"object_id": "env2", "tag": "not-mine", "size_bytes": 50, "status": "unused"}],
        }
        return {
            "schema_version": 2,
            "summary": {"total_unused_environment_ids": 2, "count": 2, "total_size_bytes": 0},
            "grouped_by_object_id": grouped,
            "entries": [e for entries in grouped.values() for e in entries],
        }

    def test_grouped_dict_filters_and_drops_empty_keys(self):
        result = filter_unused_environments(self._data(), ORG_TAGS)
        assert list(result["grouped_by_object_id"].keys()) == ["env1"]
        assert result["summary"]["total_unused_environment_ids"] == 1
        assert result["summary"]["total_size_bytes"] == 50

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        result = filter_unused_environments(self._data(), ORG_TAGS)
        assert [e["tag"] for e in result["entries"]] == ["my-tag-1"]

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_unused_environments(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 1


class TestFilterOldRevisions:
    def _data(self):
        # The real generator (delete_old_revisions.py) puts `docker_tag`
        # (legacy), `tag` (standardized), and `parent_id` (the grouping key,
        # pushed onto the entry itself) on every entry — they're the same
        # dict objects `entries` is built from, not a separately-shaped
        # list. Test fixtures need all three fields, or a filter bug here
        # would look like a fixture bug instead (as it did the first time
        # this test was written with docker_tag only, and again when
        # grouping moved from filtering grouped_by_parent directly to
        # grouping already-filtered entries by parent_id).
        grouped = {
            "env-parent": [
                {
                    "docker_tag": "my-tag-1",
                    "tag": "my-tag-1",
                    "parent_id": "env-parent",
                    "image_type": "environment",
                    "size_bytes": 10,
                }
            ],
            "other-env-parent": [
                {
                    "docker_tag": "not-mine",
                    "tag": "not-mine",
                    "parent_id": "other-env-parent",
                    "image_type": "environment",
                    "size_bytes": 10,
                }
            ],
            "model-parent": [
                {
                    "docker_tag": "my-tag-2",
                    "tag": "my-tag-2",
                    "parent_id": "model-parent",
                    "image_type": "model",
                    "size_bytes": 20,
                }
            ],
        }
        return {
            "schema_version": 2,
            "summary": {
                "total_old_revisions": 3,
                "environments_affected": 2,
                "models_affected": 1,
                "count": 3,
                "total_size_bytes": 40,
                # This is the field the report-standardization work renamed
                # (was total_size_bytes/total_size_gb before) to avoid
                # colliding with the standardized naive-sum fields above —
                # confirm the filter leaves it alone rather than trying to
                # recompute a dedup-aware total it has no way to derive.
                "total_freed_size_bytes": 999,
            },
            "grouped_by_parent": grouped,
            "entries": [e for entries in grouped.values() for e in entries],
        }

    def test_grouped_by_parent_and_type_breakdown(self):
        result = filter_old_revisions(self._data(), ORG_TAGS)
        assert set(result["grouped_by_parent"].keys()) == {"env-parent", "model-parent"}
        assert result["summary"]["total_old_revisions"] == 2
        assert result["summary"]["environments_affected"] == 1
        assert result["summary"]["models_affected"] == 1
        assert result["summary"]["total_size_bytes"] == 30

    def test_entries_join_on_docker_tag_field_not_tag(self):
        # A entry with only "docker_tag" (no "tag") must still match —
        # confirm the generic _entry_tag() helper checks both field names.
        # Filtering now reads from `entries`, not `grouped_by_parent`
        # directly, so the entry needs to actually be in `entries` (with its
        # grouping key) for this to exercise anything real.
        entry = {"docker_tag": "my-tag-1", "parent_id": "p"}
        data = {"summary": {}, "grouped_by_parent": {"p": [entry]}, "entries": [entry]}
        result = filter_old_revisions(data, ORG_TAGS)
        assert result["grouped_by_parent"] == {"p": [entry]}

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        result = filter_old_revisions(self._data(), ORG_TAGS)
        assert {e["tag"] for e in result["entries"]} == {"my-tag-1", "my-tag-2"}

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_old_revisions(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 2

    def test_dedup_aware_legacy_total_is_left_alone(self):
        """total_freed_size_bytes has no reliable way to be recomputed from
        a filtered subset (it's dedup-aware across the *original* full
        candidate set) — confirmed unchanged rather than silently zeroed
        or mis-recomputed."""
        result = filter_old_revisions(self._data(), ORG_TAGS)
        assert result["summary"]["total_freed_size_bytes"] == 999


class TestFilterImageSizeReport:
    def _data(self):
        entries = [
            {"tag": "my-tag-1", "size_bytes": 100, "total_size_bytes": 100},
            {"tag": "not-mine", "size_bytes": 200, "total_size_bytes": 200},
        ]
        return {
            "schema_version": 2,
            "summary": {"total_images": 2, "count": 2, "total_size_bytes": 300},
            "images": entries,
            "entries": entries,
        }

    def test_filters_flat_images_list(self):
        result = filter_image_size_report(self._data(), ORG_TAGS)
        assert len(result["images"]) == 1
        assert result["summary"]["total_images"] == 1
        assert result["summary"]["total_size_bytes"] == 100

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        result = filter_image_size_report(self._data(), ORG_TAGS)
        assert [e["tag"] for e in result["entries"]] == ["my-tag-1"]

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_image_size_report(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 1


class TestFilterUserSizeReport:
    def _data(self):
        # The real generator (user_size_report.py) puts `size_bytes` on
        # every image as a standardized alias of `total_size_bytes` — needed
        # since _recompute_size_summary sums `size_bytes` specifically.
        u1_images = [
            {"tag": "my-tag-1", "total_size_bytes": 100, "size_bytes": 100, "user_id": "u1"},
            {"tag": "not-mine", "total_size_bytes": 200, "size_bytes": 200, "user_id": "u1"},
        ]
        u2_images = [{"tag": "not-mine", "total_size_bytes": 50, "size_bytes": 50, "user_id": "u2"}]
        return {
            "schema_version": 2,
            "summary": {"total_users": 2, "total_images": 3, "count": 3, "total_size_bytes": 350},
            "users": [
                {"user_id": "u1", "image_count": 2, "total_size_bytes": 300, "images": u1_images},
                {"user_id": "u2", "image_count": 1, "total_size_bytes": 50, "images": u2_images},
            ],
            "entries": u1_images + u2_images,
        }

    def test_keeps_users_with_at_least_one_matching_image(self):
        result = filter_user_size_report(self._data(), ORG_TAGS)
        assert len(result["users"]) == 1
        assert result["users"][0]["user_id"] == "u1"
        assert result["users"][0]["image_count"] == 1  # only my-tag-1 survives
        assert result["users"][0]["total_size_bytes"] == 100
        assert result["summary"]["total_users"] == 1
        assert result["summary"]["total_images"] == 1

    def test_user_with_no_matching_images_is_dropped(self):
        data = {
            "summary": {},
            "users": [{"user_id": "u1", "images": [{"tag": "not-mine"}]}],
            "entries": [{"tag": "not-mine", "user_id": "u1"}],
        }
        result = filter_user_size_report(data, ORG_TAGS)
        assert result["users"] == []

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        """The reconstructed entries list must reflect the filtered users,
        not the original unfiltered per-user image lists."""
        result = filter_user_size_report(self._data(), ORG_TAGS)
        assert [e["tag"] for e in result["entries"]] == ["my-tag-1"]

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_user_size_report(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 1


class TestFilterIntegrityCheck:
    def _data(self):
        entries = [
            {
                "collection": "environment_revisions",
                "issue_type": "orphaned_revision",
                "image_tag": "my-tag-1",
                "tag": "my-tag-1",
            },
            {"collection": "environment_revisions", "issue_type": "missing_environment_id", "tag": None},
        ]
        return {
            "schema_version": 2,
            "summary": {"total_issues": 2, "count": 2, "total_size_bytes": 0},
            "issues": entries,
            "entries": entries,
        }

    def test_only_issues_with_matching_image_tag_survive(self):
        result = filter_integrity_check(self._data(), ORG_TAGS)
        assert len(result["issues"]) == 1
        assert result["summary"]["total_issues"] == 1

    def test_unattributable_issue_without_tag_is_dropped_not_shown(self):
        data = {"summary": {}, "issues": [{"issue_type": "broken_clone_reference"}], "entries": []}
        result = filter_integrity_check(data, ORG_TAGS)
        assert result["issues"] == []

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        result = filter_integrity_check(self._data(), ORG_TAGS)
        assert len(result["entries"]) == 1
        assert result["entries"][0]["image_tag"] == "my-tag-1"

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_integrity_check(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 1


class TestFilterDeletionAnalysis:
    def _data(self):
        unused = [
            {"tag": "my-tag-1", "status": "unused", "size_bytes": 10},
            {"tag": "not-mine", "status": "unused", "size_bytes": 20},
        ]
        used = [
            {"tag": "my-tag-2", "status": "used", "size_bytes": 30},
            {"tag": "not-mine-2", "status": "used", "size_bytes": 40},
        ]
        return {
            "schema_version": 2,
            "summary": {
                "unused_images": 2,
                "used_images": 2,
                "total_images_analyzed": 4,
                "count": 4,
                "total_size_bytes": 100,
            },
            "unused_images": unused,
            "used_images": used,
            "entries": unused + used,
        }

    def test_filters_both_unused_and_used_lists(self):
        result = filter_deletion_analysis(self._data(), ORG_TAGS)
        assert len(result["unused_images"]) == 1
        assert len(result["used_images"]) == 1
        assert result["summary"]["unused_images"] == 1
        assert result["summary"]["used_images"] == 1
        assert result["summary"]["total_images_analyzed"] == 2

    def test_standardized_entries_field_is_filtered_not_leaked(self):
        result = filter_deletion_analysis(self._data(), ORG_TAGS)
        assert {e["tag"] for e in result["entries"]} == {"my-tag-1", "my-tag-2"}

    def test_standardized_count_is_recomputed_not_leaked(self):
        result = filter_deletion_analysis(self._data(), ORG_TAGS)
        assert result["summary"]["count"] == 2
        assert result["summary"]["total_size_bytes"] == 40  # 10 + 30


class TestFilteringReadsFromEntriesNotLegacyField:
    """Each fixture's legacy field is deliberately empty/stale while
    `entries` carries the real, matching data — proving the filter
    functions read from `entries`, not from filtering their own legacy
    structure independently. If a future change reverted that for just one
    report type, these would catch it immediately."""

    def test_archived_tags(self):
        data = {
            "summary": {},
            "archived_tags": [],
            "entries": [{"tag": "my-tag-1", "object_id": "a1", "size_bytes": 100}],
        }
        result = filter_archived_tags(data, ORG_TAGS)
        assert [e["tag"] for e in result["archived_tags"]] == ["my-tag-1"]

    def test_unused_environments(self):
        data = {
            "summary": {},
            "grouped_by_object_id": {},
            "entries": [{"tag": "my-tag-1", "object_id": "env1", "size_bytes": 50}],
        }
        result = filter_unused_environments(data, ORG_TAGS)
        assert list(result["grouped_by_object_id"].keys()) == ["env1"]

    def test_old_revisions(self):
        data = {
            "summary": {},
            "grouped_by_parent": {},
            "entries": [{"tag": "my-tag-1", "parent_id": "p1", "size_bytes": 10}],
        }
        result = filter_old_revisions(data, ORG_TAGS)
        assert list(result["grouped_by_parent"].keys()) == ["p1"]

    def test_image_size_report(self):
        data = {"summary": {}, "images": [], "entries": [{"tag": "my-tag-1", "size_bytes": 100}]}
        result = filter_image_size_report(data, ORG_TAGS)
        assert [e["tag"] for e in result["images"]] == ["my-tag-1"]

    def test_user_size_report(self):
        data = {
            "summary": {},
            "users": [],
            "entries": [{"tag": "my-tag-1", "user_id": "u1", "size_bytes": 100}],
        }
        result = filter_user_size_report(data, ORG_TAGS)
        assert result["users"][0]["user_id"] == "u1"
        assert [e["tag"] for e in result["users"][0]["images"]] == ["my-tag-1"]

    def test_integrity_check(self):
        data = {"summary": {}, "issues": [], "entries": [{"image_tag": "my-tag-1", "tag": "my-tag-1"}]}
        result = filter_integrity_check(data, ORG_TAGS)
        assert [e["tag"] for e in result["issues"]] == ["my-tag-1"]

    def test_deletion_analysis(self):
        data = {
            "summary": {},
            "unused_images": [],
            "used_images": [],
            "entries": [{"tag": "my-tag-1", "status": "unused", "size_bytes": 10}],
        }
        result = filter_deletion_analysis(data, ORG_TAGS)
        assert [e["tag"] for e in result["unused_images"]] == ["my-tag-1"]
