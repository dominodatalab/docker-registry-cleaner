"""Unified image usage service for Domino Docker images.

This module centralizes all logic for discovering where Docker images
are used across Domino:

- Runs (from `runs` collection via aggregation pipeline)
- Workspaces (from `workspace` and related collections via aggregation)
- Models (from `models` / `model_versions` via aggregation)
- Direct environment ID usage:
  - projects.overrideV2EnvironmentId
  - scheduler_jobs.jobDataPlain.overrideEnvironmentId
  - organizations.defaultV2EnvironmentId
  - app_versions.environmentId

It can:
- Run aggregations directly against MongoDB and return usage data
- Save / load usage reports via config_manager paths
"""

import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

from config_manager import config_manager
from mongo_utils import get_mongo_client, bson_to_jsonable
from report_utils import save_json

from .extract_metadata import (
    model_env_usage_pipeline,
    workspace_env_usage_pipeline,
    runs_env_usage_pipeline,
    projects_env_usage_pipeline,
    scheduler_jobs_env_usage_pipeline,
    organizations_env_usage_pipeline,
    app_versions_env_usage_pipeline,
)


class ImageUsageService:
    """Service for collecting and loading image usage information."""

    def __init__(self):
        self.mongo_db = config_manager.get_mongo_db()

    # ------------------------------------------------------------------
    # Aggregation helpers (Mongo → raw records)
    # ------------------------------------------------------------------

    def collect_model_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            return list(db.models.aggregate(model_env_usage_pipeline()))
        finally:
            client.close()

    def collect_workspace_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            return list(db.workspace.aggregate(workspace_env_usage_pipeline()))
        finally:
            client.close()

    def collect_runs_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            return list(db.runs.aggregate(runs_env_usage_pipeline()))
        finally:
            client.close()

    def collect_projects_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            return list(db.projects.aggregate(projects_env_usage_pipeline()))
        finally:
            client.close()

    def collect_scheduler_jobs_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            return list(db.scheduler_jobs.aggregate(scheduler_jobs_env_usage_pipeline()))
        finally:
            client.close()

    def collect_organizations_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            # Check if collection exists before aggregating
            if "organizations" not in db.list_collection_names():
                return []
            return list(db.organizations.aggregate(organizations_env_usage_pipeline()))
        finally:
            client.close()

    def collect_app_versions_usage(self) -> List[dict]:
        client = get_mongo_client()
        try:
            db = client[self.mongo_db]
            # Check if collection exists before aggregating
            if "app_versions" not in db.list_collection_names():
                return []
            return list(db.app_versions.aggregate(app_versions_env_usage_pipeline()))
        finally:
            client.close()

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    def run_aggregations(
        self, target: str = "all"
    ) -> Dict[str, List[dict]]:
        """Run usage aggregations against MongoDB.

        Args:
            target: 'model', 'workspace', 'runs', 'projects', 'scheduler_jobs', 'organizations', 'app_versions', or 'all'

        Returns:
            Dict with keys subset of {'models', 'workspaces', 'runs', 'projects', 'scheduler_jobs', 'organizations', 'app_versions'}.
        """
        results: Dict[str, List[dict]] = {}

        if target in ("model", "all"):
            results["models"] = self.collect_model_usage()
        if target in ("workspace", "all"):
            results["workspaces"] = self.collect_workspace_usage()
        if target in ("runs", "all"):
            results["runs"] = self.collect_runs_usage()
        if target in ("projects", "all"):
            results["projects"] = self.collect_projects_usage()
        if target in ("scheduler_jobs", "all"):
            results["scheduler_jobs"] = self.collect_scheduler_jobs_usage()
        if target in ("organizations", "all"):
            results["organizations"] = self.collect_organizations_usage()
        if target in ("app_versions", "all"):
            results["app_versions"] = self.collect_app_versions_usage()

        return results

    def save_aggregations(self, target: str = "all") -> None:
        """Run aggregations and save them to a consolidated report file.
        
        Saves all results to a single consolidated JSON file for easier management.
        """
        results = self.run_aggregations(target)
        
        # Convert all results to JSON-serializable format
        consolidated = {}
        for key in ["runs", "workspaces", "models", "projects", "scheduler_jobs", "organizations", "app_versions"]:
            if key in results:
                consolidated[key] = bson_to_jsonable(results[key])
            else:
                consolidated[key] = []
        
        # Save to consolidated file
        save_json(
            config_manager.get_mongodb_usage_path(),
            consolidated,
        )

    # ------------------------------------------------------------------
    # Loading from saved reports
    # ------------------------------------------------------------------

    def load_usage_reports(self) -> Dict[str, List[dict]]:
        """Load MongoDB usage reports from saved consolidated JSON file.
        
        Returns:
            Dict with keys: 'runs', 'workspaces', 'models', 'projects', 
            'scheduler_jobs', 'organizations', 'app_versions'
        """
        consolidated_path = Path(config_manager.get_mongodb_usage_path())
        
        if not consolidated_path.exists():
            return {
                "runs": [],
                "workspaces": [],
                "models": [],
                "projects": [],
                "scheduler_jobs": [],
                "organizations": [],
                "app_versions": [],
            }
        
        try:
            with open(consolidated_path, 'r') as f:
                data = json.load(f)
                # Ensure all keys are present
                return {
                    "runs": data.get("runs", []),
                    "workspaces": data.get("workspaces", []),
                    "models": data.get("models", []),
                    "projects": data.get("projects", []),
                    "scheduler_jobs": data.get("scheduler_jobs", []),
                    "organizations": data.get("organizations", []),
                    "app_versions": data.get("app_versions", []),
                }
        except Exception as e:
            # If file is corrupted, return empty dict
            return {
                "runs": [],
                "workspaces": [],
                "models": [],
                "projects": [],
                "scheduler_jobs": [],
                "organizations": [],
                "app_versions": [],
            }

    # ------------------------------------------------------------------
    # Utility for delete_image / usage_tracker
    # ------------------------------------------------------------------

    def build_tag_usage_from_reports(
        self, reports: Dict[str, List[Dict]]
    ) -> Tuple[Set[str], Dict[str, Dict]]:
        """Given pre-loaded model/workspace/run reports, build tag → usage info.

        This mirrors the logic in ImageUsageTracker.extract_docker_tags_with_usage_info,
        but is suitable for re-use by higher-level services.

        Args:
            reports: Dict with 'runs', 'workspaces', 'models'

        Returns:
            (set_of_tags, tag_usage_dict)
        """
        # To avoid tight coupling, we import here to reuse the existing, tested logic.
        from .usage_tracker import ImageUsageTracker  # local import to avoid cycle on module load

        tracker = ImageUsageTracker()
        return tracker.extract_docker_tags_with_usage_info(reports)

