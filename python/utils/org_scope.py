"""Live MongoDB resolution of "what does organization X own", for the
org-scoped frontend access feature.

See docs/org-scoped-access-plan.md for the full design and the schema
facts this module relies on (Appendix A/C/D in particular). Deliberately
separate from extract_metadata.py's report-generation pipelines: those
build a full-instance report on a schedule; this module answers a
narrower, per-request question ("what does this specific set of orgs own
right now") so it can run synchronously as part of a backend API call
(see python/api.py's /api/org-scope) without waiting on a report refresh.

Ownership model (confirmed live, not assumed — see the plan doc):
an organization's own `_id` doubles as a pseudo-user document's `_id` in
`users`, and any Domino asset's owner field (`projects.ownerId`,
`environments_v2.ownerId`, ...) just holds that kind of user-shaped id,
personal or org. So resolving "is this owner an org or a person" is a
single lookup: check `organizations` first, fall back to `users`.

Known v1 gap: model images (`models`/`model_versions`) and app images
(`app_versions`) are not yet included. Neither collection carries a
project-linkage field anywhere in this codebase (verified by search, not
assumed), and `models.metadata.createdBy` (the only "owner" field that
does exist) identifies the user who created the model, not the owning
project/org — so it can't be used to attribute a model to an org's
project the way `projects.ownerId` or `runs.projectId` can. See
docs/org-scoped-access-plan.md for the status of this gap.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from bson import ObjectId
from bson.errors import InvalidId

from utils.extract_metadata import (
    organizations_env_usage_pipeline,
    projects_env_usage_pipeline,
    runs_env_usage_pipeline,
    scheduler_jobs_env_usage_pipeline,
    workspace_env_usage_pipeline,
)
from utils.mongo_utils import get_db, get_mongo_client

logger = logging.getLogger(__name__)


def _to_object_ids(ids: List[str]) -> List[ObjectId]:
    """Best-effort str -> ObjectId conversion, silently dropping anything
    that isn't a valid ObjectId rather than raising — a caller passing a
    stray malformed id shouldn't take down the whole scope resolution."""
    out = []
    for i in ids:
        try:
            out.append(ObjectId(i))
        except (InvalidId, TypeError):
            logger.warning(f"Skipping invalid ObjectId in org-scope request: {i!r}")
    return out


