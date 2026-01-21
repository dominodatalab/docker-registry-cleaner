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
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from utils.config_manager import config_manager
from utils.mongo_utils import get_mongo_client, bson_to_jsonable
from utils.report_utils import save_json

logger = logging.getLogger(__name__)

from utils.extract_metadata import (
    model_env_usage_pipeline,
    workspace_env_usage_pipeline,
    runs_env_usage_pipeline,
    projects_env_usage_pipeline,
    scheduler_jobs_env_usage_pipeline,
    organizations_env_usage_pipeline,
    app_versions_env_usage_pipeline,
)


class ImageUsageService:
    """Service for collecting, loading, and analyzing image usage information."""

    def __init__(self):
        self.mongo_db = config_manager.get_mongo_db()
        self.logger = logger

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
        
        # Save to consolidated file with timestamp
        save_json(
            config_manager.get_mongodb_usage_path(),
            consolidated,
            timestamp=True,
        )

    # ------------------------------------------------------------------
    # Loading from saved reports
    # ------------------------------------------------------------------

    def load_usage_reports(self) -> Dict[str, List[dict]]:
        """Load MongoDB usage reports from saved consolidated JSON file.
        
        Supports both timestamped and non-timestamped report files.
        If exact file doesn't exist, finds the most recent timestamped version.
        
        Returns:
            Dict with keys: 'runs', 'workspaces', 'models', 'projects', 
            'scheduler_jobs', 'organizations', 'app_versions'
        """
        from utils.report_utils import get_latest_report, get_reports_dir
        
        consolidated_path = Path(config_manager.get_mongodb_usage_path())
        
        # If exact file doesn't exist, try to find latest timestamped version
        if not consolidated_path.exists():
            reports_dir = get_reports_dir()
            stem = consolidated_path.stem
            suffix = consolidated_path.suffix
            pattern = f"{stem}-*-*-*-*-*-*{suffix}"
            latest = get_latest_report(pattern, reports_dir)
            if latest:
                consolidated_path = latest
                logger.info(f"Using latest timestamped report: {consolidated_path.name}")
        
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
    # Tag extraction and usage analysis
    # ------------------------------------------------------------------

    def load_mongodb_usage_reports(self) -> Dict[str, List[Dict]]:
        """Load MongoDB usage reports (runs, workspaces, models, projects, scheduler_jobs, organizations, app_versions) that contain Docker image tag references.
        
        First tries to load from saved consolidated report file. If that doesn't exist,
        runs fresh aggregations against MongoDB.
        
        Returns:
            Dict with keys: 'runs', 'workspaces', 'models', 'projects', 'scheduler_jobs', 'organizations', 'app_versions' containing lists of records
        """
        # Try loading from saved file first
        reports = self.load_usage_reports()
        
        # If no data was loaded, run fresh aggregations
        if not any(reports.values()):
            reports = self.run_aggregations("all")
            return {
                "runs": reports.get("runs", []),
                "workspaces": reports.get("workspaces", []),
                "models": reports.get("models", []),
                "projects": reports.get("projects", []),
                "scheduler_jobs": reports.get("scheduler_jobs", []),
                "organizations": reports.get("organizations", []),
                "app_versions": reports.get("app_versions", []),
            }
        
        return reports

    def extract_docker_tags_with_usage_info(self, mongodb_reports: Dict[str, List[Dict]]) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker image tags from MongoDB reports with detailed usage information
        
        Args:
            mongodb_reports: Dict with 'runs', 'workspaces', 'models', 'projects', 'scheduler_jobs', 'organizations', 'app_versions' keys containing lists of records
        
        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'runs': [...], 'workspaces': [...], 'models': [...], 'scheduler_jobs': [...], 'projects': [...], 'organizations': [...], 'app_versions': [...]}
        """
        tags = set()
        usage_info = {}  # Maps tag -> dict with usage details
        
        # Extract tags from runs
        for record in mongodb_reports.get('runs', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                run_info = {
                    'run_id': record.get('run_id') or record.get('_id', 'unknown'),
                    'project_id': record.get('project_id', 'unknown'),
                    'project_name': record.get('project_name', 'unknown'),
                    'project_owner_id': record.get('project_owner_id', 'unknown'),
                    'project_owner_name': record.get('project_owner_name', 'unknown'),
                    'status': record.get('status', 'unknown'),
                    'started': record.get('started') or record.get('any_started'),
                    'completed': record.get('completed') or record.get('any_completed') or record.get('last_used')
                }
                usage_info[tag]['runs'].append(run_info)
        
        # Extract tags from workspaces
        for record in mongodb_reports.get('workspaces', []):
            workspace_id = record.get('workspace_id') or record.get('_id', 'unknown')
            workspace_name = record.get('workspace_name', 'unknown')
            project_name = record.get('project_name', 'unknown')
            
            tag_fields = [
                ('environment_docker_tag', 'environment'),
                ('project_default_environment_docker_tag', 'project_default'),
                ('compute_environment_docker_tag', 'compute_cluster'),
                ('session_environment_docker_tag', 'session'),
                ('session_compute_environment_docker_tag', 'session_compute')
            ]
            for field, usage_type in tag_fields:
                if field in record and record[field]:
                    tag = record[field]
                    tags.add(tag)
                    if tag not in usage_info:
                        usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                    workspace_usage = {
                        'workspace_id': workspace_id,
                        'workspace_name': workspace_name,
                        'project_name': project_name,
                        'usage_type': usage_type,
                        'workspace_last_change': record.get('workspace_last_change')
                    }
                    usage_info[tag]['workspaces'].append(workspace_usage)
        
        # Extract tags from models
        for record in mongodb_reports.get('models', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                model_info = {
                    'model_id': record.get('model_id') or record.get('_id', 'unknown'),
                    'model_name': record.get('model_name', 'unknown'),
                    'version_id': record.get('model_version_id', 'unknown')
                }
                usage_info[tag]['models'].append(model_info)
        
        # Extract tags from projects (from pipeline results)
        for record in mongodb_reports.get('projects', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                project_info = {
                    '_id': str(record.get('project_id', '')),
                    'name': record.get('project_name', 'unknown'),
                    'ownerId': str(record.get('owner_id', '')) if record.get('owner_id') else 'unknown'
                }
                usage_info[tag]['projects'].append(project_info)
        
        # Extract tags from scheduler_jobs (from pipeline results)
        for record in mongodb_reports.get('scheduler_jobs', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                job_info = {
                    '_id': str(record.get('job_id', '')),
                    'jobName': record.get('job_name', 'unknown'),
                    'projectId': str(record.get('project_id', '')) if record.get('project_id') else 'unknown'
                }
                usage_info[tag]['scheduler_jobs'].append(job_info)
        
        # Extract tags from organizations (from pipeline results)
        for record in mongodb_reports.get('organizations', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                org_info = {
                    '_id': str(record.get('organization_id', '')),
                    'name': record.get('organization_name', 'unknown')
                }
                usage_info[tag]['organizations'].append(org_info)
        
        # Extract tags from app_versions (from pipeline results)
        for record in mongodb_reports.get('app_versions', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                app_version_info = {
                    '_id': str(record.get('app_version_id', '')),
                    'appId': record.get('app_id'),
                    'versionNumber': record.get('version_number')
                }
                usage_info[tag]['app_versions'].append(app_version_info)
        
        return tags, usage_info

    def get_usage_for_tag(self, tag: str, mongodb_reports: Dict[str, List[Dict]] = None) -> Dict:
        """Get usage information for a specific tag
        
        Args:
            tag: Docker image tag to check
            mongodb_reports: Optional MongoDB usage reports
        
        Returns:
            Dict with usage information: {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        
        _, usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports)
        return usage_info.get(tag, {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []})
    
    def generate_usage_summary(self, usage: Dict) -> str:
        """Generate a human-readable summary of why an image is in use
        
        Args:
            usage: Usage dictionary with 'runs', 'workspaces', 'models', 'scheduler_jobs', 'projects', 'organizations', 'app_versions' info
        
        Returns:
            Human-readable string describing usage
        """
        reasons = []
        
        if usage.get('runs'):
            run_count = len(usage['runs'])
            reasons.append(f"{run_count} execution{'s' if run_count > 1 else ''} in MongoDB")
        
        if usage.get('workspaces'):
            ws_count = len(usage['workspaces'])
            reasons.append(f"{ws_count} workspace{'s' if ws_count > 1 else ''}")
        
        if usage.get('models'):
            model_count = len(usage['models'])
            reasons.append(f"{model_count} model{'s' if model_count > 1 else ''}")
        
        scheduler_jobs = usage.get('scheduler_jobs', [])
        if scheduler_jobs:
            scheduler_count = len(scheduler_jobs)
            reasons.append(f"{scheduler_count} scheduler job{'s' if scheduler_count > 1 else ''}")
        
        projects = usage.get('projects', [])
        if projects:
            project_count = len(projects)
            reasons.append(f"{project_count} project{'s' if project_count > 1 else ''} using as default")
        
        organizations = usage.get('organizations', [])
        if organizations:
            org_count = len(organizations)
            reasons.append(f"{org_count} organization{'s' if org_count > 1 else ''} using as default")
        
        app_versions = usage.get('app_versions', [])
        if app_versions:
            app_version_count = len(app_versions)
            reasons.append(f"{app_version_count} app version{'s' if app_version_count > 1 else ''}")
        
        if not reasons:
            return "Referenced in system (source unknown)"
        
        return ", ".join(reasons)
    
    def _parse_timestamp(self, timestamp_str) -> Optional[datetime]:
        """Parse a timestamp string to datetime object
        
        Args:
            timestamp_str: ISO format timestamp string (may end with 'Z') or
                MongoDB extended JSON dict like {"$date": "..."}.
        
        Returns:
            datetime object or None if parsing fails
        """
        if not timestamp_str:
            return None
        try:
            # Handle MongoDB extended JSON: {"$date": "..."}
            if isinstance(timestamp_str, dict) and "$date" in timestamp_str:
                timestamp_str = timestamp_str["$date"]
            
            # Handle numeric epoch milliseconds (defensive, not expected from current pipelines)
            if isinstance(timestamp_str, (int, float)):
                # Assume milliseconds since epoch
                return datetime.fromtimestamp(timestamp_str / 1000.0, tz=timezone.utc)
            
            # At this point we expect a string
            if not isinstance(timestamp_str, str):
                return None
            
            # Handle ISO strings possibly ending with 'Z'
            ts = timestamp_str.replace('Z', '+00:00')
            return datetime.fromisoformat(ts)
        except Exception:
            return None
    
    def _get_most_recent_usage_date(self, usage_info: Dict) -> Optional[datetime]:
        """Get the most recent usage date from usage information
        
        Checks runs (last_used, completed, started), workspaces (workspace_last_change),
        and other sources to find the most recent timestamp.
        
        Args:
            usage_info: Dict with 'runs', 'workspaces', 'models', etc. containing usage records
        
        Returns:
            Most recent datetime or None if no timestamps found
        """
        most_recent = None
        
        # Check runs - prefer last_used, then completed, then started
        for run in usage_info.get('runs', []):
            for field in ['last_used', 'completed', 'started']:
                ts = self._parse_timestamp(run.get(field))
                if ts:
                    if most_recent is None or ts > most_recent:
                        most_recent = ts
                    break  # Use first available timestamp per run
        
        # Check workspaces - use workspace_last_change
        for workspace in usage_info.get('workspaces', []):
            ts = self._parse_timestamp(workspace.get('workspace_last_change'))
            if ts:
                if most_recent is None or ts > most_recent:
                    most_recent = ts
        
        return most_recent
    
    def check_tags_in_use(self, tags: List[str], mongodb_reports: Dict[str, List[Dict]] = None, recent_days: Optional[int] = None) -> Tuple[Set[str], Dict[str, Dict]]:
        """Check which tags from a list are in use
        
        Args:
            tags: List of Docker image tags to check
            mongodb_reports: Optional MongoDB usage reports
            recent_days: Optional number of days - if provided, only consider tags as "in-use" if they were used within the last N days
        
        Returns:
            Tuple of (set of tags that are in use, dict mapping tag -> usage info)
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        
        all_used_tags, all_usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports)
        
        tags_set = set(tags)
        in_use_tags = tags_set.intersection(all_used_tags)
        
        # Build usage info for only the tags we're checking
        usage_info = {tag: all_usage_info.get(tag, {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}) for tag in in_use_tags}
        
        # Filter by age if recent_days is specified
        if recent_days is not None and recent_days > 0:
            threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)
            filtered_in_use_tags = set()
            filtered_usage_info = {}
            
            for tag in in_use_tags:
                tag_usage = usage_info.get(tag, {})
                
                # Check for usage from sources without timestamps (always keep these - they represent current config)
                has_config_usage = (
                    len(tag_usage.get('models', [])) > 0 or
                    len(tag_usage.get('scheduler_jobs', [])) > 0 or
                    len(tag_usage.get('projects', [])) > 0 or
                    len(tag_usage.get('organizations', [])) > 0 or
                    len(tag_usage.get('app_versions', [])) > 0
                )
                
                if has_config_usage:
                    # Keep tags with current configuration usage (conservative)
                    filtered_in_use_tags.add(tag)
                    filtered_usage_info[tag] = tag_usage
                    continue
                
                # Check for recent usage from runs or workspaces (with timestamps)
                most_recent = self._get_most_recent_usage_date(tag_usage)
                if most_recent is not None and most_recent >= threshold:
                    filtered_in_use_tags.add(tag)
                    filtered_usage_info[tag] = tag_usage
            
            return filtered_in_use_tags, filtered_usage_info
        
        return in_use_tags, usage_info
    
    def find_usage_for_environment_ids(self, environment_ids: Set[str], mongodb_reports: Dict[str, List[Dict]] = None) -> Dict[str, Dict]:
        """Find usage information for a set of environment/revision IDs
        
        This matches tags that contain these IDs (e.g., tags starting with the ObjectID).
        
        Args:
            environment_ids: Set of environment or revision ObjectIDs to find usage for
            mongodb_reports: Optional MongoDB usage reports
        
        Returns:
            Dict mapping each environment_id to its usage info:
            {
                'environment_id': {
                    'matching_tags': [...],  # Tags that contain this ID
                    'workspaces': [...],     # Workspace records using these tags
                    'runs': [...],           # Run records using these tags
                    'models': [...]          # Model records using these tags
                }
            }
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        
        # Helper to check if a tag contains any of our IDs
        def _tag_matches_ids(tag: str) -> bool:
            if not tag or len(tag) < 24:
                return False
            # Check if tag starts with any of our IDs
            for env_id in environment_ids:
                if tag.startswith(env_id) or env_id in tag:
                    return True
            return False
        
        # Helper to check if a record directly references our IDs
        def _record_matches_ids(rec: Dict) -> bool:
            env_id_val = str(rec.get("environment_id") or "")
            rev_id_val = str(rec.get("environment_revision_id") or "")
            if env_id_val in environment_ids or rev_id_val in environment_ids:
                return True
            tag = rec.get("environment_docker_tag")
            if isinstance(tag, str) and _tag_matches_ids(tag):
                return True
            return False
        
        # Build usage info per environment ID
        usage_by_id = {env_id: {
            'matching_tags': [],
            'workspaces': [],
            'runs': [],
            'models': []
        } for env_id in environment_ids}
        
        # Check workspace records
        for rec in mongodb_reports.get('workspaces', []):
            if any(_tag_matches_ids(rec.get(key, '')) for key in [
                "environment_docker_tag",
                "project_default_environment_docker_tag",
                "compute_environment_docker_tag",
                "session_environment_docker_tag",
                "session_compute_environment_docker_tag",
            ]):
                # Find which IDs this workspace matches
                for env_id in environment_ids:
                    workspace_tags = [
                        rec.get(key, '') for key in [
                            "environment_docker_tag",
                            "project_default_environment_docker_tag",
                            "compute_environment_docker_tag",
                            "session_environment_docker_tag",
                            "session_compute_environment_docker_tag",
                        ]
                    ]
                    if any(_tag_matches_ids(tag) for tag in workspace_tags):
                        usage_by_id[env_id]['workspaces'].append(rec)
        
        # Check run records
        for rec in mongodb_reports.get('runs', []):
            if _record_matches_ids(rec):
                env_id_val = str(rec.get("environment_id") or "")
                rev_id_val = str(rec.get("environment_revision_id") or "")
                # Add to both env_id and rev_id if they're in our set
                if env_id_val in environment_ids:
                    usage_by_id[env_id_val]['runs'].append(rec)
                if rev_id_val in environment_ids:
                    usage_by_id[rev_id_val]['runs'].append(rec)
        
        # Check model records
        for rec in mongodb_reports.get('models', []):
            tag = rec.get('environment_docker_tag', '')
            if _tag_matches_ids(tag):
                for env_id in environment_ids:
                    if env_id in tag:
                        usage_by_id[env_id]['models'].append(rec)
        
        return usage_by_id
    
    def find_direct_environment_id_usage(self, environment_ids: Set[str]) -> Dict[str, Dict]:
        """Find direct environment ID usage in MongoDB collections (not via Docker tags)
        
        This checks collections that reference environment IDs directly using pipeline results:
        - projects (overrideV2EnvironmentId)
        - scheduler_jobs (jobDataPlain.overrideEnvironmentId)
        - organizations (defaultV2EnvironmentId)
        - app_versions (environmentId)
        
        Args:
            environment_ids: Set of environment ObjectIDs to check
        
        Returns:
            Dict mapping each environment_id to its direct usage:
            {
                'environment_id': {
                    'projects': [...],           # Projects using as default
                    'scheduler_jobs': [...],     # Scheduler jobs using as override
                    'organizations': [...],      # Organizations using as default
                    'app_versions': [...]        # App versions referencing
                }
            }
        """
        # Load pipeline reports
        reports = self.load_mongodb_usage_reports()
        
        usage_by_id = {env_id: {
            'projects': [],
            'scheduler_jobs': [],
            'organizations': [],
            'app_versions': []
        } for env_id in environment_ids}
        
        # Filter projects by environment_id
        for record in reports.get('projects', []):
            env_id = str(record.get('environment_id', ''))
            if env_id in usage_by_id:
                usage_by_id[env_id]['projects'].append({
                    '_id': record.get('project_id'),
                    'name': record.get('project_name', ''),
                    'ownerId': record.get('owner_id')
                })
        
        # Filter scheduler_jobs by environment_id
        for record in reports.get('scheduler_jobs', []):
            env_id = str(record.get('environment_id', ''))
            if env_id in usage_by_id:
                usage_by_id[env_id]['scheduler_jobs'].append({
                    '_id': record.get('job_id'),
                    'jobName': record.get('job_name', ''),
                    'projectId': record.get('project_id')
                })
        
        # Filter organizations by environment_id
        for record in reports.get('organizations', []):
            env_id = str(record.get('environment_id', ''))
            if env_id in usage_by_id:
                usage_by_id[env_id]['organizations'].append({
                    '_id': record.get('organization_id'),
                    'name': record.get('organization_name', '')
                })
        
        # Filter app_versions by environment_id
        for record in reports.get('app_versions', []):
            env_id = str(record.get('environment_id', ''))
            if env_id in usage_by_id:
                usage_by_id[env_id]['app_versions'].append({
                    '_id': record.get('app_version_id'),
                    'appId': record.get('app_id'),
                    'versionNumber': record.get('version_number')
                })
        
        return usage_by_id

