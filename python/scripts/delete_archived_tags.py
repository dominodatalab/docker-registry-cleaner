#!/usr/bin/env python3
"""
Find and optionally delete archived tags in MongoDB and Docker registry.

This script queries MongoDB for archived environment and/or model ObjectIDs and finds
matching Docker tags in the registry. Can optionally delete the Docker images and clean
up MongoDB records.

Workflow:
- Query MongoDB for archived environments and/or models (where isArchived == true)
- Query related revision/version collections for related ObjectIDs
- Extract ObjectIDs from both archived records and their revisions/versions
- Find Docker tags containing these ObjectIDs in environment and/or model images
- Generate a comprehensive report of archived tags and their sizes
- Optionally delete Docker images and clean up MongoDB records (with --apply)

Usage examples:
  # Find archived environment tags (dry-run)
  python delete_archived_tags.py --environment

  # Find archived model tags (dry-run)
  python delete_archived_tags.py --model

  # Find both archived environments and models
  python delete_archived_tags.py --environment --model

  # Delete archived environment tags directly
  python delete_archived_tags.py --environment --apply

  # Force deletion without confirmation
  python delete_archived_tags.py --environment --apply --force

  # Back up images to S3 before deletion
  python delete_archived_tags.py --environment --apply --backup

  # Optional: Back up images to S3 with custom bucket and region
  python delete_archived_tags.py --environment --apply --backup --s3-bucket my-backup-bucket --region us-east-1

  # Optional: Override registry settings
  python delete_archived_tags.py --environment --registry-url registry.example.com --repository my-repo

  # Optional: Custom output file
  python delete_archived_tags.py --environment --output archived-tags.json

  # Optional: Delete from pre-generated file
  python delete_archived_tags.py --environment --apply --input archived-tags.json
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from bson import ObjectId

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from scripts.backup_restore import process_backup
from utils.config_manager import ConfigManager, SkopeoClient, config_manager
from utils.deletion_base import BaseDeletionScript
from utils.image_data_analysis import ImageAnalyzer
from utils.image_metadata import extract_model_tag_from_version_doc
from utils.image_usage import ImageUsageService
from utils.logging_utils import get_logger, setup_logging
from utils.mongo_utils import get_mongo_client
from utils.report_utils import ensure_mongodb_reports, get_timestamp_suffix, save_json, sizeof_fmt
from utils.tag_matching import model_tags_match

logger = get_logger(__name__)


@dataclass
class ArchivedTagInfo:
    """Data class for archived tag information"""

    object_id: str
    image_type: str
    tag: str
    full_image: str
    size_bytes: int = 0
    record_type: Optional[str] = None  # 'environment', 'revision', 'model', or 'version'


class ArchivedTagsFinder(BaseDeletionScript):
    """Main class for finding and managing archived tags"""

    def __init__(
        self,
        registry_url: str,
        repository: str,
        process_environments: bool = False,
        process_models: bool = False,
        enable_docker_deletion: bool = False,
        registry_statefulset: Optional[str] = None,
        recent_days: Optional[int] = None,
    ):
        super().__init__(
            registry_url=registry_url,
            repository=repository,
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset=registry_statefulset,
        )
        self.max_workers = config_manager.get_max_workers()
        self.recent_days = recent_days

        # Determine what to process
        self.process_environments = process_environments
        self.process_models = process_models

        # Determine image types to scan based on what we're processing
        self.image_types = []
        if self.process_environments:
            self.image_types.append("environment")
        if self.process_models:
            self.image_types.append("model")

        if not self.image_types:
            raise ValueError("Must specify at least one of --environment or --model")

    def _generate_usage_summary(self, usage: Dict) -> str:
        """Generate a human-readable summary of why an image/tag is in use.

        Mirrors the behavior used in delete_image, using count fields when available.
        """
        reasons = []

        # Check runs - prefer count field, fall back to list length
        runs_count = usage.get("runs_count", 0)
        if runs_count == 0:
            runs_list = usage.get("runs", [])
            runs_count = len(runs_list) if runs_list else 0
        if runs_count > 0:
            reasons.append(f"{runs_count} execution{'s' if runs_count > 1 else ''} in MongoDB")

        # Check workspaces - prefer count field, fall back to list length
        workspaces_count = usage.get("workspaces_count", 0)
        if workspaces_count == 0:
            workspaces_list = usage.get("workspaces", [])
            workspaces_count = len(workspaces_list) if workspaces_list else 0
        if workspaces_count > 0:
            reasons.append(f"{workspaces_count} workspace{'s' if workspaces_count > 1 else ''}")

        # Check models - prefer count field, fall back to list length
        models_count = usage.get("models_count", 0)
        if models_count == 0:
            models_list = usage.get("models", [])
            models_count = len(models_list) if models_list else 0
        if models_count > 0:
            reasons.append(f"{models_count} model{'s' if models_count > 1 else ''}")

        # Check scheduler_jobs (always a list)
        scheduler_jobs = usage.get("scheduler_jobs", [])
        if scheduler_jobs:
            scheduler_count = len(scheduler_jobs)
            reasons.append(f"{scheduler_count} scheduler job{'s' if scheduler_count > 1 else ''}")

        # Check projects (always a list)
        projects = usage.get("projects", [])
        if projects:
            project_count = len(projects)
            reasons.append(f"{project_count} project{'s' if project_count > 1 else ''} using as default")

        # Check organizations (always a list)
        organizations = usage.get("organizations", [])
        if organizations:
            org_count = len(organizations)
            reasons.append(f"{org_count} organization{'s' if org_count > 1 else ''} using as default")

        # Check app_versions (always a list)
        app_versions = usage.get("app_versions", [])
        if app_versions:
            app_version_count = len(app_versions)
            reasons.append(f"{app_version_count} app version{'s' if app_version_count > 1 else ''}")

        if not reasons:
            # Try to provide more context about what we checked
            checked_fields = []
            if usage.get("runs") or usage.get("runs_count"):
                checked_fields.append("runs")
            if usage.get("workspaces") or usage.get("workspaces_count"):
                checked_fields.append("workspaces")
            if usage.get("models") or usage.get("models_count"):
                checked_fields.append("models")
            if usage.get("scheduler_jobs"):
                checked_fields.append("scheduler_jobs")
            if usage.get("projects"):
                checked_fields.append("projects")
            if usage.get("organizations"):
                checked_fields.append("organizations")
            if usage.get("app_versions"):
                checked_fields.append("app_versions")

            if checked_fields:
                return f"No usage found (checked: {', '.join(checked_fields)}, all empty)"
            else:
                return "No usage found (no usage data available)"

        return ", ".join(reasons)

    def fetch_archived_object_ids(
        self,
    ) -> Tuple[List[str], Dict[str, str], Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[Tuple[str, str]]]]:
        """Fetch archived ObjectIDs from MongoDB

        Returns:
            Tuple of (all_archived_ids, id_to_type_map, environment_to_revisions, model_to_versions, model_tag_to_version).
            id_to_type_map maps ObjectID to record type.
            environment_to_revisions maps environment ID -> list of revision IDs.
            model_to_versions maps model ID -> list of version IDs.
            model_tag_to_version maps model ID -> list of (stored_tag, version_id) for resolving registry tags like modelId-vX-timestamp-UID.
        """
        mongo_client = get_mongo_client()
        all_archived_ids = []
        id_to_type_map = {}  # Maps ObjectID to 'environment', 'revision', 'model', or 'version'
        revision_to_cloned_revision = {}  # Maps revision ID to cloned revision ID
        revision_to_environment = {}  # Maps revision ID to environment ID
        environment_to_revisions: Dict[str, List[str]] = {}
        model_to_versions: Dict[str, List[str]] = {}
        model_tag_to_version: Dict[str, List[Tuple[str, str]]] = {}  # model_id -> [(stored_tag, version_id), ...]

        try:
            db = mongo_client[config_manager.get_mongo_db()]

            # Process environments if requested
            if self.process_environments:
                environments_collection = db["environments_v2"]
                cursor = environments_collection.find({"isArchived": True}, {"_id": 1})
                archived_environment_ids = []

                for doc in cursor:
                    _id = doc.get("_id")
                    if _id is not None:
                        obj_id_str = str(_id)
                        archived_environment_ids.append(obj_id_str)
                        id_to_type_map[obj_id_str] = "environment"

                self.logger.info(f"Found {len(archived_environment_ids)} archived environment ObjectIDs")

                # Check environment_revisions for documents with matching environmentId
                environment_revisions_collection = db["environment_revisions"]
                archived_revision_ids = []

                if archived_environment_ids:
                    environment_object_ids = [ObjectId(env_id) for env_id in archived_environment_ids]
                    # Exclude revisions where metadata.isBuilt=false (failed builds); they have dockerImageName but no matching Docker tag
                    revision_cursor = environment_revisions_collection.find(
                        {"environmentId": {"$in": environment_object_ids}, "metadata.isBuilt": {"$ne": False}},
                        {"_id": 1, "clonedEnvironmentRevisionId": 1, "environmentId": 1},
                    )

                    for doc in revision_cursor:
                        _id = doc.get("_id")
                        if _id is not None:
                            obj_id_str = str(_id)
                            archived_revision_ids.append(obj_id_str)
                            id_to_type_map[obj_id_str] = "revision"

                            # Store cloned revision ID if present
                            cloned_rev_id = doc.get("clonedEnvironmentRevisionId")
                            if cloned_rev_id is not None:
                                revision_to_cloned_revision[obj_id_str] = str(cloned_rev_id)

                            # Store environment ID for this revision
                            env_id = doc.get("environmentId")
                            if env_id is not None:
                                revision_to_environment[obj_id_str] = str(env_id)

                    self.logger.info(
                        f"Found {len(archived_revision_ids)} environment revision ObjectIDs for archived environments"
                    )
                    if revision_to_cloned_revision:
                        self.logger.info(
                            f"Found {len(revision_to_cloned_revision)} revisions with clonedEnvironmentRevisionId"
                        )
                    # Map environment ID -> list of revision IDs (for report: tags keyed by env map to revisions)
                    for rev_id, env_id in revision_to_environment.items():
                        environment_to_revisions.setdefault(env_id, []).append(rev_id)

                all_archived_ids.extend(archived_environment_ids)
                all_archived_ids.extend(archived_revision_ids)

            # Process models if requested
            if self.process_models:
                models_collection = db["models"]
                cursor = models_collection.find({"isArchived": True}, {"_id": 1})
                archived_model_ids = []

                for doc in cursor:
                    _id = doc.get("_id")
                    if _id is not None:
                        obj_id_str = str(_id)
                        archived_model_ids.append(obj_id_str)
                        id_to_type_map[obj_id_str] = "model"

                self.logger.info(f"Found {len(archived_model_ids)} archived model ObjectIDs")

                # Check model_versions for documents with matching modelId.value
                model_versions_collection = db["model_versions"]
                archived_version_ids = []

                if archived_model_ids:
                    model_object_ids = [ObjectId(model_id) for model_id in archived_model_ids]
                    version_cursor = model_versions_collection.find(
                        {"modelId.value": {"$in": model_object_ids}},
                        {"_id": 1, "modelId.value": 1, "metadata.builds": 1},
                    )

                    for doc in version_cursor:
                        _id = doc.get("_id")
                        if _id is not None:
                            obj_id_str = str(_id)
                            archived_version_ids.append(obj_id_str)
                            id_to_type_map[obj_id_str] = "version"
                            model_id_val = doc.get("modelId") if isinstance(doc.get("modelId"), dict) else None
                            if model_id_val is not None:
                                mid = model_id_val.get("value")
                                if mid is not None:
                                    model_to_versions.setdefault(str(mid), []).append(obj_id_str)
                                    # Map stored tag (e.g. modelId-v2) -> version_id for resolving registry tags like modelId-vX-timestamp-UID
                                    stored_tag = extract_model_tag_from_version_doc(doc)
                                    if stored_tag:
                                        model_tag_to_version.setdefault(str(mid), []).append((stored_tag, obj_id_str))

                    self.logger.info(f"Found {len(archived_version_ids)} model version ObjectIDs for archived models")

                all_archived_ids.extend(archived_model_ids)
                all_archived_ids.extend(archived_version_ids)

            # Remove duplicates while preserving order
            unique_ids = list(dict.fromkeys(all_archived_ids))

            # Filter out revisions/environments that depend on cloned revisions/environments not in deletion set
            if revision_to_cloned_revision:
                unique_ids, id_to_type_map = self._filter_cloned_dependencies(
                    unique_ids, id_to_type_map, revision_to_cloned_revision, revision_to_environment
                )

            self.logger.info(f"Total archived ObjectIDs to search for: {len(unique_ids)}")
            return unique_ids, id_to_type_map, environment_to_revisions, model_to_versions, model_tag_to_version

        finally:
            mongo_client.close()

    def _check_cloned_revision_chain(
        self, cloned_rev_id: str, archived_set: set, environment_revisions_collection, visited: set = None
    ) -> Tuple[bool, str]:
        """Recursively check if a cloned revision and its environment (and all their dependencies) are in deletion set.

        Args:
            cloned_rev_id: The cloned revision ID to check
            archived_set: Set of archived ObjectIDs
            environment_revisions_collection: MongoDB collection for environment_revisions
            visited: Set of already visited revision IDs to prevent infinite loops

        Returns:
            Tuple of (is_safe_to_delete, reason_if_not)
        """
        if visited is None:
            visited = set()

        # Prevent infinite loops
        if cloned_rev_id in visited:
            return True, ""  # Already checked, assume safe
        visited.add(cloned_rev_id)

        # Check if cloned revision is in deletion set
        if cloned_rev_id not in archived_set:
            return False, f"cloned revision {cloned_rev_id} not in deletion set"

        # Get cloned revision document to check its environment and any nested cloned revisions
        cloned_rev_doc = environment_revisions_collection.find_one(
            {"_id": ObjectId(cloned_rev_id)}, {"environmentId": 1, "clonedEnvironmentRevisionId": 1}
        )

        if not cloned_rev_doc:
            return False, f"cloned revision {cloned_rev_id} not found"

        # Check if cloned revision's environment is in deletion set
        cloned_env_id = cloned_rev_doc.get("environmentId")
        if cloned_env_id is not None:
            cloned_env_id_str = str(cloned_env_id)
            if cloned_env_id_str not in archived_set:
                return False, f"cloned environment {cloned_env_id_str} not in deletion set"

        # Check if cloned revision itself has a cloned revision (recursive dependency)
        nested_cloned_rev_id = cloned_rev_doc.get("clonedEnvironmentRevisionId")
        if nested_cloned_rev_id is not None:
            nested_cloned_rev_id_str = str(nested_cloned_rev_id)
            is_safe, reason = self._check_cloned_revision_chain(
                nested_cloned_rev_id_str, archived_set, environment_revisions_collection, visited
            )
            if not is_safe:
                return False, f"nested cloned revision dependency: {reason}"

        return True, ""

    def _filter_cloned_dependencies(
        self,
        archived_ids: List[str],
        id_to_type_map: Dict[str, str],
        revision_to_cloned_revision: Dict[str, str],
        revision_to_environment: Dict[str, str],
    ) -> Tuple[List[str], Dict[str, str]]:
        """Filter out revisions and environments that depend on cloned revisions/environments not in deletion set.

        If a revision has clonedEnvironmentRevisionId, it and its environment should only be deleted
        if the cloned revision and its environment (and all their dependencies) are also going to be deleted.

        Args:
            archived_ids: List of archived ObjectIDs
            id_to_type_map: Mapping of ObjectID to record type
            revision_to_cloned_revision: Mapping of revision ID to cloned revision ID
            revision_to_environment: Mapping of revision ID to environment ID

        Returns:
            Tuple of (filtered_archived_ids, filtered_id_to_type_map)
        """
        if not revision_to_cloned_revision:
            return archived_ids, id_to_type_map

        archived_set = set(archived_ids)
        ids_to_remove = set()

        # Need to look up environments for cloned revisions
        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            environment_revisions_collection = db["environment_revisions"]

            # For each revision with clonedEnvironmentRevisionId, check if cloned revision chain is safe to delete
            for rev_id, cloned_rev_id in revision_to_cloned_revision.items():
                is_safe, reason = self._check_cloned_revision_chain(
                    cloned_rev_id, archived_set, environment_revisions_collection
                )

                if not is_safe:
                    # Cloned revision chain is not safe to delete, so exclude this revision and its environment
                    ids_to_remove.add(rev_id)
                    env_id = revision_to_environment.get(rev_id)
                    if env_id:
                        ids_to_remove.add(env_id)
                    self.logger.info(f"Skipping revision {rev_id} ({reason})")
                else:
                    self.logger.debug(
                        f"Revision {rev_id} and cloned revision {cloned_rev_id} (and all dependencies) are in deletion set - OK to delete"
                    )
        finally:
            mongo_client.close()

        # Remove IDs that depend on cloned revisions/environments not in deletion set
        if ids_to_remove:
            filtered_ids = [oid for oid in archived_ids if oid not in ids_to_remove]
            filtered_id_to_type_map = {
                oid: record_type for oid, record_type in id_to_type_map.items() if oid not in ids_to_remove
            }
            removed_count = len(archived_ids) - len(filtered_ids)
            self.logger.info(
                f"Filtered out {removed_count} ObjectIDs (revisions/environments with cloned dependencies not in deletion set)"
            )
            return filtered_ids, filtered_id_to_type_map

        return archived_ids, id_to_type_map

    def get_in_use_environment_ids(self, env_ids: List[str], rev_ids: List[str]) -> Dict[str, bool]:
        """Check workspace and workspace_session collections for references to the given
        environment and environment revision IDs.

        Args:
            env_ids: Environment ObjectID strings
            rev_ids: Environment revision ObjectID strings

        Returns:
            Mapping of ObjectID string -> True if referenced (in use)
        """
        in_use: Dict[str, bool] = {}
        if not env_ids and not rev_ids:
            return in_use

        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]

            env_object_ids = [ObjectId(eid) for eid in env_ids] if env_ids else []
            rev_object_ids = [ObjectId(rid) for rid in rev_ids] if rev_ids else []

            # workspace: configTemplate.environmentId
            if env_object_ids:
                ws_cursor = db["workspace"].find(
                    {"configTemplate.environmentId": {"$in": env_object_ids}}, {"configTemplate.environmentId": 1}
                )
                for _ in ws_cursor:
                    # Mark all envs as possibly used; precise which one requires projection aggregation,
                    # but it's sufficient to mark env_ids as in-use if any doc matches
                    # We'll resolve exact IDs via separate queries below
                    pass

                # More precise: fetch distinct environmentId values
                used_env_ids = db["workspace"].distinct(
                    "configTemplate.environmentId", {"configTemplate.environmentId": {"$in": env_object_ids}}
                )
                for oid in used_env_ids:
                    in_use[str(oid)] = True

            # workspace_session: environmentId, config.environmentId, computeClusterEnvironmentId,
            # config.computeClusterProps.computeEnvironmentId
            if env_object_ids:
                ws_sess_env_query = {
                    "$or": [
                        {"environmentId": {"$in": env_object_ids}},
                        {"config.environmentId": {"$in": env_object_ids}},
                        {"computeClusterEnvironmentId": {"$in": env_object_ids}},
                        {"config.computeClusterProps.computeEnvironmentId": {"$in": env_object_ids}},
                    ]
                }
                used_env_ids = set()
                used_env_ids.update(db["workspace_session"].distinct("environmentId", ws_sess_env_query))
                used_env_ids.update(db["workspace_session"].distinct("config.environmentId", ws_sess_env_query))
                used_env_ids.update(db["workspace_session"].distinct("computeClusterEnvironmentId", ws_sess_env_query))
                used_env_ids.update(
                    db["workspace_session"].distinct(
                        "config.computeClusterProps.computeEnvironmentId", ws_sess_env_query
                    )
                )
                for oid in used_env_ids:
                    if oid is not None:
                        in_use[str(oid)] = True

            # workspace_session: environmentRevisionId, computeClusterEnvironmentRevisionId
            if rev_object_ids:
                ws_sess_rev_query = {
                    "$or": [
                        {"environmentRevisionId": {"$in": rev_object_ids}},
                        {"computeClusterEnvironmentRevisionId": {"$in": rev_object_ids}},
                    ]
                }
                used_rev_ids = set()
                used_rev_ids.update(db["workspace_session"].distinct("environmentRevisionId", ws_sess_rev_query))
                used_rev_ids.update(
                    db["workspace_session"].distinct("computeClusterEnvironmentRevisionId", ws_sess_rev_query)
                )
                for oid in used_rev_ids:
                    if oid is not None:
                        in_use[str(oid)] = True

            # userPreferences: defaultEnvironmentId
            # If a user has a defaultEnvironmentId set, we treat that environment (and its revisions)
            # as "in use" and skip it from deletion. Also log how many users reference each env.
            if env_object_ids and "userPreferences" in db.list_collection_names():
                user_prefs = db["userPreferences"]
                pipeline = [
                    {"$match": {"defaultEnvironmentId": {"$in": env_object_ids}}},
                    {"$group": {"_id": "$defaultEnvironmentId", "user_count": {"$sum": 1}}},
                ]
                pref_results = list(user_prefs.aggregate(pipeline))
                if pref_results:
                    # Map env ObjectId -> user_count
                    pref_env_ids = []
                    for doc in pref_results:
                        env_oid = doc.get("_id")
                        user_count = doc.get("user_count", 0)
                        if env_oid is not None:
                            env_id_str = str(env_oid)
                            in_use[env_id_str] = True
                            pref_env_ids.append(env_oid)
                            self.logger.info(
                                f"Environment {env_id_str} is set as defaultEnvironmentId "
                                f"for {user_count} user(s) in userPreferences; marking as in-use."
                            )

                    # Also mark all revisions for these environments as in use
                    if pref_env_ids:
                        revisions_collection = db["environment_revisions"]
                        rev_cursor = revisions_collection.find({"environmentId": {"$in": pref_env_ids}}, {"_id": 1})
                        rev_count = 0
                        for rev_doc in rev_cursor:
                            rev_id = rev_doc.get("_id")
                            if rev_id is not None:
                                in_use[str(rev_id)] = True
                                rev_count += 1
                        if rev_count:
                            self.logger.info(
                                f"Marked {rev_count} environment_revision IDs as in-use due to userPreferences defaults."
                            )

            return in_use
        finally:
            mongo_client.close()

    def list_tags_for_image(self, image: str) -> List[str]:
        """List tags for a specific image using skopeo"""
        ref = f"docker://{self.registry_url}/{self.repository}/{image}"
        output = self.skopeo_client.run_skopeo_command("list-tags", [ref])

        if not output:
            return []

        try:
            payload = json.loads(output)
            return payload.get("Tags", []) or []
        except json.JSONDecodeError:
            self.logger.error(f"Failed to parse list-tags output for {ref}")
            return []

    def find_matching_tags(
        self,
        archived_ids: List[str],
        id_to_type_map: Dict[str, str],
        environment_to_revisions: Optional[Dict[str, List[str]]] = None,
        model_to_versions: Optional[Dict[str, List[str]]] = None,
        model_tag_to_version: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    ) -> List[ArchivedTagInfo]:
        """Find Docker tags that contain archived ObjectIDs.

        When a tag matches an environment (or model) ID, we try to resolve the tag suffix
        to a specific revision (or version) so each tag maps 1:1 to a revision/version for reporting.
        Model tags in format <modelId>-vX-<timestamp>_<UID> are resolved using stored tags from
        model_versions (metadata.builds.slug.image.tag) and model_tags_match.
        """
        archived_set = set(archived_ids)
        environment_to_revisions = environment_to_revisions or {}
        model_to_versions = model_to_versions or {}
        model_tag_to_version = model_tag_to_version or {}
        matching_tags = []

        for image_type in self.image_types:
            self.logger.info(f"Scanning {image_type} images for archived ObjectIDs...")
            tags = self.list_tags_for_image(image_type)
            self.logger.info(f"  Found {len(tags)} tags in {image_type}")

            for tag in tags:
                for obj_id in archived_set:
                    # Use prefix matching (not substring) since tags format is: <objectid>-<version/revision>
                    if not (tag.startswith(obj_id + "-") or tag == obj_id):
                        continue
                    # Prefer revision/version so "object IDs with tags" is 1:1 with tags
                    record_type = id_to_type_map.get(obj_id, "unknown")
                    resolved_id = obj_id
                    if tag.startswith(obj_id + "-"):
                        suffix = tag[len(obj_id) + 1 :]
                        if record_type == "environment":
                            for rev_id in environment_to_revisions.get(obj_id, []):
                                if suffix == rev_id or suffix.startswith(rev_id + "-"):
                                    resolved_id = rev_id
                                    break
                        elif record_type == "model":
                            # Resolve using stored tags so format modelId-vX-timestamp-UID matches (model_tags_match)
                            for stored_tag, ver_id in model_tag_to_version.get(obj_id, []):
                                if model_tags_match(tag, stored_tag):
                                    resolved_id = ver_id
                                    break
                            if resolved_id == obj_id:
                                # Fallback: suffix might be version ObjectId (e.g. modelId-<versionId>-extra)
                                for ver_id in model_to_versions.get(obj_id, []):
                                    if suffix == ver_id or suffix.startswith(ver_id + "-"):
                                        resolved_id = ver_id
                                        break
                    full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                    tag_info = ArchivedTagInfo(
                        object_id=resolved_id,
                        image_type=image_type,
                        tag=tag,
                        full_image=full_image,
                        record_type=id_to_type_map.get(resolved_id, record_type),
                    )
                    matching_tags.append(tag_info)

        self.logger.info(f"Found {len(matching_tags)} matching tags for archived ObjectIDs")
        return matching_tags

    def calculate_freed_space(self, archived_tags: List[ArchivedTagInfo]) -> int:
        """Calculate total space that would be freed by deleting archived tags.

        This method uses ImageAnalyzer to properly account for shared layers.
        Only layers that would have no remaining references after deletion are counted.

        IMPORTANT: We must analyze ALL image types in the registry (environment and model),
        not just the types we're processing. Otherwise ref_count only counts references
        from one type, so layers shared with the other type are incorrectly counted as
        "freed" and we massively overestimate (e.g. 1.9 TB predicted vs 200 GB actual).
        """
        if not archived_tags:
            return 0

        try:
            self.logger.info("Analyzing ALL Docker images to calculate accurate freed space...")
            self.logger.info("Analyzing both environment and model images so shared layers are counted correctly.")

            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)

            # CRITICAL: Analyze ALL image types in the registry (environment AND model), not just
            # self.image_types. If we only analyze the type we're deleting (e.g. --environment),
            # layer ref_count only counts references from that type. Layers shared with model
            # images would then be wrongly counted as "freed", overestimating by a large margin.
            all_registry_image_types = ["environment", "model"]
            for image_type in all_registry_image_types:
                self.logger.info(f"Analyzing ALL {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=None)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")

            # Build list of image_ids from archived tags (deduplicate: same tag can appear
            # multiple times when it matches multiple archived IDs; deletion is per unique image)
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids_with_dupes = [f"{tag.image_type}:{tag.tag}" for tag in archived_tags]
            unique_image_ids = list(dict.fromkeys(image_ids_with_dupes))
            if len(unique_image_ids) < len(image_ids_with_dupes):
                self.logger.info(
                    f"Using {len(unique_image_ids)} unique images ({len(image_ids_with_dupes)} tag references) for freed space calculation"
                )

            # Calculate individual size for each tag (layers unique to that image)
            self.logger.info("Calculating individual image sizes...")
            for tag in archived_tags:
                image_id = f"{tag.image_type}:{tag.tag}"
                # Calculate what would be freed if only this image was deleted
                tag.size_bytes = analyzer.freed_space_if_deleted([image_id])

            # Calculate freed space using ImageAnalyzer's method (pass unique image_ids so
            # we don't undercount: duplicate ids would make delete_count > current_ref and
            # layers would never be counted as freed)
            total_freed = analyzer.freed_space_if_deleted(unique_image_ids)

            self.logger.info(f"Total space that would be freed: {sizeof_fmt(total_freed)}")

            return total_freed

        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            from utils.logging_utils import log_exception

            log_exception(self.logger, "Error calculating freed space", exc_info=e)
            return 0

    def delete_archived_tags(
        self,
        archived_tags: List[ArchivedTagInfo],
        backup: bool = False,
        s3_bucket: Optional[str] = None,
        region: str = "us-west-2",
        mongo_cleanup: bool = False,
        resume: bool = False,
        operation_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Delete archived Docker images and clean up MongoDB records

        Args:
            archived_tags: List of archived tags to delete
            backup: Whether to backup images to S3 before deletion
            s3_bucket: S3 bucket name for backups
            region: AWS region for S3 and ECR operations
        """
        if not archived_tags:
            self.logger.info("No archived tags to delete")
            return {}

        deletion_results = {"docker_images_deleted": 0, "mongo_records_cleaned": 0, "images_backed_up": 0}

        try:
            # Backup images to S3 if requested
            if backup and s3_bucket:
                self.logger.info(f"📦 Backing up {len(archived_tags)} images to S3 bucket: {s3_bucket}")

                # Prepare tags for backup_restore.process_backup
                tags_to_backup = [tag.tag for tag in archived_tags]
                full_repo = f"{self.registry_url}/{self.repository}"

                # Initialize ConfigManager and SkopeoClient for backup
                cfg_mgr = ConfigManager()
                backup_skopeo_client = SkopeoClient(cfg_mgr)

                # Call process_backup from backup_restore
                try:
                    process_backup(
                        skopeo_client=backup_skopeo_client,
                        full_repo=full_repo,
                        tags=tags_to_backup,
                        s3_bucket=s3_bucket,
                        region=region,
                        dry_run=False,
                        delete=False,  # Don't delete yet, we'll do that below
                        min_age_days=None,
                        workers=1,
                        tmpdir=None,
                        failed_tags_file=None,
                    )
                    deletion_results["images_backed_up"] = len(tags_to_backup)
                    self.logger.info(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
                except Exception as backup_err:
                    self.logger.error(f"❌ Backup failed: {backup_err}")
                    self.logger.error("Aborting deletion to prevent data loss")
                    raise

            # Enable deletion in registry if it's in the same Kubernetes cluster
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            registry_enabled = False
            if registry_in_cluster:
                registry_enabled = self.enable_registry_deletion()

            try:
                # Deduplicate tags before deletion (a tag may appear multiple times if it contains multiple ObjectIDs)
                # Build a mapping from unique tags to all their associated ObjectIDs
                unique_tags = {}  # key: (image_type, tag), value: list of ObjectIDs
                for tag_info in archived_tags:
                    key = (tag_info.image_type, tag_info.tag)
                    if key not in unique_tags:
                        unique_tags[key] = {"tag_info": tag_info, "object_ids": []}
                    unique_tags[key]["object_ids"].append(tag_info.object_id)

                # Check for existing checkpoint if resuming
                tag_identifiers = [f"{img_type}:{tag}" for (img_type, tag), _ in unique_tags.items()]
                if resume:
                    remaining_tags = self.checkpoint_manager.get_remaining_items(
                        "delete_archived_tags", tag_identifiers, operation_id
                    )
                    if remaining_tags:
                        self.logger.info(
                            f"📋 Resuming from checkpoint: {len(remaining_tags)} items remaining out of {len(tag_identifiers)} total"
                        )
                        # Filter unique_tags to only include remaining items
                        remaining_set = set(remaining_tags)
                        unique_tags = {k: v for k, v in unique_tags.items() if f"{k[0]}:{k[1]}" in remaining_set}
                    else:
                        self.logger.info("📋 Checkpoint found but all items are already completed")
                        return deletion_results
                else:
                    # Create initial checkpoint
                    self.checkpoint_manager.save_checkpoint(
                        "delete_archived_tags", [], len(tag_identifiers), operation_id=operation_id
                    )

                self.logger.info(
                    f"Deleting {len(unique_tags)} unique Docker images from registry ({len(archived_tags)} total references)..."
                )
                if len(unique_tags) < len(archived_tags):
                    self.logger.info(
                        f"  Note: {len(archived_tags) - len(unique_tags)} tags contain multiple archived ObjectIDs"
                    )

                deleted_count = 0
                failed_deletions = []
                failed_deletions_with_reason = {}  # Maps tag -> reason (usage info)
                successfully_deleted_object_ids = set()

                # Check which tags are in use before attempting deletion
                # Ensure MongoDB reports are fresh
                ensure_mongodb_reports()
                service = ImageUsageService()
                tags_to_check = [tag_info.tag for tag_info in archived_tags]
                in_use_tags, usage_info = service.check_tags_in_use(tags_to_check, recent_days=self.recent_days)

                if in_use_tags:
                    self.logger.warning(
                        f"⚠️  Found {len(in_use_tags)} tags that are currently in use - these will be skipped"
                    )
                    for tag in in_use_tags:
                        usage = usage_info.get(tag, {})
                        usage_summary = service.generate_usage_summary(usage)
                        self.logger.warning(f"  • {tag}: {usage_summary}")
                        failed_deletions_with_reason[tag] = {
                            "reason": "in_use",
                            "usage_summary": usage_summary,
                            "usage": usage,
                        }

                # Parallelize deletion operations
                import concurrent.futures

                max_workers = min(self.max_workers, len(unique_tags), 10)  # Cap at 10 to avoid overwhelming registry

                def delete_single_tag(tag_key_data):
                    """Delete a single tag and return result"""
                    (image_type, tag), data = tag_key_data
                    tag_info = data["tag_info"]
                    associated_object_ids = data["object_ids"]

                    # Skip if tag is in use
                    if tag_info.tag in in_use_tags:
                        usage = usage_info.get(tag_info.tag, {})
                        usage_summary = service.generate_usage_summary(usage)
                        self.logger.warning(f"  Skipping {tag_info.full_image} (in use: {usage_summary})")
                        return ("skipped_in_use", tag_info.full_image, None, usage_summary)

                    try:
                        self.logger.info(f"  Deleting: {tag_info.full_image}")
                        success = self.skopeo_client.delete_image(
                            f"{self.repository}/{tag_info.image_type}", tag_info.tag
                        )
                        if success:
                            if len(associated_object_ids) > 1:
                                self.logger.info(
                                    f"    ✓ Deleted successfully (contains {len(associated_object_ids)} archived ObjectIDs)"
                                )
                            else:
                                self.logger.info(f"    ✓ Deleted successfully")
                            return ("success", tag_info.full_image, associated_object_ids, None)
                        else:
                            self.logger.warning(f"    ✗ Failed to delete - MongoDB record will NOT be cleaned")
                            return ("failed", tag_info.full_image, None, None)
                    except Exception as e:
                        self.logger.error(f"    ✗ Error deleting: {e} - MongoDB record will NOT be cleaned")
                        return ("failed", tag_info.full_image, None, None)

                # Process deletions in parallel
                if max_workers > 1 and len(unique_tags) > 1:
                    self.logger.info(f"Deleting {len(unique_tags)} images using {max_workers} parallel workers...")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_tag = {executor.submit(delete_single_tag, item): item for item in unique_tags.items()}

                        completed_items = []
                        failed_items = []
                        skipped_items = []

                        for future in concurrent.futures.as_completed(future_to_tag):
                            try:
                                result = future.result()
                                status, full_image, object_ids, usage_summary = result
                                if status == "success":
                                    deleted_count += 1
                                    successfully_deleted_object_ids.update(object_ids)
                                    completed_items.append(full_image)
                                elif status == "skipped_in_use":
                                    failed_deletions.append(full_image)
                                    skipped_items.append(full_image)
                                    failed_deletions_with_reason[full_image] = {
                                        "reason": "in_use",
                                        "usage_summary": usage_summary,
                                    }
                                else:
                                    failed_deletions.append(full_image)
                                    failed_items.append(full_image)
                                    failed_deletions_with_reason[full_image] = {"reason": "deletion_failed"}

                                # Save checkpoint periodically (every 10 items)
                                total_processed = len(completed_items) + len(failed_items) + len(skipped_items)
                                if total_processed % 10 == 0:
                                    self.checkpoint_manager.save_checkpoint(
                                        "delete_archived_tags",
                                        completed_items,
                                        len(tag_identifiers),
                                        failed_items=failed_items,
                                        skipped_items=skipped_items,
                                        operation_id=operation_id,
                                    )
                            except Exception as e:
                                self.logger.error(f"Error processing deletion result: {e}")

                        # Final checkpoint save
                        self.checkpoint_manager.save_checkpoint(
                            "delete_archived_tags",
                            completed_items,
                            len(tag_identifiers),
                            failed_items=failed_items,
                            skipped_items=skipped_items,
                            operation_id=operation_id,
                        )
                else:
                    # Sequential deletion for small batches or single worker
                    completed_items = []
                    failed_items = []
                    skipped_items = []

                    for idx, ((image_type, tag), data) in enumerate(unique_tags.items()):
                        result = delete_single_tag(((image_type, tag), data))
                        status, full_image, object_ids, usage_summary = result
                        if status == "success":
                            deleted_count += 1
                            successfully_deleted_object_ids.update(object_ids)
                            completed_items.append(full_image)
                        elif status == "skipped_in_use":
                            failed_deletions.append(full_image)
                            skipped_items.append(full_image)
                            failed_deletions_with_reason[full_image] = {
                                "reason": "in_use",
                                "usage_summary": usage_summary,
                            }
                        else:
                            failed_deletions.append(full_image)
                            failed_items.append(full_image)
                            failed_deletions_with_reason[full_image] = {"reason": "deletion_failed"}

                        # Save checkpoint periodically (every 10 items)
                        if (idx + 1) % 10 == 0:
                            self.checkpoint_manager.save_checkpoint(
                                "delete_archived_tags",
                                completed_items,
                                len(tag_identifiers),
                                failed_items=failed_items,
                                skipped_items=skipped_items,
                                operation_id=operation_id,
                            )

                    # Final checkpoint save
                    self.checkpoint_manager.save_checkpoint(
                        "delete_archived_tags",
                        completed_items,
                        len(tag_identifiers),
                        failed_items=failed_items,
                        skipped_items=skipped_items,
                        operation_id=operation_id,
                    )

                deletion_results["docker_images_deleted"] = deleted_count

                if failed_deletions:
                    in_use_count = sum(1 for r in failed_deletions_with_reason.values() if r.get("reason") == "in_use")
                    failed_count = len(failed_deletions) - in_use_count

                    self.logger.warning(f"Could not delete {len(failed_deletions)} Docker images:")
                    if in_use_count > 0:
                        self.logger.warning(f"  {in_use_count} image(s) are currently in use:")
                        for img in failed_deletions:
                            if img in failed_deletions_with_reason:
                                reason_info = failed_deletions_with_reason[img]
                                if reason_info.get("reason") == "in_use":
                                    usage_summary = reason_info.get("usage_summary", "Unknown usage")
                                    self.logger.warning(f"    • {img}: {usage_summary}")
                    if failed_count > 0:
                        self.logger.warning(f"  {failed_count} image(s) failed to delete:")
                        for img in failed_deletions:
                            if (
                                img not in failed_deletions_with_reason
                                or failed_deletions_with_reason[img].get("reason") != "in_use"
                            ):
                                self.logger.warning(f"    • {img}")
                    self.logger.warning("MongoDB records for failed deletions will be preserved.")

                # Clean up MongoDB records - ONLY for successfully deleted Docker images
                if mongo_cleanup:
                    # Group ObjectIDs by their record type
                    ids_by_type = {}
                    for tag in archived_tags:
                        if tag.object_id in successfully_deleted_object_ids:
                            record_type = tag.record_type
                            if record_type not in ids_by_type:
                                ids_by_type[record_type] = []
                            ids_by_type[record_type].append(tag.object_id)

                    # Remove duplicates
                    for record_type in ids_by_type:
                        ids_by_type[record_type] = list(set(ids_by_type[record_type]))

                    if ids_by_type:
                        mongo_client = get_mongo_client()

                        try:
                            db = mongo_client[config_manager.get_mongo_db()]

                            # Clean up environment records
                            if "environment" in ids_by_type:
                                environments_collection = db["environments_v2"]
                                revisions_collection_for_envs = db["environment_revisions"]
                                models_collection_for_envs = db["models"]
                                self.logger.info(
                                    f"Cleaning up {len(ids_by_type['environment'])} environment records from MongoDB..."
                                )
                                for obj_id_str in ids_by_type["environment"]:
                                    try:
                                        obj_id = ObjectId(obj_id_str)
                                        # Only delete environment if there are no remaining revisions referencing it
                                        remaining_revs = revisions_collection_for_envs.count_documents(
                                            {"environmentId": obj_id}, limit=1
                                        )
                                        if remaining_revs and remaining_revs > 0:
                                            self.logger.info(
                                                f"  ↪ Skipping environment {obj_id_str} (has remaining environment_revisions)"
                                            )
                                            continue
                                        # Ensure no non-archived models reference this environment
                                        referencing_models = models_collection_for_envs.count_documents(
                                            {"isArchived": False, "environmentId": obj_id}, limit=1
                                        )
                                        if referencing_models and referencing_models > 0:
                                            self.logger.info(
                                                f"  ↪ Skipping environment {obj_id_str} (referenced by non-archived models)"
                                            )
                                            continue
                                        result = environments_collection.delete_one({"_id": obj_id})
                                        if result.deleted_count > 0:
                                            self.logger.info(f"  ✓ Deleted environment: {obj_id_str}")
                                            deletion_results["mongo_records_cleaned"] += 1
                                        else:
                                            self.logger.warning(f"  ✗ Environment not found: {obj_id_str}")
                                    except Exception as e:
                                        self.logger.error(f"  ✗ Error deleting environment {obj_id_str}: {e}")

                            # Clean up environment revision records
                            if "revision" in ids_by_type:
                                environment_revisions_collection = db["environment_revisions"]
                                model_versions_collection = db["model_versions"]
                                models_collection = db["models"]
                                self.logger.info(
                                    f"Cleaning up {len(ids_by_type['revision'])} environment_revision records from MongoDB..."
                                )
                                for obj_id_str in ids_by_type["revision"]:
                                    try:
                                        obj_id = ObjectId(obj_id_str)
                                        # Ensure no model_versions from unarchived models reference this environment revision
                                        # Find distinct model IDs that reference this revision
                                        model_ids = model_versions_collection.distinct(
                                            "modelId.value", {"environmentRevisionId": obj_id}
                                        )
                                        if model_ids:
                                            # Check if any of those models are unarchived
                                            unarchived_ref = models_collection.count_documents(
                                                {"_id": {"$in": model_ids}, "isArchived": False}, limit=1
                                            )
                                            if unarchived_ref and unarchived_ref > 0:
                                                self.logger.info(
                                                    f"  ↪ Skipping environment_revision {obj_id_str} (referenced by versions of unarchived models)"
                                                )
                                                continue
                                        result = environment_revisions_collection.delete_one({"_id": obj_id})
                                        if result.deleted_count > 0:
                                            self.logger.info(f"  ✓ Deleted environment_revision: {obj_id_str}")
                                            deletion_results["mongo_records_cleaned"] += 1
                                        else:
                                            self.logger.warning(f"  ✗ Environment_revision not found: {obj_id_str}")
                                    except Exception as e:
                                        self.logger.error(f"  ✗ Error deleting environment_revision {obj_id_str}: {e}")

                            # Clean up model records
                            if "model" in ids_by_type:
                                models_collection = db["models"]
                                versions_collection_for_models = db["model_versions"]
                                self.logger.info(
                                    f"Cleaning up {len(ids_by_type['model'])} model records from MongoDB..."
                                )
                                for obj_id_str in ids_by_type["model"]:
                                    try:
                                        obj_id = ObjectId(obj_id_str)
                                        # Only delete model if there are no remaining versions referencing it
                                        remaining_versions = versions_collection_for_models.count_documents(
                                            {"modelId.value": obj_id}, limit=1
                                        )
                                        if remaining_versions and remaining_versions > 0:
                                            self.logger.info(
                                                f"  ↪ Skipping model {obj_id_str} (has remaining model_versions)"
                                            )
                                            continue
                                        result = models_collection.delete_one({"_id": obj_id})
                                        if result.deleted_count > 0:
                                            self.logger.info(f"  ✓ Deleted model: {obj_id_str}")
                                            deletion_results["mongo_records_cleaned"] += 1
                                        else:
                                            self.logger.warning(f"  ✗ Model not found: {obj_id_str}")
                                    except Exception as e:
                                        self.logger.error(f"  ✗ Error deleting model {obj_id_str}: {e}")

                            # Clean up model version records
                            if "version" in ids_by_type:
                                model_versions_collection = db["model_versions"]
                                self.logger.info(
                                    f"Cleaning up {len(ids_by_type['version'])} model_version records from MongoDB..."
                                )
                                for obj_id_str in ids_by_type["version"]:
                                    try:
                                        obj_id = ObjectId(obj_id_str)
                                        result = model_versions_collection.delete_one({"_id": obj_id})
                                        if result.deleted_count > 0:
                                            self.logger.info(f"  ✓ Deleted model_version: {obj_id_str}")
                                            deletion_results["mongo_records_cleaned"] += 1
                                        else:
                                            self.logger.warning(f"  ✗ Model_version not found: {obj_id_str}")
                                    except Exception as e:
                                        self.logger.error(f"  ✗ Error deleting model_version {obj_id_str}: {e}")
                        finally:
                            mongo_client.close()
                else:
                    self.logger.info("Skipping MongoDB cleanup (use --mongo-cleanup to enable)")

                self.logger.info("Archived tag deletion completed successfully")

            finally:
                # Always disable deletion in registry if it was enabled
                if registry_enabled:
                    self.disable_registry_deletion()

                # Clean up checkpoint if operation completed successfully
                total_processed = deletion_results.get("docker_images_deleted", 0) + len(failed_deletions)
                if total_processed > 0:
                    # Check if all items were processed
                    checkpoint = self.checkpoint_manager.load_checkpoint("delete_archived_tags", operation_id)
                    if checkpoint:
                        total_completed = (
                            len(checkpoint.completed_items)
                            + len(checkpoint.failed_items)
                            + len(checkpoint.skipped_items)
                        )
                        if total_completed >= checkpoint.total_items:
                            # Operation completed - delete checkpoint
                            self.checkpoint_manager.delete_checkpoint("delete_archived_tags", operation_id)
                            self.logger.info("✓ Checkpoint cleaned up (operation completed)")

        except Exception as e:
            self.logger.error(f"Error deleting archived tags: {e}")
            self.logger.info("💾 Progress saved to checkpoint - use --resume to continue")
            raise

        return deletion_results

    def generate_report(
        self,
        archived_ids: List[str],
        archived_tags: List[ArchivedTagInfo],
        id_to_type_map: Dict[str, str],
        freed_space_bytes: int = 0,
        environment_to_revisions: Optional[Dict[str, List[str]]] = None,
        model_to_versions: Optional[Dict[str, List[str]]] = None,
    ) -> Dict:
        """Generate a comprehensive report of archived tags

        Args:
            archived_ids: List of all archived ObjectIDs
            archived_tags: List of archived tag info objects
            id_to_type_map: Mapping of ObjectID to record type
            freed_space_bytes: Total bytes that would be freed by deletion (accounts for shared layers)
            environment_to_revisions: Optional map environment ID -> list of revision IDs (for tag mapping)
            model_to_versions: Optional map model ID -> list of version IDs (for tag mapping)
        """
        environment_to_revisions = environment_to_revisions or {}
        model_to_versions = model_to_versions or {}

        # Categorize IDs by type
        ids_by_type = {"environment": [], "revision": [], "model": [], "version": []}

        for obj_id in archived_ids:
            record_type = id_to_type_map.get(obj_id, "unknown")
            if record_type in ids_by_type:
                ids_by_type[record_type].append(obj_id)

        # Group tags by ObjectID and image type
        by_object_id = {}
        by_image_type = {}
        for img_type in self.image_types:
            by_image_type[img_type] = 0

        for tag in archived_tags:
            obj_id = tag.object_id
            if obj_id not in by_object_id:
                by_object_id[obj_id] = []
            by_object_id[obj_id].append(tag)
            if tag.image_type in by_image_type:
                by_image_type[tag.image_type] += 1

        # ObjectIDs with tags: count only revisions and versions that have matching tags.
        # Tags may be keyed by environment/model ID or by revision/version ID; map parent IDs
        # to their revisions/versions so we count 1:1 with versions and revisions.
        revision_ids_with_tags: Set[str] = set()
        version_ids_with_tags: Set[str] = set()
        for obj_id in by_object_id:
            record_type = id_to_type_map.get(obj_id, "unknown")
            if record_type == "revision":
                revision_ids_with_tags.add(obj_id)
            elif record_type == "version":
                version_ids_with_tags.add(obj_id)
            elif record_type == "environment":
                revision_ids_with_tags.update(environment_to_revisions.get(obj_id, []))
            elif record_type == "model":
                version_ids_with_tags.update(model_to_versions.get(obj_id, []))
        total_revisions_and_versions = len(ids_by_type["revision"]) + len(ids_by_type["version"])
        object_ids_with_tags = len(revision_ids_with_tags) + len(version_ids_with_tags)
        object_ids_without_tags = total_revisions_and_versions - object_ids_with_tags

        summary = {
            "total_archived_object_ids": len(archived_ids),
            "archived_environment_ids": len(ids_by_type["environment"]),
            "archived_revision_ids": len(ids_by_type["revision"]),
            "archived_model_ids": len(ids_by_type["model"]),
            "archived_version_ids": len(ids_by_type["version"]),
            "total_matching_tags": len(archived_tags),
            "freed_space_gb": round(freed_space_bytes / (1024 * 1024 * 1024), 2),
            "tags_by_image_type": by_image_type,
            "object_ids_with_tags": object_ids_with_tags,
            "object_ids_without_tags": object_ids_without_tags,
        }

        # Prepare detailed data
        # Enrich with usage information (runs, workspaces, models, projects, etc.)
        service = ImageUsageService()
        mongodb_reports = service.load_mongodb_usage_reports()

        # Log report statistics for debugging
        self.logger.info(f"Loaded MongoDB usage reports:")
        for key, value in mongodb_reports.items():
            count = len(value) if isinstance(value, list) else 0
            self.logger.info(f"  {key}: {count} records")

        _, usage_info = service.extract_docker_tags_with_usage_info(mongodb_reports)
        self.logger.info(f"Extracted usage info for {len(usage_info)} unique tags")

        # Build a prefix index for faster tag matching
        # Maps tag prefix (ObjectID part) to list of (full_tag, usage_data) tuples
        prefix_index = {}
        for usage_tag, usage_data in usage_info.items():
            # Extract ObjectID prefix (everything before the first '-' after ObjectID)
            # For tags like "507f1f77bcf86cd799439011-v2" or "507f1f77bcf86cd799439011-v2-1234567890_abc123"
            parts = usage_tag.split("-", 1)
            if len(parts) >= 1:
                prefix = parts[0]  # ObjectID part
                if prefix not in prefix_index:
                    prefix_index[prefix] = []
                prefix_index[prefix].append((usage_tag, usage_data))

        detailed_tags = []

        for tag in archived_tags:
            # Try exact match first
            raw_usage = usage_info.get(tag.tag)

            # If no exact match, try prefix matching for extended tag formats
            # This handles cases where registry has extended tags like <objectId>-<version>-<timestamp>_<uniqueId>
            # but MongoDB reports have simpler tags like <objectId>-<version>
            if not raw_usage:
                # Extract ObjectID prefix from registry tag
                tag_parts = tag.tag.split("-", 1)
                if len(tag_parts) >= 1:
                    tag_prefix = tag_parts[0]
                    # Look up all usage tags with the same ObjectID prefix
                    matching_usage_tags = prefix_index.get(tag_prefix, [])
                    for usage_tag, usage_data in matching_usage_tags:
                        # Check if tags match (registry tag starts with usage tag or vice versa)
                        if tag.tag.startswith(usage_tag + "-") or usage_tag.startswith(tag.tag + "-"):
                            # Found a prefix match - use this usage data
                            raw_usage = {
                                "runs": list(usage_data.get("runs", [])),
                                "workspaces": list(usage_data.get("workspaces", [])),
                                "models": list(usage_data.get("models", [])),
                                "scheduler_jobs": list(usage_data.get("scheduler_jobs", [])),
                                "projects": list(usage_data.get("projects", [])),
                                "organizations": list(usage_data.get("organizations", [])),
                                "app_versions": list(usage_data.get("app_versions", [])),
                            }
                            break

            # Initialize empty usage if still no match
            if not raw_usage:
                raw_usage = {
                    "runs": [],
                    "workspaces": [],
                    "models": [],
                    "scheduler_jobs": [],
                    "projects": [],
                    "organizations": [],
                    "app_versions": [],
                }

            runs = raw_usage.get("runs", [])
            workspaces = raw_usage.get("workspaces", [])
            models = raw_usage.get("models", [])
            scheduler_jobs = raw_usage.get("scheduler_jobs", [])
            projects = raw_usage.get("projects", [])
            organizations = raw_usage.get("organizations", [])
            app_versions = raw_usage.get("app_versions", [])

            # Build usage block similar to delete_image: include counts and truncate long lists
            usage_for_report = {
                "runs_count": len(runs),
                "runs": runs[:5],
                "workspaces_count": len(workspaces),
                "workspaces": workspaces[:5],
                "models_count": len(models),
                "models": models[:5],
                "scheduler_jobs": scheduler_jobs,
                "projects": projects,
                "organizations": organizations,
                "app_versions": app_versions,
            }

            # Human-readable summary and simple status flag
            usage_summary = self._generate_usage_summary(usage_for_report)

            # Note: For archived environments, workspaces_count and models_count are typically 0
            # because:
            # 1. Environments with active workspaces are filtered out by get_in_use_environment_ids()
            # 2. Archived environments are not used by models
            # Historical runs may still exist, which would mark the tag as "in_use"
            is_in_use = (
                usage_for_report["runs_count"] > 0
                or usage_for_report["workspaces_count"] > 0
                or usage_for_report["models_count"] > 0
                or len(scheduler_jobs) > 0
                or len(projects) > 0
                or len(organizations) > 0
                or len(app_versions) > 0
            )
            status = "in_use" if is_in_use else "unused"

            detailed_tags.append(
                {
                    "object_id": tag.object_id,
                    "image_type": tag.image_type,
                    "tag": tag.tag,
                    "full_image": tag.full_image,
                    "size_bytes": tag.size_bytes,
                    "size_gb": round(tag.size_bytes / (1024 * 1024 * 1024), 2) if tag.size_bytes > 0 else 0.0,
                    "status": status,
                    "usage": usage_for_report,
                    "usage_summary": usage_summary,
                }
            )

        # Report structure: keep summary (counts only), archived_tags, and metadata
        # Remove large ObjectID lists and redundant grouped_by_object_id (not needed for deletion)
        report = {
            "summary": summary,
            "archived_tags": detailed_tags,
            "metadata": {
                "registry_url": self.registry_url,
                "repository": self.repository,
                "image_types_scanned": self.image_types,
                "process_environments": self.process_environments,
                "process_models": self.process_models,
                "analysis_timestamp": datetime.now().isoformat(),
            },
        }

        return report

    def load_archived_tags_from_file(self, file_path: str) -> List[ArchivedTagInfo]:
        """Load archived tags from a pre-generated report file"""
        try:
            with open(file_path, "r") as f:
                report = json.load(f)

            archived_tags = []
            for tag_data in report.get("archived_tags", []):
                tag = ArchivedTagInfo(
                    object_id=tag_data["object_id"],
                    image_type=tag_data["image_type"],
                    tag=tag_data["tag"],
                    full_image=tag_data["full_image"],
                    size_bytes=tag_data.get("size_bytes", 0),
                    record_type=tag_data.get(
                        "record_type", tag_data["image_type"]
                    ),  # Default to image_type if not present
                )
                archived_tags.append(tag)

            self.logger.info(f"Loaded {len(archived_tags)} archived tags from {file_path}")
            return archived_tags

        except Exception as e:
            self.logger.error(f"Error loading archived tags from {file_path}: {e}")
            raise


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Find and optionally delete archived tags in Docker registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find archived environment tags (dry-run)
  python delete_archived_tags.py --environment

  # Find archived model tags (dry-run)
  python delete_archived_tags.py --model

  # Find both archived environments and models
  python delete_archived_tags.py --environment --model

  # Override registry settings
  python delete_archived_tags.py --environment --registry-url registry.example.com --repository my-repo

  # Custom output file
  python delete_archived_tags.py --environment --output archived-tags.json

  # Delete archived environment tags directly (requires confirmation)
  python delete_archived_tags.py --environment --apply

  # Delete archived tags from pre-generated file
  python delete_archived_tags.py --environment --apply --input archived-tags.json

  # Force deletion without confirmation
  python delete_archived_tags.py --environment --apply --force
        """,
    )

    parser.add_argument(
        "--environment", action="store_true", help="Process archived environments and environment revisions"
    )

    parser.add_argument("--model", action="store_true", help="Process archived models and model versions")

    parser.add_argument("--registry-url", help="Docker registry URL (default: from config)")

    parser.add_argument("--repository", help="Repository name (default: from config)")

    parser.add_argument("--output", help="Output file path (default: reports/archived-tags.json)")

    parser.add_argument("--apply", action="store_true", help="Actually delete archived tags (default: dry-run)")

    parser.add_argument("--input", help="Input file containing pre-generated archived tags to delete")

    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")

    parser.add_argument(
        "--backup", action="store_true", help="Backup images to S3 before deletion (requires --s3-bucket)"
    )

    parser.add_argument(
        "--s3-bucket", help="S3 bucket for backups (optional if configured in config.yaml or S3_BUCKET env var)"
    )

    parser.add_argument("--region", help="AWS region for S3 and ECR (default: from config or us-west-2)")

    parser.add_argument(
        "--enable-docker-deletion",
        action="store_true",
        help="Enable registry deletion by treating registry as in-cluster (overrides auto-detection)",
    )

    parser.add_argument(
        "--registry-statefulset",
        default="docker-registry",
        help="Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)",
    )

    parser.add_argument(
        "--mongo-cleanup",
        action="store_true",
        help="Also clean up MongoDB records after Docker image deletion (advanced / high-risk; see README)",
    )

    parser.add_argument(
        "--run-registry-gc",
        action="store_true",
        help="Run Docker registry garbage collection in the registry pod after deleting tags (internal registries only)",
    )

    parser.add_argument(
        "--resume", action="store_true", help="Resume from previous checkpoint if operation was interrupted"
    )

    parser.add_argument(
        "--operation-id", help="Unique identifier for this operation run (used for checkpoint management)"
    )
    parser.add_argument(
        "--unused-since-days",
        dest="days",
        type=int,
        metavar="N",
        help='Only consider images as "in-use" if they were used in a workload within the last N days. '
        "If the last usage was more than N days ago, the image will be considered "
        "unused and eligible for deletion. If omitted, any historical usage marks the image as in-use. "
        "This filters based on the last_used, completed, or started timestamp from runs, "
        "and workspace_last_change from workspaces.",
    )

    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()

    # Validate that at least one type is specified
    if not args.environment and not args.model:
        logger.error("❌ Error: Must specify at least one of --environment or --model")
        sys.exit(1)

    # Get S3 configuration from args or config
    s3_bucket = args.s3_bucket or config_manager.get_s3_bucket()
    s3_region = args.region or config_manager.get_s3_region()

    # Validate backup arguments
    if args.backup and not s3_bucket:
        logger.error("❌ Error: --s3-bucket is required when --backup is set")
        logger.error("   You can provide it via --s3-bucket flag, S3_BUCKET env var, or config.yaml")
        sys.exit(1)

    # Get configuration
    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    output_file = args.output or config_manager.get_archived_tags_report_path()

    try:
        # Determine operation mode
        is_delete_mode = args.apply
        use_input_file = args.input is not None

        # Determine what's being processed
        processing_types = []
        if args.environment:
            processing_types.append("environments")
        if args.model:
            processing_types.append("models")
        processing_str = " and ".join(processing_types)

        logger.info("=" * 60)
        if is_delete_mode:
            logger.info(f"   🗑️  DELETION MODE: Deleting archived {processing_str} tags")
            logger.warning("⚠️  Images WILL be deleted from the registry!")
        else:
            logger.info(f"   🔍 DRY RUN MODE (default): Finding archived {processing_str} tags")
            logger.info("   No images will be deleted. Use --apply to actually delete images.")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")

        if use_input_file:
            logger.info(f"Input file: {args.input}")
        else:
            logger.info(f"Output file: {output_file}")

        # Create finder
        finder = ArchivedTagsFinder(
            registry_url,
            repository,
            process_environments=args.environment,
            process_models=args.model,
            enable_docker_deletion=args.enable_docker_deletion,
            registry_statefulset=args.registry_statefulset,
            recent_days=args.days,
        )

        # Handle different operation modes
        if use_input_file:
            # Mode 1: Delete from pre-generated file
            logger.info(f"Loading archived tags from {args.input}...")
            archived_tags = finder.load_archived_tags_from_file(args.input)
            archived_ids = []  # Not relevant for deletion mode
            id_to_type_map = {}

            if not archived_tags:
                logger.warning(f"No archived tags found in {args.input}")
                sys.exit(0)

        else:
            # Mode 2: Find archived tags (and optionally delete them)
            logger.info("Fetching archived ObjectIDs from MongoDB...")
            archived_ids, id_to_type_map, environment_to_revisions, model_to_versions, model_tag_to_version = (
                finder.fetch_archived_object_ids()
            )

            if not archived_ids:
                logger.info(f"No archived {processing_str} ObjectIDs found")
                # Still create an empty report
                empty_report = {
                    "summary": {
                        "total_archived_object_ids": 0,
                        "archived_environment_ids": 0,
                        "archived_revision_ids": 0,
                        "archived_model_ids": 0,
                        "archived_version_ids": 0,
                        "total_matching_tags": 0,
                        "freed_space_gb": 0,
                        "tags_by_image_type": {img_type: 0 for img_type in finder.image_types},
                        "object_ids_with_tags": 0,
                        "object_ids_without_tags": 0,
                    },
                    "archived_tags": [],
                    "metadata": {
                        "registry_url": registry_url,
                        "repository": repository,
                        "image_types_scanned": finder.image_types,
                        "process_environments": args.environment,
                        "process_models": args.model,
                        "analysis_timestamp": datetime.now().isoformat(),
                    },
                }
                save_json(output_file, empty_report)
                logger.info(f"Empty report written to {output_file}")
                sys.exit(0)

            # Filter out archived environment/revision IDs that are still in use
            # NOTE: This filters out environments that are CURRENTLY referenced by workspaces/sessions.
            # After this filter, remaining archived environments should have:
            # - workspaces_count = 0 (no active workspaces using them)
            # - models_count = 0 (archived environments aren't used by models)
            # - But may still have historical runs_count > 0 (past usage)
            env_ids = [oid for oid in archived_ids if id_to_type_map.get(oid) == "environment"]
            rev_ids = [oid for oid in archived_ids if id_to_type_map.get(oid) == "revision"]
            in_use_map = finder.get_in_use_environment_ids(env_ids, rev_ids)
            if in_use_map:
                # Keep IDs that are not in use
                before = len(archived_ids)
                archived_ids = [oid for oid in archived_ids if oid not in in_use_map]
                after = len(archived_ids)
                skipped = before - after
                logger.info(
                    f"Skipping {skipped} archived environment/revision ObjectIDs still referenced by workspaces/sessions"
                )
                # Also remove from id_to_type_map
                for oid in list(id_to_type_map.keys()):
                    if oid not in archived_ids:
                        id_to_type_map.pop(oid, None)

            logger.info("Finding matching Docker tags...")
            archived_tags = finder.find_matching_tags(
                archived_ids,
                id_to_type_map,
                environment_to_revisions=environment_to_revisions,
                model_to_versions=model_to_versions,
                model_tag_to_version=model_tag_to_version,
            )

            if not archived_tags:
                logger.info("No matching Docker tags found for archived ObjectIDs")
                # Still create a report with the ObjectIDs but no tags
                report = finder.generate_report(
                    archived_ids,
                    [],
                    id_to_type_map,
                    freed_space_bytes=0,
                    environment_to_revisions=environment_to_revisions,
                    model_to_versions=model_to_versions,
                )
                save_json(output_file, report)
                logger.info(f"Report written to {output_file}")
                sys.exit(0)

        # Backup-only mode: allow backing up without deletion when --backup is provided without --apply
        if (not is_delete_mode) and args.backup:
            if not archived_tags:
                logger.info("No archived tags to back up")
                sys.exit(0)

            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(
                    f"\n⚠️  WARNING: About to back up {len(archived_tags)} archived {processing_str} tags to S3!"
                )
                logger.warning("This will upload tar archives to your configured S3 bucket.")
                response = input("\nProceed with backup only (no deletions)? (yes/no): ").strip().lower()
                if response not in ["yes", "y"]:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)

            # Execute backup only
            logger.info(f"\n📦 Backing up {len(archived_tags)} archived {processing_str} tags to S3 (no deletion)...")
            tags_to_backup = [t.tag for t in archived_tags]
            full_repo = f"{registry_url}/{repository}"

            cfg_mgr = ConfigManager()
            backup_skopeo_client = SkopeoClient(cfg_mgr)

            try:
                process_backup(
                    skopeo_client=backup_skopeo_client,
                    full_repo=full_repo,
                    tags=tags_to_backup,
                    s3_bucket=s3_bucket,
                    region=s3_region,
                    dry_run=False,
                    delete=False,
                    min_age_days=None,
                    workers=1,
                    tmpdir=None,
                    failed_tags_file=None,
                )
                logger.info(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as e:
                logger.error(f"❌ Backup failed: {e}")
                sys.exit(1)

            logger.info("\n✅ Backup-only operation completed successfully!")
            sys.exit(0)

        # Handle deletion mode
        if is_delete_mode:
            if not archived_tags:
                logger.info("No archived tags to delete")
                sys.exit(0)

            # Deletion works on unique (image_type, tag); same tag can appear multiple times if it matched multiple archived IDs
            unique_image_count = len(set((t.image_type, t.tag) for t in archived_tags))
            if unique_image_count < len(archived_tags):
                logger.info(
                    f"Found {len(archived_tags)} tag references → {unique_image_count} unique Docker images to delete"
                )

            # Confirmation prompt (unless --force) — confirm on unique image count (what will actually be deleted)
            if not finder.confirm_deletion(
                unique_image_count, f"unique archived {processing_str} Docker images", force=args.force
            ):
                logger.info("Operation cancelled by user")
                sys.exit(0)

            logger.info(f"\n🗑️  Deleting {unique_image_count} unique archived {processing_str} Docker images...")
            # Generate operation ID if not provided
            operation_id = args.operation_id or get_timestamp_suffix()

            deletion_results = finder.delete_archived_tags(
                archived_tags,
                backup=args.backup,
                s3_bucket=s3_bucket,
                region=s3_region,
                mongo_cleanup=args.mongo_cleanup,
                resume=args.resume,
                operation_id=operation_id,
            )

            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_backed_up = deletion_results.get("images_backed_up", 0)
            total_deleted = deletion_results.get("docker_images_deleted", 0)
            total_cleaned = deletion_results.get("mongo_records_cleaned", 0)
            if total_backed_up > 0:
                logger.info(f"Total images backed up to S3: {total_backed_up}")
            logger.info(f"Total Docker images deleted: {total_deleted}")
            logger.info(f"Total MongoDB records cleaned: {total_cleaned}")

            logger.info(f"\n✅ Archived {processing_str} tags deletion completed successfully!")

            # Optionally run registry garbage collection for internal registries
            if args.apply and args.run_registry_gc:
                from utils.registry_maintenance import run_registry_garbage_collection

                logger.info("Running Docker registry garbage collection after archived tag deletion...")
                gc_ok = run_registry_garbage_collection(registry_statefulset=args.registry_statefulset)
                if not gc_ok:
                    logger.warning(
                        "Docker registry garbage collection did not complete successfully; " "see logs for details."
                    )

        else:
            # Find mode - calculate freed space and generate report
            logger.info("Calculating freed space for archived tags...")
            freed_space_bytes = finder.calculate_freed_space(archived_tags)

            logger.info("Generating report...")
            report = finder.generate_report(
                archived_ids,
                archived_tags,
                id_to_type_map,
                freed_space_bytes,
                environment_to_revisions=environment_to_revisions,
                model_to_versions=model_to_versions,
            )

            # Save report
            save_json(output_file, report)

            # Print summary
            summary = report["summary"]
            logger.info("\n" + "=" * 60)
            logger.info(f"   ARCHIVED {processing_str.upper()} TAGS ANALYSIS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total archived ObjectIDs: {summary['total_archived_object_ids']}")
            if args.environment:
                logger.info(f"  - Archived environment IDs: {summary['archived_environment_ids']}")
                logger.info(f"  - Archived revision IDs: {summary['archived_revision_ids']}")
            if args.model:
                logger.info(f"  - Archived model IDs: {summary['archived_model_ids']}")
                logger.info(f"  - Archived version IDs: {summary['archived_version_ids']}")
            logger.info(f"Total matching tags: {summary['total_matching_tags']}")
            freed_space_bytes = summary.get("freed_space_bytes", summary.get("freed_space_gb", 0) * (1024**3))
            logger.info(f"Space that would be freed: {sizeof_fmt(freed_space_bytes)}")
            logger.info(f"Tags by image type:")
            for img_type, count in summary["tags_by_image_type"].items():
                logger.info(f"  {img_type}: {count} tags")
            logger.info(f"ObjectIDs with tags: {summary['object_ids_with_tags']}")
            logger.info(f"ObjectIDs without tags: {summary['object_ids_without_tags']}")

            logger.info(f"\nDetailed report saved to: {output_file}")

            if archived_tags:
                logger.warning(
                    f"\n⚠️  Found {len(archived_tags)} archived {processing_str} tags that may need cleanup!"
                )
                logger.info(
                    "Review the detailed report to identify which Docker images are associated with archived records."
                )
                logger.info("\n" + "=" * 60)
                logger.info("🔍 DRY RUN MODE COMPLETED")
                logger.info("=" * 60)
                logger.info("No images were deleted. Use --apply to actually delete images:")
                logger.info(
                    f"  python delete_archived_tags.py --apply {'--environment' if args.environment else ''} {'--model' if args.model else ''}"
                )
                logger.info("Or use --apply --input <file> to delete from a saved report.")
            else:
                logger.info(f"\n✅ No archived {processing_str} tags found!")
                logger.info(f"\n✅ Archived {processing_str} tags analysis completed successfully!")

    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Operation failed: {e}")
        from utils.logging_utils import log_exception

        log_exception(logger, "Error in main", exc_info=e)
        sys.exit(1)


if __name__ == "__main__":
    main()
