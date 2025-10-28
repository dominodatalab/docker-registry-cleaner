#!/usr/bin/env python3
"""
Find and optionally delete unused environment tags in MongoDB and Docker registry.

This script analyzes metadata from extract_metadata.py and inspect_workload.py to identify
environments that are not being used in workspaces, models, project defaults, scheduled jobs, or app versions.

Workflow:
- Auto-generate required reports if they don't exist (or use --generate-reports to force)
  - Extract model and workspace environment usage from MongoDB
  - Inspect Kubernetes workloads to find running containers
- Query MongoDB for all environments, project defaults, scheduled job environments, and app version environments
- Identify environments NOT being used anywhere
- Find Docker tags containing these unused environment ObjectIDs
- Generate a comprehensive report of unused environments and their sizes
- Optionally delete Docker images and clean up MongoDB records (with --apply)

Usage examples:
  # Find unused environments (auto-generates reports if missing)
  python delete_unused_environments.py
  
  # Force regeneration of reports
  python delete_unused_environments.py --generate-reports
  
  # Delete unused environments directly
  python delete_unused_environments.py --apply
  
  # Back up images to S3 before deletion (requires --s3-bucket)
  python delete_unused_environments.py --apply --backup

  # Optional: Back up images to S3 with custom bucket and region
  python delete_unused_environments.py --apply --backup --s3-bucket my-backup-bucket --region us-east-1

  # Optional: Override registry settings
  python delete_unused_environments.py --registry-url registry.example.com --repository my-repo

  # Optional: Custom output file
  python delete_unused_environments.py --output unused-envs.json

  # Optional:Delete unused environments from pre-generated file
  python delete_unused_environments.py --apply --input unused-envs.json
"""

import argparse
import json
import re
import sys

from bson import ObjectId
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import extract_metadata
from config_manager import config_manager, SkopeoClient
from image_data_analysis import ImageAnalyzer
from inspect_workload import WorkloadInspector
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json

logger = get_logger(__name__)


@dataclass
class UnusedEnvInfo:
    """Data class for unused environment information"""
    object_id: str
    env_name: str = ""
    image_type: str = ""
    tag: str = ""
    full_image: str = ""
    size_bytes: int = 0


