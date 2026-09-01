"""
Filters pre-generated report JSON down to what an org-scoped session
should see, using the docker-tag scope resolved by the backend's
GET /api/org-scope endpoint (python/utils/org_scope.py). See
docs/org-scoped-access-plan.md §3.2/§3.3 for the design.

Row-level filtering — which entries an org-scoped user sees at all — is
exact: every filter function below only keeps entries whose docker tag
appears in the org's resolved tag scope. This is the security-relevant
property and it's exact, not best-effort.

Filtered *summary* statistics are best-effort by contrast: this
recomputes straightforward counts and size sums over the now-filtered
data, but does not hand-replicate every report generator's full bespoke
accounting (e.g. per-image-type breakdowns, "why can't delete" narrative
text, protected-tag reasoning counts, or the dedup-aware totals some
reports carry under other names — `freed_space_gb`,
`total_freed_size_bytes`, `total_size_saved`, `freed_space_bytes` on a
per-user entry — none of which this module knows how to safely
recompute from a filtered subset, so they're left as-is, untouched). A
summary field this module doesn't know how to safely recompute is left
as-is from the original report — this can overstate a scoped view's
totals relative to what it actually shows, but never hides an entry it
shouldn't, and never raises a KeyError against a template expecting a
field to exist.

Reports without a filter registered in REPORT_FILTERS are not reachable
by an org-scoped session at all — see frontend/app.py's enforcement in
load_report(). Backend-only reports (final-report.json,
mongodb_usage_report.json, layers-and-sizes.json, etc.) were never part
of the browsable UI (frontend/app.py's _USER_FACING_REPORT_PREFIXES) and
stay that way for org-scoped sessions too — filtering them isn't in scope
for v1 (see docs/org-scoped-access-tickets.md).

Filtering itself is unified around the standardized `entries` field every
report carries as of docs/report-schema-standardization-plan.md's phase 1
— `_filtered_entries()` is the one place tag-matching happens, used by
every report type below, rather than each report independently filtering
its own legacy structure. What still varies per report is only how the
already-filtered `entries` get *reshaped* back into that report's
legacy, report-specific structure (a flat list, a dict grouped by an id,
a per-user nested list, or two lists split by status) — `report.html`
reads those legacy structures today, not `entries` (migrating it to read
`entries` directly is phase 2 of the standardization plan: bigger,
separate work, not done — see that doc). Every entry already carries the
field its report's legacy structure groups by (`object_id`, `parent_id`,
`user_id`, `status`) since the generators push those down additively, so
reshaping is a generic group-by, not report-specific logic.

Depends on every report actually having an `entries` field — reports
generated before docs/report-schema-standardization-plan.md's phase 1
landed won't have one, and will filter down to nothing (safe-direction
empty, not a leak) until they regenerate. Documented there as an accepted
rollout wrinkle, not something this module works around.
"""

from typing import Any, Callable, Dict, List, Optional, Set

FilterFn = Callable[[Dict[str, Any], Set[str]], Dict[str, Any]]


def _entry_tag(entry: Dict[str, Any]) -> Optional[str]:
    return entry.get("tag") or entry.get("docker_tag")


def _filtered_entries(data: Dict[str, Any], org_tags: Set[str]) -> List[Dict[str, Any]]:
    """The one tag-matching operation every report type filters through."""
    return [e for e in data.get("entries", []) if _entry_tag(e) in org_tags]


def _grouped_by(entries: List[Dict[str, Any]], key_field: str) -> Dict[Any, List[Dict[str, Any]]]:
    """Bucket already-filtered entries by the value of `key_field` — used
    both for the two dict-keyed-by-id legacy shapes (`object_id`,
    `parent_id`) and for deletion-analysis's split-by-`status`, which is
    the same operation. A key with zero surviving entries never appears,
    rather than showing up with an empty list."""
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for e in entries:
        key = e.get(key_field)
        if key is None:
            continue
        grouped.setdefault(key, []).append(e)
    return grouped


