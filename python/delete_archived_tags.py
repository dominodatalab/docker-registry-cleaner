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

from bson import ObjectId
from datetime import datetime
from typing import Dict, List, Tuple
from dataclasses import dataclass

from backup_restore import process_backup
from config_manager import config_manager, SkopeoClient, ConfigManager
from image_data_analysis import ImageAnalyzer
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json

logger = get_logger(__name__)


@dataclass
class ArchivedTagInfo:
    """Data class for archived tag information"""
    object_id: str
    image_type: str
    tag: str
    full_image: str
    size_bytes: int = 0
    record_type: str = None  # 'environment', 'revision', 'model', or 'version'


class ArchivedTagsFinder:
    """Main class for finding and managing archived tags"""
    
    def __init__(self, registry_url: str, repository: str, process_environments: bool = False, process_models: bool = False,
                 enable_docker_deletion: bool = False, registry_statefulset_name: str = None, max_workers: int = 4):
        self.registry_url = registry_url
        self.repository = repository
        self.max_workers = max_workers
        self.skopeo_client = SkopeoClient(
            config_manager, 
            use_pod=config_manager.get_skopeo_use_pod(),
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset_name=registry_statefulset_name
        )
        self.logger = get_logger(__name__)
        
        # Determine what to process
        self.process_environments = process_environments
        self.process_models = process_models
        
        # Determine image types to scan based on what we're processing
        self.image_types = []
        if self.process_environments:
            self.image_types.append('environment')
        if self.process_models:
            self.image_types.append('model')
        
        if not self.image_types:
            raise ValueError("Must specify at least one of --environment or --model")
    
    def fetch_archived_object_ids(self) -> Tuple[List[str], Dict[str, str]]:
        """Fetch archived ObjectIDs from MongoDB
        
        Returns:
            Tuple of (all_archived_ids, id_to_type_map) where id_to_type_map maps ObjectID to record type
        """
        mongo_client = get_mongo_client()
        all_archived_ids = []
        id_to_type_map = {}  # Maps ObjectID to 'environment', 'revision', 'model', or 'version'
        
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
                        id_to_type_map[obj_id_str] = 'environment'
                
                self.logger.info(f"Found {len(archived_environment_ids)} archived environment ObjectIDs")
                
                # Check environment_revisions for documents with matching environmentId
                environment_revisions_collection = db["environment_revisions"]
                archived_revision_ids = []
                
                if archived_environment_ids:
                    environment_object_ids = [ObjectId(env_id) for env_id in archived_environment_ids]
                    revision_cursor = environment_revisions_collection.find(
                        {"environmentId": {"$in": environment_object_ids}}, 
                        {"_id": 1}
                    )
                    
                    for doc in revision_cursor:
                        _id = doc.get("_id")
                        if _id is not None:
                            obj_id_str = str(_id)
                            archived_revision_ids.append(obj_id_str)
                            id_to_type_map[obj_id_str] = 'revision'
                    
                    self.logger.info(f"Found {len(archived_revision_ids)} environment revision ObjectIDs for archived environments")
                
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
                        id_to_type_map[obj_id_str] = 'model'
                
                self.logger.info(f"Found {len(archived_model_ids)} archived model ObjectIDs")
                
                # Check model_versions for documents with matching modelId.value
                model_versions_collection = db["model_versions"]
                archived_version_ids = []
                
                if archived_model_ids:
                    model_object_ids = [ObjectId(model_id) for model_id in archived_model_ids]
                    version_cursor = model_versions_collection.find(
                        {"modelId.value": {"$in": model_object_ids}}, 
                        {"_id": 1}
                    )
                    
                    for doc in version_cursor:
                        _id = doc.get("_id")
                        if _id is not None:
                            obj_id_str = str(_id)
                            archived_version_ids.append(obj_id_str)
                            id_to_type_map[obj_id_str] = 'version'
                    
                    self.logger.info(f"Found {len(archived_version_ids)} model version ObjectIDs for archived models")
                
                all_archived_ids.extend(archived_model_ids)
                all_archived_ids.extend(archived_version_ids)
            
            # Remove duplicates while preserving order
            unique_ids = list(dict.fromkeys(all_archived_ids))
            
            self.logger.info(f"Total archived ObjectIDs to search for: {len(unique_ids)}")
            return unique_ids, id_to_type_map
            
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
    
    def find_matching_tags(self, archived_ids: List[str], id_to_type_map: Dict[str, str]) -> List[ArchivedTagInfo]:
        """Find Docker tags that contain archived ObjectIDs"""
        archived_set = set(archived_ids)
        matching_tags = []
        
        for image_type in self.image_types:
            self.logger.info(f"Scanning {image_type} images for archived ObjectIDs...")
            tags = self.list_tags_for_image(image_type)
            self.logger.info(f"  Found {len(tags)} tags in {image_type}")
            
            for tag in tags:
                for obj_id in archived_set:
                    # Use prefix matching (not substring) since tags format is: <objectid>-<version/revision>
                    if tag.startswith(obj_id + '-') or tag == obj_id:
                        full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                        tag_info = ArchivedTagInfo(
                            object_id=obj_id,
                            image_type=image_type,
                            tag=tag,
                            full_image=full_image,
                            record_type=id_to_type_map.get(obj_id, 'unknown')
                        )
                        matching_tags.append(tag_info)
        
        self.logger.info(f"Found {len(matching_tags)} matching tags for archived ObjectIDs")
        return matching_tags
    
    def calculate_freed_space(self, archived_tags: List[ArchivedTagInfo]) -> int:
        """Calculate total space that would be freed by deleting archived tags.
        
        This method uses ImageAnalyzer to properly account for shared layers.
        Only layers that would have no remaining references after deletion are counted.
        
        IMPORTANT: This analyzes ALL images in the registry (not just archived ones)
        to get accurate reference counts. This ensures we don't overestimate freed space
        by accounting for shared layers between archived and non-archived images.
        
        Args:
            archived_tags: List of archived tags to analyze
            
        Returns:
            Total bytes that would be freed
        """
        if not archived_tags:
            return 0
        
        try:
            self.logger.info(f"Analyzing ALL Docker images to calculate accurate freed space (using {self.max_workers} workers)...")
            self.logger.info("This analyzes all images (not just archived) to count shared layer references correctly.")
            
            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)
            
            # CRITICAL FIX: Analyze ALL images (not just archived ones) to get accurate reference counts
            # This ensures that shared layers between archived and non-archived images are properly accounted for
            for image_type in self.image_types:
                self.logger.info(f"Analyzing ALL {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=None, max_workers=self.max_workers)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")
            
            # Build list of image_ids from archived tags
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = [f"{tag.image_type}:{tag.tag}" for tag in archived_tags]
            
            # Calculate freed space using ImageAnalyzer's method
            # This properly accounts for shared layers - only counts layers that would have
            # zero references after deletion (i.e., not used by any remaining images)
            total_freed = analyzer.freed_space_if_deleted(image_ids)
            
            self.logger.info(f"Total space that would be freed: {total_freed / (1024**3):.2f} GB")
            
            return total_freed
            
        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return 0
    
    def delete_archived_tags(self, archived_tags: List[ArchivedTagInfo], backup: bool = False, s3_bucket: str = None, region: str = 'us-west-2') -> Dict[str, int]:
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
        
        deletion_results = {
            'docker_images_deleted': 0,
            'mongo_records_cleaned': 0,
            'images_backed_up': 0
        }
        
        try:
            # Backup images to S3 if requested
            if backup and s3_bucket:
                self.logger.info(f"📦 Backing up {len(archived_tags)} images to S3 bucket: {s3_bucket}")
                
                # Prepare tags for backup_restore.process_backup
                tags_to_backup = [tag.tag for tag in archived_tags]
                full_repo = f"{self.registry_url}/{self.repository}"
                
                # Initialize ConfigManager and SkopeoClient for backup
                cfg_mgr = ConfigManager()
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
            
            # Enable deletion in registry if it's in the same Kubernetes cluster
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            if registry_in_cluster:
                self.logger.info("Registry is in-cluster, enabling deletion...")
                if not self.skopeo_client.enable_registry_deletion():
                    self.logger.warning("Failed to enable registry deletion - continuing anyway")
            
            try:
                # Deduplicate tags before deletion (a tag may appear multiple times if it contains multiple ObjectIDs)
                # Build a mapping from unique tags to all their associated ObjectIDs
                unique_tags = {}  # key: (image_type, tag), value: list of ObjectIDs
                for tag_info in archived_tags:
                    key = (tag_info.image_type, tag_info.tag)
                    if key not in unique_tags:
                        unique_tags[key] = {
                            'tag_info': tag_info,
                            'object_ids': []
                        }
                    unique_tags[key]['object_ids'].append(tag_info.object_id)
                
                self.logger.info(f"Deleting {len(unique_tags)} unique Docker images from registry ({len(archived_tags)} total references)...")
                if len(unique_tags) < len(archived_tags):
                    self.logger.info(f"  Note: {len(archived_tags) - len(unique_tags)} tags contain multiple archived ObjectIDs")
                
                deleted_count = 0
                failed_deletions = []
                successfully_deleted_object_ids = set()
                
                for (image_type, tag), data in unique_tags.items():
                    tag_info = data['tag_info']
                    associated_object_ids = data['object_ids']
                    try:
                        self.logger.info(f"  Deleting: {tag_info.full_image}")
                        success = self.skopeo_client.delete_image(
                            f"{self.repository}/{tag_info.image_type}",
                            tag_info.tag
                        )
                        if success:
                            deleted_count += 1
                            # Add all ObjectIDs associated with this tag
                            successfully_deleted_object_ids.update(associated_object_ids)
                            if len(associated_object_ids) > 1:
                                self.logger.info(f"    ✓ Deleted successfully (contains {len(associated_object_ids)} archived ObjectIDs)")
                            else:
                                self.logger.info(f"    ✓ Deleted successfully")
                        else:
                            failed_deletions.append(tag_info.full_image)
                            self.logger.warning(f"    ✗ Failed to delete - MongoDB record will NOT be cleaned")
                    except Exception as e:
                        failed_deletions.append(tag_info.full_image)
                        self.logger.error(f"    ✗ Error deleting: {e} - MongoDB record will NOT be cleaned")
                
                deletion_results['docker_images_deleted'] = deleted_count
                
                if failed_deletions:
                    self.logger.warning(f"Failed to delete {len(failed_deletions)} Docker images:")
                    for img in failed_deletions:
                        self.logger.warning(f"  - {img}")
                    self.logger.warning("MongoDB records for failed deletions will be preserved.")
                
                # Clean up MongoDB records - ONLY for successfully deleted Docker images
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
                        if 'environment' in ids_by_type:
                            environments_collection = db["environments_v2"]
                            self.logger.info(f"Cleaning up {len(ids_by_type['environment'])} environment records from MongoDB...")
                            for obj_id_str in ids_by_type['environment']:
                                try:
                                    obj_id = ObjectId(obj_id_str)
                                    result = environments_collection.delete_one({"_id": obj_id})
                                    if result.deleted_count > 0:
                                        self.logger.info(f"  ✓ Deleted environment: {obj_id_str}")
                                        deletion_results['mongo_records_cleaned'] += 1
                                    else:
                                        self.logger.warning(f"  ✗ Environment not found: {obj_id_str}")
                                except Exception as e:
                                    self.logger.error(f"  ✗ Error deleting environment {obj_id_str}: {e}")
                        
                        # Clean up environment revision records
                        if 'revision' in ids_by_type:
                            environment_revisions_collection = db["environment_revisions"]
                            self.logger.info(f"Cleaning up {len(ids_by_type['revision'])} environment_revision records from MongoDB...")
                            for obj_id_str in ids_by_type['revision']:
                                try:
                                    obj_id = ObjectId(obj_id_str)
                                    result = environment_revisions_collection.delete_one({"_id": obj_id})
                                    if result.deleted_count > 0:
                                        self.logger.info(f"  ✓ Deleted environment_revision: {obj_id_str}")
                                        deletion_results['mongo_records_cleaned'] += 1
                                    else:
                                        self.logger.warning(f"  ✗ Environment_revision not found: {obj_id_str}")
                                except Exception as e:
                                    self.logger.error(f"  ✗ Error deleting environment_revision {obj_id_str}: {e}")
                        
                        # Clean up model records
                        if 'model' in ids_by_type:
                            models_collection = db["models"]
                            self.logger.info(f"Cleaning up {len(ids_by_type['model'])} model records from MongoDB...")
                            for obj_id_str in ids_by_type['model']:
                                try:
                                    obj_id = ObjectId(obj_id_str)
                                    result = models_collection.delete_one({"_id": obj_id})
                                    if result.deleted_count > 0:
                                        self.logger.info(f"  ✓ Deleted model: {obj_id_str}")
                                        deletion_results['mongo_records_cleaned'] += 1
                                    else:
                                        self.logger.warning(f"  ✗ Model not found: {obj_id_str}")
                                except Exception as e:
                                    self.logger.error(f"  ✗ Error deleting model {obj_id_str}: {e}")
                        
                        # Clean up model version records
                        if 'version' in ids_by_type:
                            model_versions_collection = db["model_versions"]
                            self.logger.info(f"Cleaning up {len(ids_by_type['version'])} model_version records from MongoDB...")
                            for obj_id_str in ids_by_type['version']:
                                try:
                                    obj_id = ObjectId(obj_id_str)
                                    result = model_versions_collection.delete_one({"_id": obj_id})
                                    if result.deleted_count > 0:
                                        self.logger.info(f"  ✓ Deleted model_version: {obj_id_str}")
                                        deletion_results['mongo_records_cleaned'] += 1
                                    else:
                                        self.logger.warning(f"  ✗ Model_version not found: {obj_id_str}")
                                except Exception as e:
                                    self.logger.error(f"  ✗ Error deleting model_version {obj_id_str}: {e}")
                    finally:
                        mongo_client.close()
                else:
                    self.logger.info("No MongoDB records to clean (no Docker images were successfully deleted)")
            
                self.logger.info("Archived tag deletion completed successfully")
                
            finally:
                # Always disable deletion in registry if it was enabled
                if registry_in_cluster:
                    self.logger.info("Disabling deletion in registry...")
                    if not self.skopeo_client.disable_registry_deletion():
                        self.logger.warning("Failed to disable registry deletion")
            
        except Exception as e:
            self.logger.error(f"Error deleting archived tags: {e}")
            raise
        
        return deletion_results
    
    def generate_report(self, archived_ids: List[str], archived_tags: List[ArchivedTagInfo], 
                       id_to_type_map: Dict[str, str], freed_space_bytes: int = 0) -> Dict:
        """Generate a comprehensive report of archived tags
        
        Args:
            archived_ids: List of all archived ObjectIDs
            archived_tags: List of archived tag info objects
            id_to_type_map: Mapping of ObjectID to record type
            freed_space_bytes: Total bytes that would be freed by deletion (accounts for shared layers)
        """
        
        # Categorize IDs by type
        ids_by_type = {
            'environment': [],
            'revision': [],
            'model': [],
            'version': []
        }
        
        for obj_id in archived_ids:
            record_type = id_to_type_map.get(obj_id, 'unknown')
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
        
        # Create summary statistics
        summary = {
            'total_archived_object_ids': len(archived_ids),
            'archived_environment_ids': len(ids_by_type['environment']),
            'archived_revision_ids': len(ids_by_type['revision']),
            'archived_model_ids': len(ids_by_type['model']),
            'archived_version_ids': len(ids_by_type['version']),
            'total_matching_tags': len(archived_tags),
            'freed_space_gb': round(freed_space_bytes / (1024 * 1024 * 1024), 2),
            'tags_by_image_type': by_image_type,
            'object_ids_with_tags': len(by_object_id),
            'object_ids_without_tags': len(archived_ids) - len(by_object_id)
        }
        
        # Prepare detailed data
        detailed_tags = []
        for tag in archived_tags:
            detailed_tags.append({
                'object_id': tag.object_id,
                'image_type': tag.image_type,
                'tag': tag.tag,
                'full_image': tag.full_image
            })
        
        report = {
            'summary': summary,
            'archived_object_ids': archived_ids,
            'archived_environment_ids': ids_by_type['environment'],
            'archived_revision_ids': ids_by_type['revision'],
            'archived_model_ids': ids_by_type['model'],
            'archived_version_ids': ids_by_type['version'],
            'archived_tags': detailed_tags,
            'grouped_by_object_id': {
                obj_id: [tag.to_dict() if hasattr(tag, 'to_dict') else tag.__dict__ for tag in tags]
                for obj_id, tags in by_object_id.items()
            },
            'metadata': {
                'registry_url': self.registry_url,
                'repository': self.repository,
                'image_types_scanned': self.image_types,
                'process_environments': self.process_environments,
                'process_models': self.process_models,
                'analysis_timestamp': datetime.now().isoformat()
            }
        }
        
        return report
    
    def load_archived_tags_from_file(self, file_path: str) -> List[ArchivedTagInfo]:
        """Load archived tags from a pre-generated report file"""
        try:
            with open(file_path, 'r') as f:
                report = json.load(f)
            
            archived_tags = []
            for tag_data in report.get('archived_tags', []):
                tag = ArchivedTagInfo(
                    object_id=tag_data['object_id'],
                    image_type=tag_data['image_type'],
                    tag=tag_data['tag'],
                    full_image=tag_data['full_image'],
                    size_bytes=tag_data.get('size_bytes', 0),
                    record_type=tag_data.get('record_type', tag_data['image_type'])  # Default to image_type if not present
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
        """
    )
    
    parser.add_argument(
        '--environment',
        action='store_true',
        help='Process archived environments and environment revisions'
    )
    
    parser.add_argument(
        '--model',
        action='store_true',
        help='Process archived models and model versions'
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
        help='Output file path (default: reports/archived-tags.json)'
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete archived tags and clean up MongoDB (default: dry-run)'
    )
    
    parser.add_argument(
        '--input',
        help='Input file containing pre-generated archived tags to delete'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt when using --apply'
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
        '--enable-docker-deletion',
        action='store_true',
        help='Enable registry deletion by treating registry as in-cluster (overrides auto-detection)'
    )
    
    parser.add_argument(
        '--registry-statefulset-name',
        default='docker-registry',
        help='Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)'
    )
    
    parser.add_argument(
        '--max-workers',
        type=int,
        help='Maximum number of parallel workers for tag inspection (default: from config)'
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
    max_workers = args.max_workers or config_manager.get_max_workers()
    
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
            logger.info(f"   Deleting archived {processing_str} tags")
        else:
            logger.info(f"   Finding archived {processing_str} tags")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")
        logger.info(f"Max Workers: {max_workers}")
        
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
            registry_statefulset_name=args.registry_statefulset_name,
            max_workers=max_workers
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
            archived_ids, id_to_type_map = finder.fetch_archived_object_ids()
            
            if not archived_ids:
                logger.info(f"No archived {processing_str} ObjectIDs found")
                # Still create an empty report
                empty_report = {
                    'summary': {
                        'total_archived_object_ids': 0,
                        'archived_environment_ids': 0,
                        'archived_revision_ids': 0,
                        'archived_model_ids': 0,
                        'archived_version_ids': 0,
                        'total_matching_tags': 0,
                        'freed_space_gb': 0,
                        'tags_by_image_type': {img_type: 0 for img_type in finder.image_types},
                        'object_ids_with_tags': 0,
                        'object_ids_without_tags': 0
                    },
                    'archived_object_ids': [],
                    'archived_environment_ids': [],
                    'archived_revision_ids': [],
                    'archived_model_ids': [],
                    'archived_version_ids': [],
                    'archived_tags': [],
                    'grouped_by_object_id': {},
                    'metadata': {
                        'registry_url': registry_url,
                        'repository': repository,
                        'image_types_scanned': finder.image_types,
                        'process_environments': args.environment,
                        'process_models': args.model,
                        'analysis_timestamp': datetime.now().isoformat()
                    }
                }
                save_json(output_file, empty_report)
                logger.info(f"Empty report written to {output_file}")
                sys.exit(0)
            
            logger.info("Finding matching Docker tags...")
            archived_tags = finder.find_matching_tags(archived_ids, id_to_type_map)
            
            if not archived_tags:
                logger.info("No matching Docker tags found for archived ObjectIDs")
                # Still create a report with the ObjectIDs but no tags
                report = finder.generate_report(archived_ids, [], id_to_type_map, freed_space_bytes=0)
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
                logger.warning(f"\n⚠️  WARNING: About to back up {len(archived_tags)} archived {processing_str} tags to S3!")
                logger.warning("This will upload tar archives to your configured S3 bucket.")
                response = input("\nProceed with backup only (no deletions)? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)

            # Execute backup only
            logger.info(f"\n📦 Backing up {len(archived_tags)} archived {processing_str} tags to S3 (no deletion)...")
            tags_to_backup = [t.tag for t in archived_tags]
            full_repo = f"{registry_url}/{repository}"

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
            if not archived_tags:
                logger.info("No archived tags to delete")
                sys.exit(0)
            
            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to delete {len(archived_tags)} archived {processing_str} tags!")
                logger.warning("This will delete Docker images and clean up MongoDB records.")
                logger.warning("This action cannot be undone.")
                
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)
            
            logger.info(f"\n🗑️  Deleting {len(archived_tags)} archived {processing_str} tags...")
            deletion_results = finder.delete_archived_tags(
                archived_tags,
                backup=args.backup,
                s3_bucket=s3_bucket,
                region=s3_region
            )
            
            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_backed_up = deletion_results.get('images_backed_up', 0)
            total_deleted = deletion_results.get('docker_images_deleted', 0)
            total_cleaned = deletion_results.get('mongo_records_cleaned', 0)
            if total_backed_up > 0:
                logger.info(f"Total images backed up to S3: {total_backed_up}")
            logger.info(f"Total Docker images deleted: {total_deleted}")
            logger.info(f"Total MongoDB records cleaned: {total_cleaned}")
            
            logger.info(f"\n✅ Archived {processing_str} tags deletion completed successfully!")
            
        else:
            # Find mode - calculate freed space and generate report
            logger.info("Calculating freed space for archived tags...")
            freed_space_bytes = finder.calculate_freed_space(archived_tags)
            
            logger.info("Generating report...")
            report = finder.generate_report(archived_ids, archived_tags, id_to_type_map, freed_space_bytes)
            
            # Save report
            save_json(output_file, report)
            
            # Print summary
            summary = report['summary']
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
            logger.info(f"Space that would be freed: {summary['freed_space_gb']:.2f} GB")
            logger.info(f"Tags by image type:")
            for img_type, count in summary['tags_by_image_type'].items():
                logger.info(f"  {img_type}: {count} tags")
            logger.info(f"ObjectIDs with tags: {summary['object_ids_with_tags']}")
            logger.info(f"ObjectIDs without tags: {summary['object_ids_without_tags']}")
            
            logger.info(f"\nDetailed report saved to: {output_file}")
            
            if archived_tags:
                logger.warning(f"\n⚠️  Found {len(archived_tags)} archived {processing_str} tags that may need cleanup!")
                logger.info("Review the detailed report to identify which Docker images are associated with archived records.")
                logger.info("Use --apply flag to delete these images and clean up MongoDB records.")
                logger.info("Or use --apply --input <file> to delete from a saved report.")
            else:
                logger.info(f"\n✅ No archived {processing_str} tags found!")
            
            logger.info(f"\n✅ Archived {processing_str} tags analysis completed successfully!")
        
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

