"""Utility module for tracking where Docker images are in use"""

import json
import logging
from typing import Dict, List, Optional, Set, Tuple

from .image_usage import ImageUsageService

logger = logging.getLogger(__name__)


class ImageUsageTracker:
    """Tracks where Docker images are in use (runs, workspaces, models, scheduler_jobs, projects)"""
    
    def __init__(self):
        self.logger = logger
        self._service = ImageUsageService()
    
    def load_mongodb_usage_reports(self) -> Dict[str, List[Dict]]:
        """Load MongoDB usage reports (runs, workspaces, models, projects, scheduler_jobs, organizations, app_versions) that contain Docker image tag references.
        
        First tries to load from saved consolidated report file. If that doesn't exist,
        runs fresh aggregations against MongoDB.
        
        Returns:
            Dict with keys: 'runs', 'workspaces', 'models', 'projects', 'scheduler_jobs', 'organizations', 'app_versions' containing lists of records
        """
        # Try loading from saved file first
        reports = self._service.load_usage_reports()
        
        # If no data was loaded, run fresh aggregations
        if not any(reports.values()):
            reports = self._service.run_aggregations("all")
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
                        'usage_type': usage_type
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
    
    def extract_docker_tags_from_direct_environment_usage(self) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker tags from scheduler_jobs, projects, organizations, and app_versions that reference environment IDs directly
        
        This method now uses pipeline results from the consolidated MongoDB usage report.
        
        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'scheduler_jobs': [...], 'projects': [...], 'organizations': [...], 'app_versions': [...]}
        """
        # Load reports that include projects, scheduler_jobs, organizations, and app_versions
        reports = self.load_mongodb_usage_reports()
        
        tags = set()
        usage_info = {}  # Maps tag -> dict with usage details
        
        # Extract tags from projects (from pipeline results)
        for record in reports.get('projects', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                project_info = {
                    '_id': str(record.get('project_id', '')),
                    'name': record.get('project_name', 'unknown'),
                    'ownerId': str(record.get('owner_id', '')) if record.get('owner_id') else 'unknown'
                }
                usage_info[tag]['projects'].append(project_info)
        
        # Extract tags from scheduler_jobs (from pipeline results)
        for record in reports.get('scheduler_jobs', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                job_info = {
                    '_id': str(record.get('job_id', '')),
                    'jobName': record.get('job_name', 'unknown'),
                    'projectId': str(record.get('project_id', '')) if record.get('project_id') else 'unknown'
                }
                usage_info[tag]['scheduler_jobs'].append(job_info)
        
        # Extract tags from organizations (from pipeline results)
        for record in reports.get('organizations', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                org_info = {
                    '_id': str(record.get('organization_id', '')),
                    'name': record.get('organization_name', 'unknown')
                }
                usage_info[tag]['organizations'].append(org_info)
        
        # Extract tags from app_versions (from pipeline results)
        for record in reports.get('app_versions', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}
                app_version_info = {
                    '_id': str(record.get('app_version_id', '')),
                    'appId': record.get('app_id'),
                    'versionNumber': record.get('version_number')
                }
                usage_info[tag]['app_versions'].append(app_version_info)
        
        if tags:
            self.logger.info(f"Found {len(tags)} Docker tags referenced by scheduler_jobs, projects, organizations, and app_versions")
            scheduler_count = sum(len(usage_info[tag].get('scheduler_jobs', [])) for tag in tags)
            projects_count = sum(len(usage_info[tag].get('projects', [])) for tag in tags)
            orgs_count = sum(len(usage_info[tag].get('organizations', [])) for tag in tags)
            app_versions_count = sum(len(usage_info[tag].get('app_versions', [])) for tag in tags)
            self.logger.info(f"  - {scheduler_count} scheduler jobs, {projects_count} projects, {orgs_count} organizations, {app_versions_count} app versions")
        
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
        usage_info = {tag: all_usage_info.get(tag, {'runs': [], 'workspaces': [], 'models': [], 'scheduler_jobs': [], 'projects': [], 'organizations': [], 'app_versions': []}) for tag in in_use_tags}
        
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