"""Utility module for tracking where Docker images are in use"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config_manager import config_manager
from mongo_utils import get_mongo_client

logger = logging.getLogger(__name__)


class ImageUsageTracker:
    """Tracks where Docker images are in use (pods, runs, workspaces, models)"""
    
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
        runs_file = Path(output_dir) / config_manager.get_runs_env_usage_path()
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
        workspace_file = Path(output_dir) / "workspace_env_usage_output.json"
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
        model_file = Path(output_dir) / "model_env_usage_output.json"
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
    
    def load_workload_report(self, report_path: str = None) -> Dict:
        """Load workload analysis report from JSON file"""
        if report_path is None:
            report_path = config_manager.get_workload_report_path()
        try:
            with open(report_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Workload report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.warning(f"Invalid JSON in workload report: {e}")
            return {}
    
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
    
    def extract_docker_tags_with_usage_info(self, mongodb_reports: Dict[str, List[Dict]], workload_report: Dict = None) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker image tags from MongoDB reports and workload report with detailed usage information
        
        Args:
            mongodb_reports: Dict with 'runs', 'workspaces', 'models' keys containing lists of records
            workload_report: Optional workload report from Kubernetes
        
        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'pods': [...], 'runs': [...], 'workspaces': [...], 'models': [...]}
        """
        tags = set()
        usage_info = {}  # Maps tag -> dict with usage details
        
        # Extract tags from workload report (running pods)
        if workload_report:
            workload_map = workload_report.get('image_tags', workload_report)
            for tag, tag_info in workload_map.items():
                if tag_info.get('count', 0) > 0:
                    tags.add(tag)
                    if tag not in usage_info:
                        usage_info[tag] = {'pods': [], 'runs': [], 'workspaces': [], 'models': []}
                    pods_using = tag_info.get('pods', [])
                    if isinstance(pods_using, list):
                        usage_info[tag]['pods'] = pods_using
                    elif isinstance(pods_using, str):
                        usage_info[tag]['pods'] = [pods_using]
        
        # Extract tags from runs
        for record in mongodb_reports.get('runs', []):
            if 'environment_docker_tag' in record and record['environment_docker_tag']:
                tag = record['environment_docker_tag']
                tags.add(tag)
                if tag not in usage_info:
                    usage_info[tag] = {'pods': [], 'runs': [], 'workspaces': [], 'models': []}
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
                        usage_info[tag] = {'pods': [], 'runs': [], 'workspaces': [], 'models': []}
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
                    usage_info[tag] = {'pods': [], 'runs': [], 'workspaces': [], 'models': []}
                model_info = {
                    'model_id': record.get('model_id') or record.get('_id', 'unknown'),
                    'model_name': record.get('model_name', 'unknown'),
                    'version_id': record.get('model_version_id', 'unknown')
                }
                usage_info[tag]['models'].append(model_info)
        
        return tags, usage_info
    
    def get_usage_for_tag(self, tag: str, mongodb_reports: Dict[str, List[Dict]] = None, workload_report: Dict = None) -> Dict:
        """Get usage information for a specific tag
        
        Args:
            tag: Docker image tag to check
            mongodb_reports: Optional MongoDB usage reports
            workload_report: Optional workload report
        
        Returns:
            Dict with usage information: {'pods': [], 'runs': [], 'workspaces': [], 'models': []}
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        if not workload_report:
            workload_report = self.load_workload_report()
        
        _, usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports, workload_report)
        return usage_info.get(tag, {'pods': [], 'runs': [], 'workspaces': [], 'models': []})
    
    def generate_usage_summary(self, usage: Dict) -> str:
        """Generate a human-readable summary of why an image is in use
        
        Args:
            usage: Usage dictionary with 'pods', 'runs', 'workspaces', 'models' info
        
        Returns:
            Human-readable string describing usage
        """
        reasons = []
        
        if usage.get('pods'):
            pod_count = len(usage['pods'])
            reasons.append(f"{pod_count} running pod{'s' if pod_count > 1 else ''}")
        
        if usage.get('runs'):
            run_count = len(usage['runs'])
            reasons.append(f"{run_count} execution{'s' if run_count > 1 else ''} in MongoDB")
        
        if usage.get('workspaces'):
            ws_count = len(usage['workspaces'])
            reasons.append(f"{ws_count} workspace{'s' if ws_count > 1 else ''}")
        
        if usage.get('models'):
            model_count = len(usage['models'])
            reasons.append(f"{model_count} model{'s' if model_count > 1 else ''}")
        
        if not reasons:
            return "Referenced in system (source unknown)"
        
        return ", ".join(reasons)
    
    def check_tags_in_use(self, tags: List[str], mongodb_reports: Dict[str, List[Dict]] = None, workload_report: Dict = None) -> Tuple[Set[str], Dict[str, Dict]]:
        """Check which tags from a list are in use
        
        Args:
            tags: List of Docker image tags to check
            mongodb_reports: Optional MongoDB usage reports
            workload_report: Optional workload report
        
        Returns:
            Tuple of (set of tags that are in use, dict mapping tag -> usage info)
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        if not workload_report:
            workload_report = self.load_workload_report()
        
        all_used_tags, all_usage_info = self.extract_docker_tags_with_usage_info(mongodb_reports, workload_report)
        
        tags_set = set(tags)
        in_use_tags = tags_set.intersection(all_used_tags)
        
        # Build usage info for only the tags we're checking
        usage_info = {tag: all_usage_info.get(tag, {'pods': [], 'runs': [], 'workspaces': [], 'models': []}) for tag in in_use_tags}
        
        return in_use_tags, usage_info
    
    def find_usage_for_environment_ids(self, environment_ids: Set[str], mongodb_reports: Dict[str, List[Dict]] = None, workload_report: Dict = None) -> Dict[str, Dict]:
        """Find usage information for a set of environment/revision IDs
        
        This matches tags that contain these IDs (e.g., tags starting with the ObjectID).
        
        Args:
            environment_ids: Set of environment or revision ObjectIDs to find usage for
            mongodb_reports: Optional MongoDB usage reports
            workload_report: Optional workload report
        
        Returns:
            Dict mapping each environment_id to its usage info:
            {
                'environment_id': {
                    'matching_tags': [...],  # Tags that contain this ID
                    'workspaces': [...],     # Workspace records using these tags
                    'runs': [...],           # Run records using these tags
                    'pods': [...],           # Pods using these tags
                }
            }
        """
        if not mongodb_reports:
            mongodb_reports = self.load_mongodb_usage_reports()
        if not workload_report:
            workload_report = self.load_workload_report()
        
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
            'pods': [],
            'models': []
        } for env_id in environment_ids}
        
        # Check workload report for matching tags
        workload_map = workload_report.get('image_tags', workload_report)
        for tag, tag_info in workload_map.items():
            if _tag_matches_ids(tag):
                # Find which IDs this tag matches
                for env_id in environment_ids:
                    if tag.startswith(env_id) or env_id in tag:
                        usage_by_id[env_id]['matching_tags'].append(tag)
                        pods = tag_info.get('pods', [])
                        if isinstance(pods, list):
                            usage_by_id[env_id]['pods'].extend(pods)
                        elif isinstance(pods, str):
                            usage_by_id[env_id]['pods'].append(pods)
        
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