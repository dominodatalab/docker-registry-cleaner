"""Utility module for tracking where Docker images are in use"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config_manager import config_manager
from mongo_utils import get_mongo_client

logger = logging.getLogger(__name__)


class ImageUsageTracker:
    """Tracks where Docker images are in use (runs, workspaces, models, scheduler_jobs, projects)"""
    
    def __init__(self):
        self.logger = logger
    
    def load_mongodb_usage_reports(self) -> Dict[str, List[Dict]]:
        """Load MongoDB usage reports (runs, workspaces, models) that contain Docker image tag references
        
        Returns:
            Dict with keys: 'runs', 'workspaces', 'models' containing lists of records
        """
        output_dir = config_manager.get_output_dir()
        reports = {
            'runs': [],
            'workspaces': [],
            'models': []
        }
        
        # Load runs environment usage
        runs_file = Path(config_manager.get_runs_env_usage_path())
        if runs_file.exists():
            try:
                with open(runs_file, 'r') as f:
                    content = f.read()
                    try:
                        reports['runs'] = json.loads(content)
                        if not isinstance(reports['runs'], list):
                            reports['runs'] = [reports['runs']] if reports['runs'] else []
                    except json.JSONDecodeError:
                        reports['runs'] = self._parse_mongodb_json(content)
                self.logger.info(f"Loaded {len(reports['runs'])} runs environment records")
            except Exception as e:
                self.logger.warning(f"Could not load runs environment usage: {e}")
        else:
            self.logger.warning(f"Runs environment usage file not found: {runs_file}")
        
        # Load workspace environment usage
        workspace_file = Path(config_manager.get_workspace_env_usage_path())
        if workspace_file.exists():
            try:
                with open(workspace_file, 'r') as f:
                    content = f.read()
                    try:
                        reports['workspaces'] = json.loads(content)
                        if not isinstance(reports['workspaces'], list):
                            reports['workspaces'] = [reports['workspaces']] if reports['workspaces'] else []
                    except json.JSONDecodeError:
                        reports['workspaces'] = self._parse_mongodb_json(content)
                self.logger.info(f"Loaded {len(reports['workspaces'])} workspace environment records")
            except Exception as e:
                self.logger.warning(f"Could not load workspace environment usage: {e}")
        
        # Load model environment usage
        model_file = Path(config_manager.get_model_env_usage_path())
        if model_file.exists():
            try:
                with open(model_file, 'r') as f:
                    content = f.read()
                    try:
                        reports['models'] = json.loads(content)
                        if not isinstance(reports['models'], list):
                            reports['models'] = [reports['models']] if reports['models'] else []
                    except json.JSONDecodeError:
                        reports['models'] = self._parse_mongodb_json(content)
                self.logger.info(f"Loaded {len(reports['models'])} model environment records")
            except Exception as e:
                self.logger.warning(f"Could not load model environment usage: {e}")
        
        return reports
    
    
    def _parse_mongodb_json(self, content: str) -> List[dict]:
        """Parse MongoDB extended JSON format to extract data"""
        try:
            cleaned = content.strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            elif isinstance(parsed, dict):
                return [parsed]
        except:
            pass
        return []
    
    def extract_docker_tags_with_usage_info(self, mongodb_reports: Dict[str, List[Dict]]) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker image tags from MongoDB reports with detailed usage information
        
        Args:
            mongodb_reports: Dict with 'runs', 'workspaces', 'models' keys containing lists of records
        
        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'runs': [...], 'workspaces': [...], 'models': [...], 'scheduler_jobs': [...], 'projects': [...]}
        """
        tags = set()
        usage_info = {}  # Maps tag -> dict with usage details
        
        # Extract tags from runs
        for record in mongodb_reports.get('runs', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []}
                run_info = {
                    'run_id': record.get('run_id') or record.get('_id', 'unknown'),
                    'project_id': record.get('project_id', 'unknown'),
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
                        usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []}
                    workspace_usage = {
                        'workspace_id': workspace_id,
                        'workspace_name': workspace_name,
                        'project_name': project_name,
                        'usage_type': usage_type
                    }
                    usage_info[tag]['workspaces'].append(workspace_usage)
        
        # Extract tags from models
        for record in mongodb_reports.get('models', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []}
                model_info = {
                    'model_id': record.get('model_id') or record.get('_id', 'unknown'),
                    'model_name': record.get('model_name', 'unknown'),
                    'version_id': record.get('model_version_id', 'unknown')
                }
                usage_info[tag]['models'].append(model_info)
        
        # Also check scheduler_jobs and projects collections for environment ID references
        # These reference environment IDs directly, so we need to resolve them to Docker tags
        direct_tags, direct_usage_info = self.extract_docker_tags_from_direct_environment_usage()
        if direct_tags:
            tags.update(direct_tags)
            # Merge usage info
            for tag, tag_usage in direct_usage_info.items():
                if tag not in usage_info:
                    usage_info[tag] = {
                        'runs': [],
                        'workspaces': [],
                        'models': [],
                        'scheduler_jobs': [],
                        'projects': []
                    }
                # Merge scheduler_jobs and projects usage
                usage_info[tag]['scheduler_jobs'].extend(tag_usage.get('scheduler_jobs', []))
                usage_info[tag]['projects'].extend(tag_usage.get('projects', []))
        
        return tags, usage_info
    
    def extract_docker_tags_from_direct_environment_usage(self) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker tags from scheduler_jobs and projects that reference environment IDs directly
        
        This queries MongoDB collections that reference environment IDs (not Docker tags):
        - scheduler_jobs (jobDataPlain.overrideEnvironmentId)
        - projects (overrideV2EnvironmentId)
        
        Then resolves those environment IDs to Docker tags via environment_revisions.
        
        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'scheduler_jobs': [...], 'projects': [...]}
        """
        from bson import ObjectId
        
        tags = set()
        usage_info = {}  # Maps tag -> dict with usage details
        
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]
        
        try:
            # Collect all environment IDs referenced in scheduler_jobs and projects
            env_ids_from_scheduler = set()
            env_ids_from_projects = set()
            scheduler_jobs_data = {}  # Maps env_id -> list of job info
            projects_data = {}  # Maps env_id -> list of project info
            
            # Query scheduler_jobs
            scheduler_coll = db["scheduler_jobs"]
            for doc in scheduler_coll.find(
                {"jobDataPlain.overrideEnvironmentId": {"$exists": True, "$ne": None}},
                {"_id": 1, "jobName": 1, "projectId": 1, "jobDataPlain.overrideEnvironmentId": 1}
            ):
                job_data = doc.get("jobDataPlain", {})
                env_id_obj = job_data.get("overrideEnvironmentId")
                if env_id_obj:
                    env_id = str(env_id_obj)
                    env_ids_from_scheduler.add(env_id)
                    if env_id not in scheduler_jobs_data:
                        scheduler_jobs_data[env_id] = []
                    scheduler_jobs_data[env_id].append({
                        '_id': str(doc.get('_id', '')),
                        'jobName': doc.get('jobName', 'unknown'),
                        'projectId': str(doc.get('projectId', '')) if doc.get('projectId') else 'unknown'
                    })
            
            # Query projects
            projects_coll = db["projects"]
            for doc in projects_coll.find(
                {"overrideV2EnvironmentId": {"$exists": True, "$ne": None}},
                {"_id": 1, "name": 1, "ownerId": 1, "overrideV2EnvironmentId": 1}
            ):
                env_id_obj = doc.get("overrideV2EnvironmentId")
                if env_id_obj:
                    env_id = str(env_id_obj)
                    env_ids_from_projects.add(env_id)
                    if env_id not in projects_data:
                        projects_data[env_id] = []
                    projects_data[env_id].append({
                        '_id': str(doc.get('_id', '')),
                        'name': doc.get('name', 'unknown'),
                        'ownerId': str(doc.get('ownerId', '')) if doc.get('ownerId') else 'unknown'
                    })
            
            # Resolve environment IDs to Docker tags via environment_revisions
            # For each environment, get its active revision's Docker tag
            all_env_ids = env_ids_from_scheduler | env_ids_from_projects
            if all_env_ids:
                envs_coll = db["environments_v2"]
                revs_coll = db["environment_revisions"]
                
                # Get active revision IDs for these environments
                env_to_active_rev = {}
                valid_env_ids = [ObjectId(eid) for eid in all_env_ids if len(eid) == 24]
                if valid_env_ids:
                    for doc in envs_coll.find(
                        {"_id": {"$in": valid_env_ids}},
                        {"_id": 1, "activeRevisionId": 1}
                    ):
                        env_id = str(doc.get("_id", ""))
                        active_rev_id = doc.get("activeRevisionId")
                        if active_rev_id:
                            env_to_active_rev[env_id] = str(active_rev_id)
                
                # Get Docker tags from active revisions
                active_rev_ids = [ObjectId(rid) for rid in env_to_active_rev.values() if len(rid) == 24]
                if active_rev_ids:
                    rev_to_tag = {}
                    for doc in revs_coll.find(
                        {"_id": {"$in": active_rev_ids}},
                        {"_id": 1, "metadata.dockerImageName.tag": 1}
                    ):
                        rev_id = str(doc.get("_id", ""))
                        docker_tag = doc.get("metadata", {}).get("dockerImageName", {}).get("tag")
                        if docker_tag:
                            rev_to_tag[rev_id] = docker_tag
                    
                    # Map environment IDs to Docker tags
                    for env_id in all_env_ids:
                        active_rev_id = env_to_active_rev.get(env_id)
                        if active_rev_id:
                            docker_tag = rev_to_tag.get(active_rev_id)
                            if docker_tag:
                                tags.add(docker_tag)
                                if docker_tag not in usage_info:
                                    usage_info[docker_tag] = {
                                        'pods': [],
                                        'runs': [],
                                        'workspaces': [],
                                        'models': [],
                                        'scheduler_jobs': [],
                                        'projects': []
                                    }
                                
                                # Add scheduler_jobs usage
                                if env_id in scheduler_jobs_data:
                                    usage_info[docker_tag]['scheduler_jobs'].extend(scheduler_jobs_data[env_id])
                                
                                # Add projects usage
                                if env_id in projects_data:
                                    usage_info[docker_tag]['projects'].extend(projects_data[env_id])
                
                if tags:
                    self.logger.info(f"Found {len(tags)} Docker tags referenced by scheduler_jobs and projects")
                    scheduler_count = sum(len(usage_info[tag].get('scheduler_jobs', [])) for tag in tags)
                    projects_count = sum(len(usage_info[tag].get('projects', [])) for tag in tags)
                    self.logger.info(f"  - {scheduler_count} scheduler jobs, {projects_count} projects")
        
        finally:
            mongo_client.close()
        
        return tags, usage_info
    
    def get_usage_for_tag(self, tag: str, mongodb_reports: Dict[str, List[Dict]] = None) -> Dict:
        """Get usage information for a specific tag
        
        Args:
            tag: Docker image tag to check
            mongodb_reports: Optional MongoDB usage reports
        
        Returns:
            Dict with usage information: {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []}
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        
        _, usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports)
        return usage_info.get(tag, {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []})
    
    def generate_usage_summary(self, usage: Dict) -> str:
        """Generate a human-readable summary of why an image is in use
        
        Args:
            usage: Usage dictionary with 'runs', 'workspaces', 'models', 'scheduler_jobs', 'projects' info
        
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
        
        if not reasons:
            return "Referenced in system (source unknown)"
        
        return ", ".join(reasons)
    
    def check_tags_in_use(self, tags: List[str], mongodb_reports: Dict[str, List[Dict]] = None) -> Tuple[Set[str], Dict[str, Dict]]:
        """Check which tags from a list are in use
        
        Args:
            tags: List of Docker image tags to check
            mongodb_reports: Optional MongoDB usage reports
        
        Returns:
            Tuple of (set of tags that are in use, dict mapping tag -> usage info)
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        
        all_used_tags, all_usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports)
        
        tags_set = set(tags)
        in_use_tags = tags_set.intersection(all_used_tags)
        
        # Build usage info for only the tags we're checking
        usage_info = {tag: all_usage_info.get(tag, {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': []}) for tag in in_use_tags}
        
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
        
        This checks collections that reference environment IDs directly:
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
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]
        
        usage_by_id = {env_id: {
            'projects': [],
            'scheduler_jobs': [],
            'organizations': [],
            'app_versions': []
        } for env_id in environment_ids}
        
        try:
            # Check projects collection
            projects_coll = db["projects"]
            for doc in projects_coll.find(
                {"overrideV2EnvironmentId": {"$in": [env_id for env_id in environment_ids]}},
                {"_id": 1, "name": 1, "ownerId": 1, "overrideV2EnvironmentId": 1}
            ):
                env_id = str(doc.get("overrideV2EnvironmentId", ""))
                if env_id in usage_by_id:
                    usage_by_id[env_id]['projects'].append({
                        '_id': doc.get('_id'),
                        'name': doc.get('name', ''),
                        'ownerId': doc.get('ownerId')
                    })
            
            # Check scheduler_jobs collection
            scheduler_coll = db["scheduler_jobs"]
            for doc in scheduler_coll.find(
                {"jobDataPlain.overrideEnvironmentId": {"$in": [env_id for env_id in environment_ids]}},
                {"_id": 1, "jobName": 1, "projectId": 1, "jobDataPlain.overrideEnvironmentId": 1}
            ):
                job_data = doc.get("jobDataPlain", {})
                env_id = str(job_data.get("overrideEnvironmentId", ""))
                if env_id in usage_by_id:
                    usage_by_id[env_id]['scheduler_jobs'].append({
                        '_id': doc.get('_id'),
                        'jobName': doc.get('jobName', ''),
                        'projectId': doc.get('projectId')
                    })
            
            # Check organizations collection (if it exists)
            if "organizations" in db.list_collection_names():
                orgs_coll = db["organizations"]
                for doc in orgs_coll.find(
                    {"defaultV2EnvironmentId": {"$in": [env_id for env_id in environment_ids]}},
                    {"_id": 1, "name": 1, "defaultV2EnvironmentId": 1}
                ):
                    env_id = str(doc.get("defaultV2EnvironmentId", ""))
                    if env_id in usage_by_id:
                        usage_by_id[env_id]['organizations'].append({
                            '_id': doc.get('_id'),
                            'name': doc.get('name', '')
                        })
            
            # Check app_versions collection (if it exists)
            if "app_versions" in db.list_collection_names():
                app_versions_coll = db["app_versions"]
                for doc in app_versions_coll.find(
                    {"environmentId": {"$in": [env_id for env_id in environment_ids]}},
                    {"_id": 1, "appId": 1, "versionNumber": 1, "environmentId": 1}
                ):
                    env_id = str(doc.get("environmentId", ""))
                    if env_id in usage_by_id:
                        usage_by_id[env_id]['app_versions'].append({
                            '_id': doc.get('_id'),
                            'appId': doc.get('appId'),
                            'versionNumber': doc.get('versionNumber')
                        })
        
        finally:
            mongo_client.close()
        
        return usage_by_id