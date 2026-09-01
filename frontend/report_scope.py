"""
Filters pre-generated report JSON down to what an org-scoped session
should see, using the docker-tag scope resolved by the backend's
GET /api/org-scope endpoint (python/utils/org_scope.py). See
docs/org-scoped-access-plan.md §3.2/§3.3 for the design.

Row-level filtering — which entries an org-scoped user sees at all — is
exact: every filter function below only keeps entries whose docker tag
(the `tag`/`docker_tag` field every report type carries per-entry) appears
in the org's resolved tag scope. This is the security-relevant property
and it's exact, not best-effort.

Filtered *summary* statistics are best-effort by contrast: this
recomputes straightforward counts (list/dict length) and size sums over
the now-filtered data, but does not hand-replicate every report
generator's full bespoke accounting (e.g. per-image-type breakdowns,
"why can't delete" narrative text, protected-tag reasoning counts). A
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

The seven bespoke functions below exist because the seven report
generators each invented their own shape independently (flat lists, two
different dict-keyed-by-ObjectId conventions, and one nested-per-user
structure — see docs/report-schema-standardization-plan.md). That doc
proposes a standardized `{"summary", "entries", "metadata"}` shape that
would collapse this whole module to one generic filter function; it's
scoped but not yet implemented, since it touches all seven generators,
report.html, and api.py's metrics reader, not just this file.
"""

from typing import Any, Callable, Dict, List, Optional, Set

FilterFn = Callable[[Dict[str, Any], Set[str]], Dict[str, Any]]


def _entry_tag(entry: Dict[str, Any]) -> Optional[str]:
    return entry.get("tag") or entry.get("docker_tag")


def _filter_list(entries: List[Dict[str, Any]], org_tags: Set[str]) -> List[Dict[str, Any]]:
    return [e for e in entries if _entry_tag(e) in org_tags]


def _filter_grouped(grouped: Dict[str, List[Dict[str, Any]]], org_tags: Set[str]) -> Dict[str, List[Dict[str, Any]]]:
    """For the two report types keyed by ObjectId (unused-environments'
    grouped_by_object_id, old-revisions' grouped_by_parent) rather than a
    flat list. A key is dropped entirely once none of its entries survive
    filtering, rather than kept with an empty list."""
    result = {}
    for key, entries in grouped.items():
        kept = _filter_list(entries, org_tags)
        if kept:
            result[key] = kept
    return result