class UnusedEnvironmentsFinder:
    """Main class for finding and managing unused environment tags"""
    
    def __init__(self, registry_url: str, repository: str, recent_days: Optional[int] = None,
                 enable_docker_deletion: bool = False, registry_statefulset_name: str = None):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(
            config_manager, 
            use_pod=config_manager.get_skopeo_use_pod(),
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset_name=registry_statefulset_name
        )
        self.logger = get_logger(__name__)
        
        # Image types to scan (only environment images contain environment ObjectIDs)
        self.image_types = ['environment']
        # Days window to consider a run as recent (None means ignore recency filter)
        self.recent_days = recent_days
    
    def extract_object_id_from_tag(self, tag: str) -> str:
        """Extract ObjectID from a Docker tag.
        
        Environment tags typically follow pattern: <ObjectID>-<revision>
        where ObjectID is either an environment ID or revision ID.
        
        This extracts the first 24-character hex string that looks like an ObjectID.
        """
        # ObjectIDs are 24 character hex strings
        pattern = r'([a-f0-9]{24})'
        match = re.search(pattern, tag)
        if match:
            return match.group(1)
        return ""
    
    def generate_required_reports(self) -> None:
        """Generate required metadata reports by calling extract_metadata and inspect_workload"""
        self.logger.info("Generating required metadata reports...")
        
        # Generate metadata from MongoDB
        self.logger.info("Extracting metadata from MongoDB...")
        try:
            extract_metadata.run("all")  # Run both model and workspace queries
            self.logger.info("✓ Metadata extraction completed")
        except Exception as e:
            self.logger.error(f"Failed to extract metadata: {e}")
            raise
        
        # Generate workload report from Kubernetes
        self.logger.info("Inspecting Kubernetes workloads...")
        try:
            inspector = WorkloadInspector(
                registry_url=self.registry_url,
                prefix_to_remove=f"{self.registry_url}/",
                namespace=config_manager.get_compute_namespace()
            )
            
            image_tags = inspector.analyze_pods_parallel(
                pod_prefixes=config_manager.get_pod_prefixes(),
                max_workers=config_manager.get_max_workers(),
                object_ids=None
            )
            
            # Generate report using config-managed path
            workload_json = Path(config_manager.get_workload_report_path())
            workload_base = str(workload_json.parent / workload_json.stem)
            inspector.generate_report(image_tags, workload_base)
            
            self.logger.info("✓ Workload inspection completed")
        except Exception as e:
            self.logger.error(f"Failed to inspect workloads: {e}")
            raise
    
    def load_metadata_files(self) -> tuple:
        """Load metadata files from extract_metadata.py and inspect_workload.py"""
        output_dir = config_manager.get_output_dir()
        
        # Load model environment usage
        model_env_file = Path(output_dir) / "model_env_usage_output.json"
        model_env_data = []
        if model_env_file.exists():
            try:
                with open(model_env_file, 'r') as f:
                    content = f.read()
                    # Handle MongoDB extended JSON format (ObjectId, ISODate)
                    # For simplicity, we'll extract the relevant IDs using regex
                    model_env_data = self._parse_mongodb_json(content)
                self.logger.info(f"Loaded {len(model_env_data)} model environment records")
            except Exception as e:
                self.logger.warning(f"Could not load model environment usage: {e}")
        else:
            self.logger.warning(f"Model environment usage file not found: {model_env_file}")
        
        # Load workspace environment usage
        workspace_env_file = Path(output_dir) / "workspace_env_usage_output.json"
        workspace_env_data = []
        if workspace_env_file.exists():
            try:
                with open(workspace_env_file, 'r') as f:
                    content = f.read()
                    workspace_env_data = self._parse_mongodb_json(content)
                self.logger.info(f"Loaded {len(workspace_env_data)} workspace environment records")
            except Exception as e:
                self.logger.warning(f"Could not load workspace environment usage: {e}")
        else:
            self.logger.warning(f"Workspace environment usage file not found: {workspace_env_file}")
        
        # Load workload report
        workload_file = Path(output_dir) / "workload-report.json"
        workload_data = {}
        if workload_file.exists():
            try:
                with open(workload_file, 'r') as f:
                    workload_data = json.load(f)
                self.logger.info(f"Loaded {len(workload_data)} workload tags")
            except Exception as e:
                self.logger.warning(f"Could not load workload report: {e}")
        else:
            self.logger.warning(f"Workload report file not found: {workload_file}")
        
        # Load runs usage file (new)
        runs_env_file = Path(output_dir) / "runs_env_usage_output.json"
        runs_env_data = []
        if runs_env_file.exists():
            try:
                with open(runs_env_file, 'r') as f:
                    runs_env_data = json.load(f)
                self.logger.info(f"Loaded {len(runs_env_data)} runs environment records")
            except Exception as e:
                self.logger.warning(f"Could not load runs environment usage: {e}")
        else:
            self.logger.warning(f"Runs environment usage file not found: {runs_env_file}")

        return model_env_data, workspace_env_data, workload_data, runs_env_data
    
    def _parse_mongodb_json(self, content: str) -> List[dict]:
        """Parse MongoDB extended JSON format to extract data"""
        # First try to parse as a standard JSON array
        try:
            cleaned = self._clean_mongodb_json(content)
            parsed = json.loads(cleaned)
            # If it's already a list, return it
            if isinstance(parsed, list):
                return parsed
            # If it's a single dict, wrap it in a list
            elif isinstance(parsed, dict):
                return [parsed]
        except:
            # Fall back to line-by-line parsing
            pass
        
        # Split by document boundaries (lines starting with {)
        lines = content.strip().split('\n')
        documents = []
        current_doc = ""
        
        for line in lines:
            if line.strip().startswith('{'):
                if current_doc:
                    # Try to parse the previous document
                    try:
                        # Convert MongoDB extended JSON to regular JSON
                        cleaned = self._clean_mongodb_json(current_doc)
                        doc = json.loads(cleaned)
                        # Handle case where doc might be a list
                        if isinstance(doc, list):
                            documents.extend(doc)
                        else:
                            documents.append(doc)
                    except:
                        pass
                current_doc = line
            else:
                current_doc += '\n' + line
        
        # Don't forget the last document
        if current_doc:
            try:
                cleaned = self._clean_mongodb_json(current_doc)
                doc = json.loads(cleaned)
                # Handle case where doc might be a list
                if isinstance(doc, list):
                    documents.extend(doc)
                else:
                    documents.append(doc)
            except:
                pass
        
        return documents
    
    def _clean_mongodb_json(self, json_str: str) -> str:
        """Clean MongoDB extended JSON to regular JSON"""
        # Remove ObjectId() wrapper
        json_str = re.sub(r'ObjectId\("([^"]+)"\)', r'"\1"', json_str)
        # Remove ISODate() wrapper
        json_str = re.sub(r'ISODate\("([^"]+)"\)', r'"\1"', json_str)
        return json_str
    
    def extract_used_environment_ids(self, model_env_data: List[dict], 
                                     workspace_env_data: List[dict],
                                     workload_data: Dict,
                                     runs_env_data: List[dict],
                                     recent_days: Optional[int]) -> Set[str]:
        """Extract all environment and revision IDs that are currently in use"""
        used_ids = set()
        
        # From model environment usage
        for record in model_env_data:
            if 'environment_id' in record:
                used_ids.add(str(record['environment_id']))
            
            # Also check active versions for environment_revision_ids
            for version in record.get('model_active_versions', []):
                if 'environment_revision_id' in version:
                    used_ids.add(str(version['environment_revision_id']))
        
        # From workspace environment usage - extract ObjectIDs from tags
        for record in workspace_env_data:
            if 'environment_docker_tag' in record:
                obj_id = self.extract_object_id_from_tag(record['environment_docker_tag'])
                if obj_id:
                    used_ids.add(obj_id)
            
            if 'project_default_environment_docker_tag' in record:
                obj_id = self.extract_object_id_from_tag(record['project_default_environment_docker_tag'])
                if obj_id:
                    used_ids.add(obj_id)
        
        # From workload report - extract ObjectIDs from running tags
        for tag in workload_data.keys():
            obj_id = self.extract_object_id_from_tag(tag)
            if obj_id:
                used_ids.add(obj_id)

        # From runs history - add environmentId and environmentRevisionId
        # If recent_days is provided, only count runs whose 'started' is within the window
        threshold = None
        if recent_days is not None and recent_days > 0:
            threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)

        for record in runs_env_data:
            # Filter by recency if requested (prefer last_used, then completed, then started)
            if threshold is not None:
                when_raw = record.get('last_used') or record.get('completed') or record.get('started')
                if not when_raw:
                    continue
                try:
                    # Handle ISO strings possibly ending with 'Z'
                    if isinstance(when_raw, str):
                        ts = when_raw.replace('Z', '+00:00')
                        when_dt = datetime.fromisoformat(ts)
                    else:
                        continue
                except Exception:
                    when_dt = None
                if when_dt is None or when_dt < threshold:
                    continue

            env_id = record.get('environment_id')
            if env_id:
                used_ids.add(str(env_id))
            rev_id = record.get('environment_revision_id')
            if rev_id:
                used_ids.add(str(rev_id))
        
        self.logger.info(f"Found {len(used_ids)} environment/revision IDs in use from metadata files")
        return used_ids
    
    def fetch_all_environments_and_defaults(self) -> tuple:
        """Fetch all environment IDs from MongoDB, project defaults, scheduled job environments, and app versions
        
        For app versions, only includes environments from app_versions that reference unarchived model_products.
        """
        mongo_client = get_mongo_client()
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            
            # Get all environments (not archived)
            environments_collection = db["environments_v2"]
            cursor = environments_collection.find({"isArchived": False}, {"_id": 1, "name": 1})
            
            all_env_ids = {}
            for doc in cursor:
                _id = doc.get("_id")
                name = doc.get("name", "")
                if _id is not None:
                    all_env_ids[str(_id)] = name
            
            self.logger.info(f"Found {len(all_env_ids)} total (non-archived) environments in MongoDB")
            
            # Get environment revisions
            revisions_collection = db["environment_revisions"]
            revision_cursor = revisions_collection.find({}, {"_id": 1, "environmentId": 1})
            
            all_revision_ids = {}
            for doc in revision_cursor:
                _id = doc.get("_id")
                env_id = doc.get("environmentId")
                if _id is not None:
                    # Store revision with its parent environment name
                    parent_name = all_env_ids.get(str(env_id), "")
                    all_revision_ids[str(_id)] = parent_name
            
            self.logger.info(f"Found {len(all_revision_ids)} environment revisions in MongoDB")
            
            # Get project default environments
            projects_collection = db["projects"]
            project_cursor = projects_collection.find({}, {"overrideV2EnvironmentId": 1})
            
            default_env_ids = set()
            for doc in project_cursor:
                env_id = doc.get("overrideV2EnvironmentId")
                if env_id is not None:
                    default_env_ids.add(str(env_id))
            
            self.logger.info(f"Found {len(default_env_ids)} project default environments")
            
            # Get scheduled job environments
            scheduler_jobs_collection = db["scheduler_jobs"]
            scheduler_cursor = scheduler_jobs_collection.find({}, {"jobDataPlain.overrideEnvironmentId": 1})
            
            scheduler_env_ids = set()
            for doc in scheduler_cursor:
                job_data = doc.get("jobDataPlain", {})
                env_id = job_data.get("overrideEnvironmentId")
                if env_id is not None:
                    scheduler_env_ids.add(str(env_id))
            
            self.logger.info(f"Found {len(scheduler_env_ids)} scheduled job environments")
            
            # Get app version environments (collection may not exist)
            app_version_env_ids = set()
            collection_names = db.list_collection_names()
            if "model_products" in collection_names and "app_versions" in collection_names:
                # First get unarchived model products
                model_products_collection = db["model_products"]
                model_products_cursor = model_products_collection.find(
                    {"isArchived": False}, 
                    {"_id": 1}
                )
                
                unarchived_product_ids = set()
                for doc in model_products_cursor:
                    product_id = doc.get("_id")
                    if product_id is not None:
                        unarchived_product_ids.add(product_id)
                
                self.logger.info(f"Found {len(unarchived_product_ids)} unarchived model products")
                
                # Then find app_versions that reference these unarchived products
                if unarchived_product_ids:
                    app_versions_collection = db["app_versions"]
                    app_versions_cursor = app_versions_collection.find(
                        {"appId": {"$in": list(unarchived_product_ids)}},
                        {"environmentId": 1}
                    )
                    
                    for doc in app_versions_cursor:
                        env_id = doc.get("environmentId")
                        if env_id is not None:
                            app_version_env_ids.add(str(env_id))
                    
                    self.logger.info(f"Found {len(app_version_env_ids)} app version environments from unarchived products")
                else:
                    self.logger.info("No unarchived model products found, skipping app version environment check")
            else:
                missing = []
                if "model_products" not in collection_names:
                    missing.append("model_products")
                if "app_versions" not in collection_names:
                    missing.append("app_versions")
                self.logger.info(f"Collections not found: {', '.join(missing)}, skipping app version environment check")
            
            return all_env_ids, all_revision_ids, default_env_ids, scheduler_env_ids, app_version_env_ids
            
        finally:
            mongo_client.close()
    
    def find_unused_environments(self) -> List[str]:
        """Find environment and revision IDs that are not being used anywhere"""
        # Load metadata files
        model_env_data, workspace_env_data, workload_data, runs_env_data = self.load_metadata_files()
        
        # Extract IDs that are in use
        used_ids = self.extract_used_environment_ids(
            model_env_data,
            workspace_env_data,
            workload_data,
            runs_env_data,
            recent_days=self.recent_days
        )
        
        # Get all environments and defaults from MongoDB
        all_env_ids, all_revision_ids, default_env_ids, scheduler_env_ids, app_version_env_ids = self.fetch_all_environments_and_defaults()
        
        # Add project defaults, scheduled job environments, and app version environments to used IDs
        used_ids.update(default_env_ids)
        used_ids.update(scheduler_env_ids)
        used_ids.update(app_version_env_ids)
        self.logger.info(f"Total used IDs (including project defaults, scheduled jobs, and app versions): {len(used_ids)}")
        
        # Combine all environment and revision IDs
        all_ids = {}
        all_ids.update(all_env_ids)
        all_ids.update(all_revision_ids)
        
        # Find unused IDs
        unused_ids = {}
        for obj_id, name in all_ids.items():
            if obj_id not in used_ids:
                unused_ids[obj_id] = name
        
        self.logger.info(f"Found {len(unused_ids)} unused environment/revision IDs")
        
        # Return list of UnusedEnvInfo objects
        unused_env_list = []
        for obj_id, name in unused_ids.items():
            unused_env_list.append(UnusedEnvInfo(
                object_id=obj_id,
                env_name=name
            ))
        
        return unused_env_list
    
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
    
    def find_matching_tags(self, unused_envs: List[UnusedEnvInfo]) -> List[UnusedEnvInfo]:
        """Find Docker tags that contain unused environment ObjectIDs"""
        unused_ids_dict = {env.object_id: env.env_name for env in unused_envs}
        unused_set = set(unused_ids_dict.keys())
        matching_tags = []
        
        for image_type in self.image_types:
            self.logger.info(f"Scanning {image_type} images for unused environment ObjectIDs...")
            tags = self.list_tags_for_image(image_type)
            self.logger.info(f"  Found {len(tags)} tags in {image_type}")
            
            for tag in tags:
                for obj_id in unused_set:
                    if obj_id in tag:
                        full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                        env_info = UnusedEnvInfo(
                            object_id=obj_id,
                            env_name=unused_ids_dict.get(obj_id, ""),
                            image_type=image_type,
                            tag=tag,
                            full_image=full_image
                        )
                        matching_tags.append(env_info)
        
        self.logger.info(f"Found {len(matching_tags)} matching tags for unused environments")
        return matching_tags
    
    def calculate_freed_space(self, unused_tags: List[UnusedEnvInfo]) -> int:
        """Calculate total space that would be freed by deleting unused environment tags.
        
        This method uses ImageAnalyzer to properly account for shared layers.
        Only layers that would have no remaining references after deletion are counted.
        
        Args:
            unused_tags: List of unused environment tags to analyze
            
        Returns:
            Total bytes that would be freed
        """
        if not unused_tags:
            return 0
        
        try:
            self.logger.info("Analyzing Docker images to calculate freed space...")
            
            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)
            
            # Get unique ObjectIDs from unused tags
            unique_ids = list(set(tag.object_id for tag in unused_tags))
            
            # Analyze environment images filtered by unused ObjectIDs
            for image_type in self.image_types:
                self.logger.info(f"Analyzing {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=unique_ids)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")
            
            # Build list of image_ids from unused tags
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = [f"{tag.image_type}:{tag.tag}" for tag in unused_tags]
            
            # Calculate individual size for each tag (layers unique to that image)
            self.logger.info("Calculating individual image sizes...")
            for tag in unused_tags:
                image_id = f"{tag.image_type}:{tag.tag}"
                # Calculate what would be freed if only this image was deleted
                tag.size_bytes = analyzer.freed_space_if_deleted([image_id])
            
            # Calculate freed space using ImageAnalyzer's method
            # This properly accounts for shared layers
            total_freed = analyzer.freed_space_if_deleted(image_ids)
            
            self.logger.info(f"Total space that would be freed: {total_freed / (1024**3):.2f} GB")
            
            return total_freed
            
        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return 0
    
    def delete_unused_tags(self, unused_tags: List[UnusedEnvInfo], backup: bool = False, s3_bucket: str = None, region: str = 'us-west-2') -> Dict[str, int]:
        """Delete unused Docker images and clean up MongoDB records
        
        Args:
            unused_tags: List of unused environment tags to delete
            backup: Whether to backup images to S3 before deletion
            s3_bucket: S3 bucket name for backups
            region: AWS region for S3 and ECR operations
        """
        if not unused_tags:
            self.logger.info("No unused tags to delete")
            return {}
        
        deletion_results = {
            'docker_images_deleted': 0,
            'mongo_environment_revisions_cleaned': 0,
            'mongo_environments_cleaned': 0,
            'images_backed_up': 0
        }
        
        # Backup images to S3 if requested
        if backup and s3_bucket:
            from backup_restore import process_backup
            from config_manager import ConfigManager
            
            self.logger.info(f"📦 Backing up {len(unused_tags)} images to S3 bucket: {s3_bucket}")
            
            # Prepare tags for backup_restore.process_backup
            tags_to_backup = [tag.tag for tag in unused_tags]
            full_repo = f"{self.registry_url}/{self.repository}"
            
            # Initialize ConfigManager and SkopeoClient for backup
            cfg_mgr = ConfigManager()
            from config_manager import SkopeoClient
            backup_skopeo_client = SkopeoClient(cfg_mgr, use_pod=cfg_mgr.get_skopeo_use_pod())
            
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
                    failed_tags_file=None
                )
                deletion_results['images_backed_up'] = len(tags_to_backup)
                self.logger.info(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as backup_err:
                self.logger.error(f"❌ Backup failed: {backup_err}")
                self.logger.error("Aborting deletion to prevent data loss")
                raise
        
        try:
            # Enable deletion in registry if it's in the same Kubernetes cluster
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            if registry_in_cluster:
                self.logger.info("Registry is in-cluster, enabling deletion...")
                if not self.skopeo_client.enable_registry_deletion():
                    self.logger.warning("Failed to enable registry deletion - continuing anyway")
            
            # Delete Docker images directly using skopeo
            # Track which ObjectIDs were successfully deleted so we only clean up their MongoDB records
            self.logger.info(f"Deleting {len(unused_tags)} Docker images from registry...")
            
            deleted_count = 0
            failed_deletions = []
            successfully_deleted_object_ids = set()
            
            for tag_info in unused_tags:
                try:
                    self.logger.info(f"  Deleting: {tag_info.full_image}")
                    success = self.skopeo_client.delete_image(
                        f"{self.repository}/{tag_info.image_type}",
                        tag_info.tag
                    )
                    if success:
                        deleted_count += 1
                        successfully_deleted_object_ids.add(tag_info.object_id)
                        self.logger.info(f"    ✓ Deleted successfully")
                    else:
                        failed_deletions.append(tag_info.full_image)
                        self.logger.warning(f"    ✗ Failed to delete - MongoDB record will NOT be cleaned")
                except Exception as e:
                    failed_deletions.append(tag_info.full_image)
                    self.logger.error(f"    ✗ Error deleting: {e} - MongoDB record will NOT be cleaned")
            
            deletion_results['docker_images_deleted'] = deleted_count
            
            # Disable deletion in registry if it was enabled
            if registry_in_cluster:
                self.logger.info("Disabling deletion in registry...")
                if not self.skopeo_client.disable_registry_deletion():
                    self.logger.warning("Failed to disable registry deletion")
            
            if failed_deletions:
                self.logger.warning(f"Failed to delete {len(failed_deletions)} Docker images:")
                for img in failed_deletions:
                    self.logger.warning(f"  - {img}")
                self.logger.warning("MongoDB records for failed deletions will be preserved.")
            
            # Clean up MongoDB records - ONLY for successfully deleted Docker images
            object_ids_to_clean = [oid for oid in successfully_deleted_object_ids]
            
            if object_ids_to_clean:
                mongo_client = get_mongo_client()
                
                try:
                    db = mongo_client[config_manager.get_mongo_db()]
                    
                    # First, try to delete from environment_revisions
                    self.logger.info(f"Cleaning up MongoDB records for {len(object_ids_to_clean)} successfully deleted images...")
                    environment_revisions_collection = db["environment_revisions"]
                    environments_collection = db["environments_v2"]
                    
                    for obj_id_str in object_ids_to_clean:
                        try:
                            obj_id = ObjectId(obj_id_str)
                            
                            # Try to delete from environment_revisions first
                            result = environment_revisions_collection.delete_one({"_id": obj_id})
                            if result.deleted_count > 0:
                                self.logger.info(f"  ✓ Deleted environment_revision: {obj_id_str}")
                                deletion_results['mongo_environment_revisions_cleaned'] += 1
                            else:
                                # If not found in revisions, try environments_v2
                                result = environments_collection.delete_one({"_id": obj_id})
                                if result.deleted_count > 0:
                                    self.logger.info(f"  ✓ Deleted environment: {obj_id_str}")
                                    deletion_results['mongo_environments_cleaned'] += 1
                                else:
                                    self.logger.warning(f"  ✗ Record not found in either collection: {obj_id_str}")
                        except Exception as e:
                            self.logger.error(f"  ✗ Error deleting MongoDB record {obj_id_str}: {e}")
                finally:
                    mongo_client.close()
            else:
                self.logger.info("No MongoDB records to clean (no Docker images were successfully deleted)")
            
            self.logger.info("Unused environment tag deletion completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error deleting unused tags: {e}")
            raise
        
        return deletion_results
    
    def generate_report(self, unused_envs: List[UnusedEnvInfo], 
                       unused_tags: List[UnusedEnvInfo], 
                       freed_space_bytes: int = 0) -> Dict:
        """Generate a comprehensive report of unused environments
        
        Args:
            unused_envs: List of all unused environment info objects (from MongoDB)
            unused_tags: List of unused tag info objects (from Docker)
            freed_space_bytes: Total bytes that would be freed by deletion (accounts for shared layers)
        """
        
        # Group by ObjectID
        by_object_id = {}
        
        for tag in unused_tags:
            obj_id = tag.object_id
            if obj_id not in by_object_id:
                by_object_id[obj_id] = []
            by_object_id[obj_id].append(tag)
        
        # Create summary statistics
        summary = {
            'total_unused_environment_ids': len(unused_envs),
            'total_matching_tags': len(unused_tags),
            'freed_space_gb': round(freed_space_bytes / (1024 * 1024 * 1024), 2),
            'object_ids_with_tags': len(by_object_id),
            'object_ids_without_tags': len(unused_envs) - len(by_object_id)
        }
        
        # Prepare grouped data
        grouped_data = {}
        for obj_id, tags in by_object_id.items():
            grouped_data[obj_id] = []
            for tag in tags:
                grouped_data[obj_id].append({
                    'object_id': tag.object_id,
                    'env_name': tag.env_name,
                    'image_type': tag.image_type,
                    'tag': tag.tag,
                    'full_image': tag.full_image,
                    'size_bytes': tag.size_bytes
                })
        
        report = {
            'summary': summary,
            'grouped_by_object_id': grouped_data,
            'metadata': {
                'registry_url': self.registry_url,
                'repository': self.repository,
                'image_types_scanned': self.image_types,
                'analysis_timestamp': datetime.now().isoformat()
            }
        }
        
        return report
    
    def load_unused_tags_from_file(self, file_path: str) -> List[UnusedEnvInfo]:
        """Load unused tags from a pre-generated report file"""
        try:
            with open(file_path, 'r') as f:
                report = json.load(f)
            
            unused_tags = []
            
            # Load from grouped_by_object_id
            grouped_data = report.get('grouped_by_object_id', {})
            for obj_id, tags_list in grouped_data.items():
                for tag_data in tags_list:
                    tag = UnusedEnvInfo(
                        object_id=tag_data['object_id'],
                        env_name=tag_data.get('env_name', ''),
                        image_type=tag_data['image_type'],
                        tag=tag_data['tag'],
                        full_image=tag_data['full_image'],
                        size_bytes=tag_data.get('size_bytes', 0)
                    )
                    unused_tags.append(tag)
            
            self.logger.info(f"Loaded {len(unused_tags)} unused tags from {file_path}")
            return unused_tags
            
        except Exception as e:
            self.logger.error(f"Error loading unused tags from {file_path}: {e}")
            raise


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Find and optionally delete unused environment tags in Docker registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find unused environment tags (auto-generates reports if missing)
  python delete_unused_environments.py

  # Force regeneration of metadata reports
  python delete_unused_environments.py --generate-reports

  # Override registry settings
  python delete_unused_environments.py --registry-url registry.example.com --repository my-repo

  # Custom output file
  python delete_unused_environments.py --output unused-envs.json

  # Delete unused environment tags directly (requires confirmation)
  python delete_unused_environments.py --apply

  # Delete unused environment tags from pre-generated file
  python delete_unused_environments.py --apply --input unused-envs.json

  # Force deletion without confirmation
  python delete_unused_environments.py --apply --force
  
  # Generate reports and then delete (full workflow)
  python delete_unused_environments.py --generate-reports --apply
        """
    )
    
    parser.add_argument(
        '--registry-url',
        help='Docker registry URL (default: from config)'
    )
    
    parser.add_argument(
        '--repository',
        help='Repository name (default: from config)'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path (default: reports/unused-environments.json)'
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete unused environment tags and clean up MongoDB (default: dry-run)'
    )
    
    parser.add_argument(
        '--input',
        help='Input file containing pre-generated unused tags to delete'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt when using --apply'
    )
    
    parser.add_argument(
        '--generate-reports',
        action='store_true',
        help='Generate required metadata reports (extract_metadata + inspect_workload) before analysis'
    )
    
    parser.add_argument(
        '--backup',
        action='store_true',
        help='Backup images to S3 before deletion (requires --s3-bucket)'
    )
    
    parser.add_argument(
        '--s3-bucket',
        help='S3 bucket for backups (optional if configured in config.yaml or S3_BUCKET env var)'
    )
    
    parser.add_argument(
        '--region',
        help='AWS region for S3 and ECR (default: from config or us-west-2)'
    )
    
    parser.add_argument(
        '--days',
        type=int,
        help='Only consider runs within the last N days as in-use (runs older than N days do not prevent deletion). If omitted, any historical run marks the environment as in-use.'
    )
    
    parser.add_argument(
        '--enable-docker-deletion',
        action='store_true',
        help='Enable registry deletion by treating registry as in-cluster (overrides auto-detection)'
    )
    
    parser.add_argument(
        '--registry-statefulset-name',
        default='docker-registry',
        help='Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)'
    )
    
    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
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
    output_file = args.output or str(Path(config_manager.get_output_dir()) / "unused-environments.json")
    
    try:
        # Determine operation mode
        is_delete_mode = args.apply
        use_input_file = args.input is not None
        
        logger.info("=" * 60)
        if is_delete_mode:
            logger.info("   Deleting unused environment tags")
        else:
            logger.info("   Finding unused environment tags")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")
        
        if use_input_file:
            logger.info(f"Input file: {args.input}")
        else:
            logger.info(f"Output file: {output_file}")
        
        # Create finder
        finder = UnusedEnvironmentsFinder(
            registry_url, 
            repository, 
            recent_days=args.days,
            enable_docker_deletion=args.enable_docker_deletion,
            registry_statefulset_name=args.registry_statefulset_name
        )
        
        # Check if reports need to be generated
        output_dir = config_manager.get_output_dir()
        reports_exist = all([
            (Path(output_dir) / "model_env_usage_output.json").exists(),
            (Path(output_dir) / "workspace_env_usage_output.json").exists(),
            (Path(output_dir) / "workload-report.json").exists()
        ])
        
        # Generate reports if requested or if they don't exist
        if args.generate_reports or (not use_input_file and not reports_exist):
            if not reports_exist:
                logger.info("Required metadata reports not found. Generating them now...")
            finder.generate_required_reports()
        
        # Handle different operation modes
        if use_input_file:
            # Mode 1: Delete from pre-generated file
            logger.info(f"Loading unused tags from {args.input}...")
            unused_tags = finder.load_unused_tags_from_file(args.input)
            unused_envs = []  # Not relevant for deletion mode
            
            if not unused_tags:
                logger.warning(f"No unused tags found in {args.input}")
                sys.exit(0)
                
        else:
            # Mode 2: Find unused tags (and optionally delete them)
            logger.info("Finding unused environments from metadata...")
            unused_envs = finder.find_unused_environments()
            
            if not unused_envs:
                logger.info("No unused environments found")
                # Still create an empty report
                empty_report = {
                    'summary': {
                        'total_unused_environment_ids': 0,
                        'total_matching_tags': 0,
                        'freed_space_gb': 0,
                        'object_ids_with_tags': 0,
                        'object_ids_without_tags': 0
                    },
                    'grouped_by_object_id': {},
                    'metadata': {
                        'registry_url': registry_url,
                        'repository': repository,
                        'image_types_scanned': finder.image_types,
                        'analysis_timestamp': datetime.now().isoformat()
                    }
                }
                save_json(output_file, empty_report)
                logger.info(f"Empty report written to {output_file}")
                sys.exit(0)
            
            logger.info("Finding matching Docker tags...")
            unused_tags = finder.find_matching_tags(unused_envs)
            
            if not unused_tags:
                logger.info("No matching Docker tags found for unused environments")
                # Still create a report with the environment IDs but no tags
                report = finder.generate_report(unused_envs, [], freed_space_bytes=0)
                save_json(output_file, report)
                logger.info(f"Report written to {output_file}")
                sys.exit(0)
        
        # Backup-only mode: allow backing up without deletion when --backup is provided without --apply
        if (not is_delete_mode) and args.backup:
            if not unused_tags:
                logger.info("No unused tags to back up")
                sys.exit(0)

            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to back up {len(unused_tags)} unused environment tags to S3!")
                logger.warning("This will upload tar archives to your configured S3 bucket.")
                response = input("\nProceed with backup only (no deletions)? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)

            # Execute backup only
            logger.info(f"\n📦 Backing up {len(unused_tags)} unused environment tags to S3 (no deletion)...")
            tags_to_backup = [t.tag for t in unused_tags]
            full_repo = f"{registry_url}/{repository}"

            from backup_restore import process_backup
            from config_manager import ConfigManager, SkopeoClient
            cfg_mgr = ConfigManager()
            backup_skopeo_client = SkopeoClient(cfg_mgr, use_pod=cfg_mgr.get_skopeo_use_pod())

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
            if not unused_tags:
                logger.info("No unused tags to delete")
                sys.exit(0)
            
            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to delete {len(unused_tags)} unused environment tags!")
                logger.warning("This will delete Docker images and clean up MongoDB records.")
                logger.warning("This action cannot be undone.")
                
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)
            
            logger.info(f"\n🗑️  Deleting {len(unused_tags)} unused environment tags...")
            deletion_results = finder.delete_unused_tags(
                unused_tags,
                backup=args.backup,
                s3_bucket=s3_bucket,
                region=s3_region
            )
            
            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_deleted = deletion_results.get('docker_images_deleted', 0)
            total_rev_cleaned = deletion_results.get('mongo_environment_revisions_cleaned', 0)
            total_env_cleaned = deletion_results.get('mongo_environments_cleaned', 0)
            logger.info(f"Total Docker images deleted: {total_deleted}")
            logger.info(f"Total environment_revisions cleaned: {total_rev_cleaned}")
            logger.info(f"Total environments_v2 cleaned: {total_env_cleaned}")
            
            logger.info("\n✅ Unused environment tags deletion completed successfully!")
            
        else:
            # Find mode - calculate freed space and generate report
            logger.info("Calculating freed space for unused tags...")
            freed_space_bytes = finder.calculate_freed_space(unused_tags)
            
            logger.info("Generating report...")
            report = finder.generate_report(unused_envs, unused_tags, freed_space_bytes)
            
            # Save report
            save_json(output_file, report)
            
            # Print summary
            summary = report['summary']
            logger.info("\n" + "=" * 60)
            logger.info("   UNUSED ENVIRONMENTS ANALYSIS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total unused environment IDs: {summary['total_unused_environment_ids']}")
            logger.info(f"Total matching tags: {summary['total_matching_tags']}")
            logger.info(f"Space that would be freed: {summary['freed_space_gb']:.2f} GB")
            logger.info(f"Environment IDs with tags: {summary['object_ids_with_tags']}")
            logger.info(f"Environment IDs without tags: {summary['object_ids_without_tags']}")
            
            logger.info(f"\nDetailed report saved to: {output_file}")
            
            if unused_tags:
                logger.warning(f"\n⚠️  Found {len(unused_tags)} unused environment tags that may need cleanup!")
                logger.info("Review the detailed report to identify which Docker images are not being used.")
                logger.info("Use --apply flag to delete these images and clean up MongoDB records.")
                logger.info("Or use --apply --input <file> to delete from a saved report.")
            else:
                logger.info("\n✅ No unused environment tags found!")
            
            logger.info("\n✅ Unused environments analysis completed successfully!")
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Operation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()