def _recompute_size_summary(summary: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Recompute the standardized `count`/`total_size_bytes`/`total_size_gb`
    fields (present in every report's summary, and on a per-user entry in
    user-size-report — docs/report-schema-standardization-plan.md) over
    `entries`. Every other summary field is left untouched by this helper.
    """
    summary = dict(summary)
    size_bytes = sum(e.get("size_bytes", 0) or 0 for e in entries)
    if "count" in summary:
        summary["count"] = len(entries)
    if "total_size_bytes" in summary:
        summary["total_size_bytes"] = size_bytes
    if "total_size_gb" in summary:
        summary["total_size_gb"] = round(size_bytes / (1024**3), 2)
    return summary


def filter_archived_tags(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    entries = _filtered_entries(data, org_tags)
    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_archived_object_ids" in summary:
        summary["total_archived_object_ids"] = len({e["object_id"] for e in entries if e.get("object_id")})
    return {**data, "archived_tags": entries, "entries": entries, "summary": summary}


def filter_unused_environments(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    entries = _filtered_entries(data, org_tags)
    grouped = _grouped_by(entries, "object_id")
    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_unused_environment_ids" in summary:
        summary["total_unused_environment_ids"] = len(grouped)
    return {**data, "grouped_by_object_id": grouped, "entries": entries, "summary": summary}


def filter_old_revisions(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    entries = _filtered_entries(data, org_tags)
    grouped = _grouped_by(entries, "parent_id")
    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_old_revisions" in summary:
        summary["total_old_revisions"] = len(entries)
    if "environments_affected" in summary:
        summary["environments_affected"] = len(
            {k for k, v in grouped.items() if v and v[0].get("image_type") == "environment"}
        )
    if "models_affected" in summary:
        summary["models_affected"] = len({k for k, v in grouped.items() if v and v[0].get("image_type") == "model"})
    return {**data, "grouped_by_parent": grouped, "entries": entries, "summary": summary}


def filter_image_size_report(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    entries = _filtered_entries(data, org_tags)
    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_images" in summary:
        summary["total_images"] = len(entries)
    return {**data, "images": entries, "entries": entries, "summary": summary}


def filter_user_size_report(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    """user-size-report nests images under each user rather than being a
    flat tag-bearing list itself — reshaped here by grouping the filtered
    `entries` (one entry per (user, image) pair) by `user_id`. Each user's
    *other* fields (`freed_space_bytes` and its per-type breakdowns, which
    are dedup-aware and this module has no way to recompute for a filtered
    subset) are carried through from that user's original entry as-is —
    best-effort, per this module's docstring, not silently dropped."""
    entries = _filtered_entries(data, org_tags)
    original_users = {u.get("user_id"): u for u in data.get("users", [])}
    grouped = _grouped_by(entries, "user_id")

    users = []
    for user_id, images in grouped.items():
        # Start from the original per-user record when one exists (carries
        # through user_name/login_id/freed_space_* etc. best-effort), but
        # user_id/images/image_count/total_size_bytes/total_size_gb are set
        # unconditionally below rather than relying on _recompute_size_summary's
        # "only touch a field that already exists" behavior (right for the
        # top-level summary's report-specific extras, wrong here — these are
        # core fields this report's entries always have, not optional ones).
        user = dict(original_users.get(user_id, {}))
        size_bytes = sum(i.get("size_bytes", 0) or 0 for i in images)
        user["user_id"] = user_id
        user["images"] = images
        user["image_count"] = len(images)
        user["total_size_bytes"] = size_bytes
        user["total_size_gb"] = round(size_bytes / (1024**3), 2)
        users.append(user)

    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_users" in summary:
        summary["total_users"] = len(users)
    if "total_images" in summary:
        summary["total_images"] = len(entries)
    return {**data, "users": users, "entries": entries, "summary": summary}


def filter_integrity_check(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    """Only issues carrying a resolvable tag can be attributed to an org at
    all — most referential-integrity issues (a dangling reference with no
    live registry image) never get one (see
    docs/org-scoped-access-plan.md's schema investigation of this report).
    Those are dropped from an org-scoped view rather than shown
    unattributed, since there's no way to confirm they're this org's."""
    entries = _filtered_entries(data, org_tags)
    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "total_issues" in summary:
        summary["total_issues"] = len(entries)
    return {**data, "issues": entries, "entries": entries, "summary": summary}


def filter_deletion_analysis(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    """Splits back into unused/used the same way archived-tags already
    distinguishes by a `status` field — grouping filtered `entries` by
    `status` is the identical operation `_grouped_by` already does for the
    dict-keyed-by-id reports, just with `"unused"`/`"used"` as the keys."""
    entries = _filtered_entries(data, org_tags)
    by_status = _grouped_by(entries, "status")
    unused = by_status.get("unused", [])
    used = by_status.get("used", [])

    summary = _recompute_size_summary(data.get("summary", {}), entries)
    if "unused_images" in summary:
        summary["unused_images"] = len(unused)
    if "used_images" in summary:
        summary["used_images"] = len(used)
    if "total_images_analyzed" in summary:
        summary["total_images_analyzed"] = len(entries)
    return {**data, "unused_images": unused, "used_images": used, "entries": entries, "summary": summary}


# Maps a report filename PREFIX (matching frontend/app.py's
# _USER_FACING_REPORT_PREFIXES — reports are saved with a timestamp suffix,
# e.g. archived-tags-2026-08-29-14-30-00.json, so this is a prefix match,
# not an exact filename) to its filter function. Every prefix an org-scoped
# session can request must have an entry here.
REPORT_FILTERS: Dict[str, FilterFn] = {
    "archived-tags": filter_archived_tags,
    "unused-environments": filter_unused_environments,
    "old-revisions": filter_old_revisions,
    "image-size-report": filter_image_size_report,
    "user-size-report": filter_user_size_report,
    "integrity-check": filter_integrity_check,
    "deletion-analysis": filter_deletion_analysis,
}


def get_filter_for_filename(filename: str) -> Optional[FilterFn]:
    """Return the filter function for this report filename, matching by
    prefix (see REPORT_FILTERS' docstring) — or None if this filename
    doesn't match any known user-facing report type at all, meaning an
    org-scoped session should never be able to load it, filtered or not."""
    for prefix, filter_fn in REPORT_FILTERS.items():
        if filename.startswith(prefix):
            return filter_fn
    return None