def _recompute_size_summary(summary: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Recompute the standardized `count`/`total_size_bytes`/`total_size_gb`
    fields (docs/report-schema-standardization-plan.md — present in every
    report's summary as of that change) as well as this report's own
    identically-named legacy field, if present, over `entries`. Every other
    summary field is left untouched by this helper.

    IMPORTANT: `entries` here must be the *filtered* set — this is the same
    computation that used to be merely best-effort for the legacy
    `total_size_bytes`/`total_size_gb` fields, but since the standardization
    change these are no longer optional/legacy-only: they're on every
    report's summary now, so getting this right here is load-bearing, not
    cosmetic.
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
    entries = _filter_list(data.get("archived_tags", []), org_tags)
    summary = dict(data.get("summary", {}))
    if "total_archived_object_ids" in summary:
        summary["total_archived_object_ids"] = len({e["object_id"] for e in entries if e.get("object_id")})
    summary = _recompute_size_summary(summary, entries)
    return {**data, "archived_tags": entries, "entries": entries, "summary": summary}


def filter_unused_environments(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    grouped = _filter_grouped(data.get("grouped_by_object_id", {}), org_tags)
    flat = [e for entries in grouped.values() for e in entries]
    summary = dict(data.get("summary", {}))
    if "total_unused_environment_ids" in summary:
        summary["total_unused_environment_ids"] = len(grouped)
    summary = _recompute_size_summary(summary, flat)
    return {**data, "grouped_by_object_id": grouped, "entries": flat, "summary": summary}


def filter_old_revisions(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    grouped = _filter_grouped(data.get("grouped_by_parent", {}), org_tags)
    flat = [e for entries in grouped.values() for e in entries]
    summary = dict(data.get("summary", {}))
    if "total_old_revisions" in summary:
        summary["total_old_revisions"] = len(flat)
    if "environments_affected" in summary:
        summary["environments_affected"] = len(
            {k for k, v in grouped.items() if v and v[0].get("image_type") == "environment"}
        )
    if "models_affected" in summary:
        summary["models_affected"] = len({k for k, v in grouped.items() if v and v[0].get("image_type") == "model"})
    summary = _recompute_size_summary(summary, flat)
    return {**data, "grouped_by_parent": grouped, "entries": flat, "summary": summary}


def filter_image_size_report(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    entries = _filter_list(data.get("images", []), org_tags)
    summary = dict(data.get("summary", {}))
    if "total_images" in summary:
        summary["total_images"] = len(entries)
    # This report's per-entry size field is total_size_bytes, not size_bytes —
    # adapt to _recompute_size_summary's expected field name.
    summary = _recompute_size_summary(summary, [{"size_bytes": e.get("total_size_bytes", 0)} for e in entries])
    return {**data, "images": entries, "entries": entries, "summary": summary}


def filter_user_size_report(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    """user-size-report nests an `images` list under each user rather than
    being a flat tag-bearing list itself — filtered one level deeper: a
    user's entry is kept only if at least one of their images is in the
    org's tag scope, with that nested list itself filtered down too.

    The standardized `entries` field (docs/report-schema-standardization-plan.md)
    is this report's flattened equivalent — one entry per (user, image)
    pair, the same shape the generator itself produces — reconstructed here
    from the already-filtered per-user image lists rather than filtered
    separately, so the two can't drift apart.
    """
    users = []
    entries: List[Dict[str, Any]] = []
    for user in data.get("users", []):
        images = _filter_list(user.get("images", []), org_tags)
        if not images:
            continue
        user = dict(user)
        user["images"] = images
        if "image_count" in user:
            user["image_count"] = len(images)
        user = _recompute_size_summary(user, [{"size_bytes": i.get("total_size_bytes", 0)} for i in images])
        users.append(user)
        entries.extend(images)

    summary = dict(data.get("summary", {}))
    if "total_users" in summary:
        summary["total_users"] = len(users)
    if "total_images" in summary:
        summary["total_images"] = sum(u.get("image_count", 0) for u in users)
    summary = _recompute_size_summary(summary, [{"size_bytes": u.get("total_size_bytes", 0)} for u in users])
    return {**data, "users": users, "entries": entries, "summary": summary}


def filter_integrity_check(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    """Only issues carrying a resolvable image_tag can be attributed to an
    org at all — most referential-integrity issues (a dangling reference
    with no live registry image) never get one (see
    docs/org-scoped-access-plan.md's schema investigation of this report).
    Those are dropped from an org-scoped view rather than shown
    unattributed, since there's no way to confirm they're this org's."""
    entries = [e for e in data.get("issues", []) if e.get("image_tag") in org_tags]
    summary = dict(data.get("summary", {}))
    if "total_issues" in summary:
        summary["total_issues"] = len(entries)
    summary = _recompute_size_summary(summary, entries)
    return {**data, "issues": entries, "entries": entries, "summary": summary}


def filter_deletion_analysis(data: Dict[str, Any], org_tags: Set[str]) -> Dict[str, Any]:
    unused = _filter_list(data.get("unused_images", []), org_tags)
    used = _filter_list(data.get("used_images", []), org_tags)
    summary = dict(data.get("summary", {}))
    if "unused_images" in summary:
        summary["unused_images"] = len(unused)
    if "used_images" in summary:
        summary["used_images"] = len(used)
    if "total_images_analyzed" in summary:
        summary["total_images_analyzed"] = len(unused) + len(used)
    entries = unused + used
    summary = _recompute_size_summary(summary, entries)
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