class OrgScopeResolver:
    """Resolves org ownership of projects/environments/images against a
    live Mongo connection. One instance = one Mongo connection, reused
    across the several queries a single scope resolution needs.

    Use as a context manager so the connection is always closed:

        with OrgScopeResolver() as resolver:
            scope = resolver.resolve(org_ids)
    """

    def __init__(self, client=None):
        self._client = client or get_mongo_client()
        self._owns_client = client is None
        self._db = get_db(self._client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OrgScopeResolver":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── org -> directly-owned projects/environments ────────────────────────

    def owned_project_ids(self, org_ids: List[ObjectId]) -> List[ObjectId]:
        """Projects whose ownerId is one of these orgs (§2.3: an org-owned
        project's ownerId is the org's own pseudo-user _id — no separate
        org-ownership field exists on `projects`)."""
        if not org_ids:
            return []
        docs = self._db["projects"].find(
            {"ownerId": {"$in": org_ids}, "isArchived": {"$ne": True}},
            {"_id": 1},
        )
        return [d["_id"] for d in docs]

    def owned_environment_tags(self, org_ids: List[ObjectId]) -> List[str]:
        """Docker tags for environments owned directly by these orgs
        (visibility == "Organization"), independent of any project usage —
        covers an org's own environment images even if never run/opened by
        anyone (so never showing up in the usage pipelines below)."""
        if not org_ids:
            return []
        envs = list(
            self._db["environments_v2"].find(
                {"ownerId": {"$in": org_ids}, "visibility": "Organization", "isArchived": {"$ne": True}},
                {"activeRevisionId": 1},
            )
        )
        revision_ids = [e["activeRevisionId"] for e in envs if e.get("activeRevisionId")]
        if not revision_ids:
            return []
        revisions = self._db["environment_revisions"].find(
            {"_id": {"$in": revision_ids}, "metadata.isBuilt": {"$ne": False}},
            {"metadata.dockerImageName": 1},
        )
        tags = []
        for rev in revisions:
            tag = ((rev.get("metadata") or {}).get("dockerImageName") or {}).get("tag")
            if tag:
                tags.append(tag)
        return tags

    # ── full-instance usage -> tag/owner map ────────────────────────────────
    #
    # Reuses the same aggregation pipelines extract_metadata.py already runs
    # for the periodic mongodb_usage_report.json, rather than reimplementing
    # (and re-risking) their environment-resolution logic. These are
    # full-collection scans — the same cost the tool already pays for its
    # scheduled reports — so the caller (python/api.py) should treat a scope
    # resolution as non-trivial work, not a cheap per-keystroke call; the
    # frontend caches its result rather than re-requesting on every page
    # (docs/org-scoped-access-plan.md, D4).

    def _project_owner_map(self) -> Dict[ObjectId, ObjectId]:
        """project _id -> ownerId, for every project — not just org-owned
        ones — since this is used to attribute *any* usage record's
        project back to its owner, org or personal, for the "also used by"
        lookup (D3), not just to confirm the calling org's own projects."""
        docs = self._db["projects"].find({}, {"_id": 1, "ownerId": 1})
        return {d["_id"]: d["ownerId"] for d in docs if d.get("ownerId") is not None}

    def _tag_owner_ids(self) -> Dict[str, Set[ObjectId]]:
        """tag -> set of owner ids (project owners, or the org directly, for
        every usage record on the instance) — the raw material both org's
        own scope (D1/D2) and "who else uses this" (D3) are derived from."""
        project_owner = self._project_owner_map()
        tag_owners: Dict[str, Set[ObjectId]] = {}

        def add(tag: Optional[str], owner_id: Optional[ObjectId]) -> None:
            if not tag or not owner_id:
                return
            tag_owners.setdefault(tag, set()).add(owner_id)

        for rec in self._db["projects"].aggregate(projects_env_usage_pipeline()):
            add(rec.get("environment_docker_tag"), rec.get("owner_id"))

        for rec in self._db["organizations"].aggregate(organizations_env_usage_pipeline()):
            add(rec.get("environment_docker_tag"), rec.get("organization_id"))

        for rec in self._db["runs"].aggregate(runs_env_usage_pipeline()):
            add(rec.get("environment_docker_tag"), rec.get("project_owner_id"))

        for rec in self._db["scheduler_jobs"].aggregate(scheduler_jobs_env_usage_pipeline()):
            owner_id = project_owner.get(rec.get("project_id"))
            add(rec.get("environment_docker_tag"), owner_id)

        for rec in self._db["workspace"].aggregate(workspace_env_usage_pipeline()):
            owner_id = rec.get("owner_id") or project_owner.get(rec.get("project_id"))
            for tag_field in (
                "environment_docker_tag",
                "session_environment_docker_tag",
                "session_compute_environment_docker_tag",
                "project_default_environment_docker_tag",
            ):
                add(rec.get(tag_field), owner_id)

        return tag_owners

    def _resolve_owner_names(self, owner_ids: Set[ObjectId]) -> Dict[ObjectId, Dict[str, str]]:
        """Classify each owner id as an organization or a personal user, and
        resolve a display name — one batched query per collection rather
        than N+1 lookups per tag."""
        if not owner_ids:
            return {}
        remaining = set(owner_ids)
        resolved: Dict[ObjectId, Dict[str, str]] = {}

        for org in self._db["organizations"].find({"_id": {"$in": list(remaining)}}, {"name": 1}):
            resolved[org["_id"]] = {"type": "organization", "id": str(org["_id"]), "name": org.get("name", "")}
            remaining.discard(org["_id"])

        if remaining:
            for user in self._db["users"].find({"_id": {"$in": list(remaining)}}, {"fullName": 1, "loginId": 1}):
                name = user.get("fullName") or (user.get("loginId") or {}).get("id", "")
                resolved[user["_id"]] = {"type": "user", "id": str(user["_id"]), "name": name}
                remaining.discard(user["_id"])

        for missing_id in remaining:
            logger.warning(f"Could not resolve owner id {missing_id} to an organization or a user")

        return resolved

    # ── public entry point ──────────────────────────────────────────────────

    def resolve(self, org_ids: List[str]) -> Dict[str, Any]:
        """Resolve the full org-scoped view for the given org ids.

        Returns a JSON-serializable dict:
            {
              "org_ids": [...],                    # echoed back, as given
              "project_ids": [...],                # projects owned directly by these orgs
              "tags": [...],                        # docker tags visible to these orgs
              "other_owners": {                     # for each of the org's own visible
                "<tag>": [{"type", "id", "name"}],  # tags, every *other* owner also
              },                                     # using that tag (D3) — omitted for
            }                                        # tags with no other owner.
        """
        oids = _to_object_ids(org_ids)
        project_ids = self.owned_project_ids(oids)
        org_id_set = set(oids)
        project_id_set = set(project_ids)

        tag_owner_ids = self._tag_owner_ids()

        def is_orgs_own(owners: Set[ObjectId]) -> bool:
            return bool(owners & org_id_set)

        own_tags: Set[str] = set(self.owned_environment_tags(oids))
        other_owners: Dict[str, Set[ObjectId]] = {}

        for tag, owners in tag_owner_ids.items():
            if is_orgs_own(owners):
                own_tags.add(tag)
                others = owners - org_id_set
                if others:
                    other_owners[tag] = others

        all_owner_ids: Set[ObjectId] = set()
        for others in other_owners.values():
            all_owner_ids |= others
        owner_names = self._resolve_owner_names(all_owner_ids)

        resolved_other_owners = {}
        for tag, owners in other_owners.items():
            named = [owner_names[o] for o in owners if o in owner_names]
            # Omit the tag entirely if none of its other owners could be named,
            # rather than leaving an empty list — that would be ambiguous
            # between "no other owner" and "another owner we can't identify".
            if named:
                resolved_other_owners[tag] = named

        return {
            "org_ids": [str(o) for o in oids],
            "project_ids": [str(p) for p in project_id_set],
            "tags": sorted(own_tags),
            "other_owners": resolved_other_owners,
        }


def resolve_org_scope(org_ids: List[str]) -> Dict[str, Any]:
    """Convenience wrapper: open a connection, resolve, close. See
    OrgScopeResolver for the connection-reuse version."""
    with OrgScopeResolver() as resolver:
        return resolver.resolve(org_ids)
