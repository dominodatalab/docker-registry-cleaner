#!/usr/bin/env python3
"""
MongoDB cleanup tool for environment revision records tied to Docker image tags.

Reads targets from a file and either finds or deletes matching documents in the
configured Mongo collection based on `metadata.dockerImageName.tag`.

Before deleting any document, checks whether it is still referenced anywhere in
Domino (runs, workspaces, projects, scheduler jobs, organizations, user preferences,
and app versions for environment_revisions; parent model status for model_versions).
Candidate documents that are still referenced are reported and skipped.

Accepted file formats (first non-comment token per line):
- Environments-style: <24-char ObjectID> [other columns ignored]
  → matches any tag that starts with "<ObjectID>-"
- Full tag: repo/image:tag or tag
  → exact match on the full tag value

Authentication/Config:
- Uses centralized settings from config_manager (host, port, rs, db, collection)
- Credentials from env: MONGODB_USERNAME (default: admin), MONGODB_PASSWORD (required)

Usage examples:
- Dry run (find): python mongo_cleanup.py --file ./environments
- Delete:         python mongo_cleanup.py --apply --file ./environments
- Custom file:    python mongo_cleanup.py --apply --file ./my-environments
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

from bson import ObjectId

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.mongo_utils import get_mongo_client


def iter_targets_from_file(path: str) -> Iterator[Tuple[str, str]]:
    """Yield (mode, value) pairs where mode is 'objectId' or 'tag'."""
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first_token = line.split()[0]
            candidate = first_token
            if len(candidate) == 24:
                try:
                    int(candidate, 16)
                    yield ("objectId", candidate)
                    continue
                except ValueError:
                    pass
            yield ("tag", first_token)


def _find_referenced_env_ids(db, env_ids: Set[str], rev_ids: Set[str]) -> Dict[str, List[str]]:
    """Return {id_str: [reason, ...]} for env/revision IDs still referenced in Domino.

    Checks both direct revision references (runs, workspace_session) and parent-environment
    references (workspace, projects, scheduler_jobs, organizations, userPreferences, app_versions).
    A revision is considered blocked if either its own _id or its parent environment ID is
    found in any of these collections.
    """
    referenced: Dict[str, List[str]] = {}

    def mark(oid, reason: str) -> None:
        if oid is not None:
            referenced.setdefault(str(oid), []).append(reason)

    env_oids = [ObjectId(e) for e in env_ids] if env_ids else []
    rev_oids = [ObjectId(r) for r in rev_ids] if rev_ids else []

    # Revision-level checks
    if rev_oids:
        for oid in db["runs"].distinct("environmentRevisionId", {"environmentRevisionId": {"$in": rev_oids}}):
            mark(oid, "runs.environmentRevisionId")
        for field in ("environmentRevisionId", "computeClusterEnvironmentRevisionId"):
            for oid in db["workspace_session"].distinct(field, {field: {"$in": rev_oids}}):
                mark(oid, f"workspace_session.{field}")

    # Environment-level checks (parent environment still in use → block its revisions)
    if env_oids:
        for oid in db["runs"].distinct("environmentId", {"environmentId": {"$in": env_oids}}):
            mark(oid, "runs.environmentId")

        for oid in db["workspace"].distinct(
            "configTemplate.environmentId", {"configTemplate.environmentId": {"$in": env_oids}}
        ):
            mark(oid, "workspace.configTemplate.environmentId")

        for field in (
            "environmentId",
            "config.environmentId",
            "computeClusterEnvironmentId",
            "config.computeClusterProps.computeEnvironmentId",
        ):
            for oid in db["workspace_session"].distinct(field, {field: {"$in": env_oids}}):
                mark(oid, f"workspace_session.{field}")

        for oid in db["projects"].distinct(
            "overrideV2EnvironmentId",
            {"overrideV2EnvironmentId": {"$in": env_oids}, "isArchived": {"$ne": True}},
        ):
            mark(oid, "projects.overrideV2EnvironmentId")

        for oid in db["scheduler_jobs"].distinct(
            "jobDataPlain.overrideEnvironmentId", {"jobDataPlain.overrideEnvironmentId": {"$in": env_oids}}
        ):
            mark(oid, "scheduler_jobs.jobDataPlain.overrideEnvironmentId")

        for oid in db["organizations"].distinct(
            "defaultV2EnvironmentId", {"defaultV2EnvironmentId": {"$in": env_oids}}
        ):
            mark(oid, "organizations.defaultV2EnvironmentId")

        existing = set(db.list_collection_names())

        if "userPreferences" in existing:
            for oid in db["userPreferences"].distinct(
                "defaultEnvironmentId", {"defaultEnvironmentId": {"$in": env_oids}}
            ):
                mark(oid, "userPreferences.defaultEnvironmentId")

        if "model_products" in existing and "app_versions" in existing:
            unarchived_app_ids = db["model_products"].distinct("_id", {"isArchived": False})
            if unarchived_app_ids:
                for oid in db["app_versions"].distinct(
                    "environmentId", {"appId": {"$in": unarchived_app_ids}, "environmentId": {"$in": env_oids}}
                ):
                    mark(oid, "app_versions.environmentId (unarchived app)")

    return referenced


def _find_referenced_model_version_ids(db, candidates: List[Dict]) -> Dict[str, List[str]]:
    """Return {ver_id: [reason]} for model_version candidates whose parent model is still active."""
    referenced: Dict[str, List[str]] = {}
    for doc in candidates:
        ver_id = str(doc["_id"])
        model_id_val = doc.get("modelId")
        if not isinstance(model_id_val, dict):
            continue
        model_oid = model_id_val.get("value")
        if model_oid is None:
            continue
        model_doc = db["models"].find_one({"_id": model_oid}, {"_id": 1, "isArchived": 1})
        if model_doc is not None and not model_doc.get("isArchived", False):
            referenced[ver_id] = [f"parent model {model_oid} is not archived"]
    return referenced


def connect_and_execute(
    apply: bool, target_mode: str, value: str, collection_name: str = "environment_revisions"
) -> None:
    """Find (and optionally delete) MongoDB records matching the given tag or ObjectID.

    Before deleting, checks whether each candidate document is still referenced
    anywhere in Domino. Referenced documents are reported and skipped.
    """
    from utils.tag_matching import extract_model_tag_prefix

    db_name = config_manager.get_mongo_db()
    client = get_mongo_client()
    db = client[db_name]
    collection = db[collection_name]

    tag_field = (
        "metadata.builds.slug.image.tag" if collection_name == "model_versions" else "metadata.dockerImageName.tag"
    )

    if target_mode == "objectId":
        query = {tag_field: {"$regex": f"^{value}-"}}
    elif collection_name == "model_versions":
        prefix = extract_model_tag_prefix(value)
        query = {
            "$or": [
                {tag_field: value},
                {tag_field: {"$regex": f"^{re.escape(prefix)}(-|$)"}},
            ]
        }
    else:
        query = {tag_field: value}

    # Fetch candidates with the fields needed for reference checking
    if collection_name == "environment_revisions":
        projection = {"_id": 1, "environmentId": 1}
    else:
        projection = {"_id": 1, "modelId": 1}

    candidates = list(collection.find(query, projection))

    if not candidates:
        print(f"  No matching documents found for {target_mode}='{value}' in {collection_name}")
        return

    # Reference check
    if collection_name == "environment_revisions":
        rev_ids = {str(doc["_id"]) for doc in candidates}
        env_ids = {str(doc["environmentId"]) for doc in candidates if doc.get("environmentId")}
        referenced = _find_referenced_env_ids(db, env_ids, rev_ids)

        def block_reasons(doc: Dict) -> List[str]:
            return referenced.get(str(doc["_id"]), []) + referenced.get(str(doc.get("environmentId", "")), [])

    elif collection_name == "model_versions":
        referenced = _find_referenced_model_version_ids(db, candidates)

        def block_reasons(doc: Dict) -> List[str]:
            return referenced.get(str(doc["_id"]), [])

    else:

        def block_reasons(doc: Dict) -> List[str]:
            return []

    safe = [doc for doc in candidates if not block_reasons(doc)]
    blocked = [doc for doc in candidates if block_reasons(doc)]

    if blocked:
        print(f"  SKIPPING {len(blocked)} document(s) still referenced in Domino:")
        for doc in blocked:
            print(f"    {doc['_id']}: {'; '.join(block_reasons(doc))}")

    if apply:
        print(f"Deleting {len(safe)} document(s) for {target_mode}='{value}' in {db_name}.{collection_name}")
        if safe:
            res = collection.delete_many({"_id": {"$in": [doc["_id"] for doc in safe]}})
            print(f"  deleted: {res.deleted_count}")
        else:
            print(f"  Nothing deleted (all {len(blocked)} candidate(s) are still referenced)")
    else:
        print(f"DRY RUN: {len(candidates)} matched, {len(blocked)} blocked, {len(safe)} safe to delete")
        for doc in candidates:
            reasons = block_reasons(doc)
            status = f"BLOCKED ({'; '.join(reasons)})" if reasons else "safe to delete"
            print(f"  {doc['_id']}: {status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cleanup Mongo records by Docker tag or ObjectID prefix")
    parser.add_argument(
        "--file", required=True, help="Path to file: first column is ObjectID (environments-style) or full tag"
    )
    parser.add_argument(
        "--collection",
        default="environment_revisions",
        help="MongoDB collection to clean up (default: environment_revisions)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply deletion (default: dry-run mode shows what would be deleted)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.apply:
        print("⚠️  DELETION MODE - MongoDB records will be deleted")
    else:
        print("🔍 DRY RUN MODE - No changes will be made")
    print()

    for target_mode, value in iter_targets_from_file(args.file):
        connect_and_execute(args.apply, target_mode, value, args.collection)


if __name__ == "__main__":
    main()
