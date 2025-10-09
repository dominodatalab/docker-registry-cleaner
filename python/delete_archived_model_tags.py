#!/usr/bin/env python3
"""
Find and optionally delete archived model tags in MongoDB and Docker registry.

This script queries MongoDB for archived model ObjectIDs and finds matching
Docker tags in the registry. Can optionally delete the Docker images and clean up
MongoDB records.

Workflow:
- Query MongoDB models collection for documents where isArchived == true
- Query model_versions collection for documents with modelId.value matching archived models
- Extract ObjectIDs from both archived models and their versions
- Find Docker tags containing these ObjectIDs in environment and model images
- Generate a comprehensive report of archived tags and their sizes
- Optionally delete Docker images and clean up MongoDB records (with --apply)

Usage examples:
  # Find archived model tags (dry-run)
  python delete_archived_model_tags.py --registry-url docker-registry:5000 --repository dominodatalab
  
  # Delete archived model tags directly
  python delete_archived_model_tags.py --apply
  
  # Delete archived model tags from pre-generated file
  python delete_archived_model_tags.py --apply --input archived-model-tags.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List
from dataclasses import dataclass

from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json
from image_data_analysis import ImageAnalyzer

logger = get_logger(__name__)


@dataclass
class ArchivedModelTagInfo:
    """Data class for archived model tag information"""
    object_id: str
    image_type: str
    tag: str
    full_image: str
    size_bytes: int = 0
    context: Dict = None

    def __post_init__(self):
        if self.context is None:
            self.context = {}


class ArchivedModelTagsFinder:
    """Main class for finding and managing archived model tags"""
    
    def __init__(self, registry_url: str, repository: str):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(config_manager, use_pod=False)
        self.logger = get_logger(__name__)
        
        # Image types to scan
        self.image_types = ['environment', 'model']
    
    def fetch_archived_object_ids(self) -> List[str]:
        """Fetch archived ObjectIDs from MongoDB models collection and related model_versions"""
        mongo_client = get_mongo_client()
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            models_collection = db["models"]
            
            # Query for archived models
            cursor = models_collection.find({"isArchived": True}, {"_id": 1})
            archived_model_ids = []
            
            for doc in cursor:
                _id = doc.get("_id")
                if _id is not None:
                    archived_model_ids.append(str(_id))
            
            self.logger.info(f"Found {len(archived_model_ids)} archived model ObjectIDs")
            
            # Now check model_versions for documents with matching modelId.value
            model_versions_collection = db["model_versions"]
            archived_version_ids = []
            
            if archived_model_ids:
                # Convert string IDs back to ObjectId for the query
                from bson import ObjectId
                model_object_ids = [ObjectId(model_id) for model_id in archived_model_ids]
                
                # Find model versions that belong to archived models
                version_cursor = model_versions_collection.find(
                    {"modelId.value": {"$in": model_object_ids}}, 
                    {"_id": 1}
                )
                
                for doc in version_cursor:
                    _id = doc.get("_id")
                    if _id is not None:
                        archived_version_ids.append(str(_id))
                
                self.logger.info(f"Found {len(archived_version_ids)} model version ObjectIDs for archived models")
            
            # Combine both sets of IDs (model IDs and version IDs)
            all_archived_ids = list(set(archived_model_ids + archived_version_ids))
            
            self.logger.info(f"Total archived ObjectIDs to search for: {len(all_archived_ids)}")
            return all_archived_ids
            
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
    
    def find_matching_tags(self, archived_ids: List[str]) -> List[ArchivedModelTagInfo]:
        """Find Docker tags that contain archived ObjectIDs"""
        archived_set = set(archived_ids)
        matching_tags = []
        
        for image_type in self.image_types:
            self.logger.info(f"Scanning {image_type} images for archived ObjectIDs...")
            tags = self.list_tags_for_image(image_type)
            self.logger.info(f"  Found {len(tags)} tags in {image_type}")
            
            for tag in tags:
                for obj_id in archived_set:
                    if obj_id in tag:
                        full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                        tag_info = ArchivedModelTagInfo(
                            object_id=obj_id,
                            image_type=image_type,
                            tag=tag,
                            full_image=full_image,
                            context={
                                'repository': self.repository,
                                'image_type': image_type,
                                'tag': tag,
                                'full_image': full_image
                            }
                        )
                        matching_tags.append(tag_info)
        
        self.logger.info(f"Found {len(matching_tags)} matching tags for archived ObjectIDs")
        return matching_tags
    
    def calculate_freed_space(self, archived_tags: List[ArchivedModelTagInfo]) -> int:
        """Calculate total space that would be freed by deleting archived tags.
        
        This method uses ImageAnalyzer to properly account for shared layers.
        Only layers that would have no remaining references after deletion are counted.
        
        Args:
            archived_tags: List of archived tags to analyze
            
        Returns:
            Total bytes that would be freed
        """
        if not archived_tags:
            return 0
        
        try:
            self.logger.info("Analyzing Docker images to calculate freed space...")
            
            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)
            
            # Get unique ObjectIDs from archived tags
            unique_ids = list(set(tag.object_id for tag in archived_tags))
            
            # Analyze both environment and model images filtered by archived ObjectIDs
            for image_type in self.image_types:
                self.logger.info(f"Analyzing {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=unique_ids)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")
            
            # Build list of image_ids from archived tags
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = [f"{tag.image_type}:{tag.tag}" for tag in archived_tags]
            
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
    
    def delete_archived_tags(self, archived_tags: List[ArchivedModelTagInfo]) -> Dict[str, int]:
        """Delete archived Docker images and clean up MongoDB records"""
        self.logger.info(f"[DEBUG] delete_archived_tags called with {len(archived_tags)} tags")
        sys.stdout.flush()
        
        if not archived_tags:
            self.logger.info("No archived tags to delete")
            return {}
        
        # Separate model IDs from version IDs for proper MongoDB cleanup
        self.logger.info("[DEBUG] Connecting to MongoDB to categorize IDs...")
        sys.stdout.flush()
        mongo_client = get_mongo_client()
        self.logger.info("[DEBUG] MongoDB client obtained")
        sys.stdout.flush()
        model_ids = []
        version_ids = []
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            models_collection = db["models"]
            
            # Categorize ObjectIDs
            unique_ids = list(set(tag.object_id for tag in archived_tags))
            self.logger.info(f"[DEBUG] Categorizing {len(unique_ids)} unique ObjectIDs...")
            
            for obj_id_str in unique_ids:
                try:
                    from bson import ObjectId
                    obj_id = ObjectId(obj_id_str)
                    if models_collection.find_one({"_id": obj_id}):
                        model_ids.append(obj_id_str)
                    else:
                        version_ids.append(obj_id_str)
                except:
                    version_ids.append(obj_id_str)
            
            self.logger.info(f"[DEBUG] Found {len(model_ids)} model IDs and {len(version_ids)} version IDs")
        finally:
            mongo_client.close()
            self.logger.info("[DEBUG] MongoDB connection closed")
        
        # Create temporary files for delete_image.py
        temp_ids_file = os.path.join(config_manager.get_output_dir(), "temp_delete_model_ids.txt")
        os.makedirs(os.path.dirname(temp_ids_file), exist_ok=True)
        
        deletion_results = {
            'docker_images_deleted': 0,
            'mongo_records_cleaned': 0
        }
        
        try:
            # Write all unique ObjectIDs to file (for Docker deletion)
            self.logger.info("[DEBUG] Writing ObjectIDs to temp file...")
            unique_ids = list(set(tag.object_id for tag in archived_tags))
            with open(temp_ids_file, "w") as f:
                for obj_id in unique_ids:
                    f.write(f"{obj_id}\n")
            self.logger.info(f"[DEBUG] Wrote {len(unique_ids)} IDs to {temp_ids_file}")
            
            # Enable deletion in registry if it's in the same Kubernetes cluster
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            if registry_in_cluster:
                self.logger.info("Registry is in-cluster, enabling deletion...")
                if not self.skopeo_client.enable_registry_deletion():
                    self.logger.warning("Failed to enable registry deletion - continuing anyway")
            
            # Delete Docker images directly using skopeo
            # Track which ObjectIDs were successfully deleted so we only clean up their MongoDB records
            self.logger.info(f"Deleting {len(archived_tags)} Docker images from registry...")
            self.logger.info("[DEBUG] Starting Docker image deletion loop...")
            
            deleted_count = 0
            failed_deletions = []
            successfully_deleted_object_ids = set()
            
            for idx, tag_info in enumerate(archived_tags):
                self.logger.info(f"[DEBUG] Processing tag {idx+1}/{len(archived_tags)}")
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
            
            # Clean up MongoDB records directly - ONLY for successfully deleted Docker images
            # Filter to only delete MongoDB records for ObjectIDs whose Docker images were deleted
            version_ids_to_clean = [vid for vid in version_ids if vid in successfully_deleted_object_ids]
            model_ids_to_clean = [mid for mid in model_ids if mid in successfully_deleted_object_ids]
            
            skipped_version_ids = len(version_ids) - len(version_ids_to_clean)
            skipped_model_ids = len(model_ids) - len(model_ids_to_clean)
            
            if skipped_version_ids > 0 or skipped_model_ids > 0:
                self.logger.info(f"Skipping MongoDB cleanup for {skipped_version_ids + skipped_model_ids} ObjectIDs due to Docker deletion failures")
            
            mongo_client = get_mongo_client()
            try:
                db = mongo_client[config_manager.get_mongo_db()]
                
                # Clean up model_versions collection - only for successfully deleted images
                if version_ids_to_clean:
                    self.logger.info(f"Cleaning up {len(version_ids_to_clean)} model_version records from MongoDB...")
                    model_versions_collection = db["model_versions"]
                    
                    for obj_id_str in version_ids_to_clean:
                        try:
                            from bson import ObjectId
                            obj_id = ObjectId(obj_id_str)
                            result = model_versions_collection.delete_one({"_id": obj_id})
                            if result.deleted_count > 0:
                                self.logger.info(f"  ✓ Deleted model_version: {obj_id_str}")
                                deletion_results['mongo_records_cleaned'] += 1
                            else:
                                self.logger.warning(f"  ✗ Model_version not found: {obj_id_str}")
                        except Exception as e:
                            self.logger.error(f"  ✗ Error deleting model_version {obj_id_str}: {e}")
                
                # Clean up models collection - only for successfully deleted images
                if model_ids_to_clean:
                    self.logger.info(f"Cleaning up {len(model_ids_to_clean)} model records from MongoDB...")
                    models_collection = db["models"]
                    
                    for obj_id_str in model_ids_to_clean:
                        try:
                            from bson import ObjectId
                            obj_id = ObjectId(obj_id_str)
                            result = models_collection.delete_one({"_id": obj_id})
                            if result.deleted_count > 0:
                                self.logger.info(f"  ✓ Deleted model: {obj_id_str}")
                                deletion_results['mongo_records_cleaned'] += 1
                            else:
                                self.logger.warning(f"  ✗ Model not found: {obj_id_str}")
                        except Exception as e:
                            self.logger.error(f"  ✗ Error deleting model {obj_id_str}: {e}")
            finally:
                mongo_client.close()
            
            self.logger.info("Archived tag deletion completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error deleting archived tags: {e}")
            raise
        finally:
            # Clean up temporary file
            try:
                os.remove(temp_ids_file)
            except OSError:
                pass
        
        return deletion_results
    
    def generate_report(self, archived_ids: List[str], archived_tags: List[ArchivedModelTagInfo], freed_space_bytes: int = 0) -> Dict:
        """Generate a comprehensive report of archived tags
        
        Args:
            archived_ids: List of all archived ObjectIDs
            archived_tags: List of archived tag info objects
            freed_space_bytes: Total bytes that would be freed by deletion (accounts for shared layers)
        """
        
        # Separate ObjectIDs by type (we need to re-fetch to distinguish between model and version IDs)
        mongo_client = get_mongo_client()
        model_ids = []
        version_ids = []
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            models_collection = db["models"]
            
            # Check which IDs are model IDs
            from bson import ObjectId
            for obj_id_str in archived_ids:
                try:
                    obj_id = ObjectId(obj_id_str)
                    if models_collection.find_one({"_id": obj_id}):
                        model_ids.append(obj_id_str)
                    else:
                        version_ids.append(obj_id_str)
                except:
                    # If it's not a valid ObjectId, assume it's a version ID
                    version_ids.append(obj_id_str)
        finally:
            mongo_client.close()
        
        # Group by ObjectID and image type
        by_object_id = {}
        by_image_type = {'environment': 0, 'model': 0}
        
        for tag in archived_tags:
            obj_id = tag.object_id
            if obj_id not in by_object_id:
                by_object_id[obj_id] = []
            by_object_id[obj_id].append(tag)
            by_image_type[tag.image_type] += 1
        
        # Create summary statistics
        summary = {
            'total_archived_object_ids': len(archived_ids),
            'archived_model_ids': len(model_ids),
            'archived_version_ids': len(version_ids),
            'total_matching_tags': len(archived_tags),
            'freed_space_bytes': freed_space_bytes,
            'freed_space_mb': round(freed_space_bytes / (1024 * 1024), 2),
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
                'full_image': tag.full_image,
                'context': tag.context
            })
        
        report = {
            'summary': summary,
            'archived_object_ids': archived_ids,
            'archived_model_ids': model_ids,
            'archived_version_ids': version_ids,
            'archived_tags': detailed_tags,
            'grouped_by_object_id': {
                obj_id: [tag.to_dict() if hasattr(tag, 'to_dict') else tag.__dict__ for tag in tags]
                for obj_id, tags in by_object_id.items()
            },
            'metadata': {
                'registry_url': self.registry_url,
                'repository': self.repository,
                'image_types_scanned': self.image_types,
                'analysis_timestamp': datetime.now().isoformat()
            }
        }
        
        return report
    
    def load_archived_tags_from_file(self, file_path: str) -> List[ArchivedModelTagInfo]:
        """Load archived tags from a pre-generated report file"""
        try:
            with open(file_path, 'r') as f:
                report = json.load(f)
            
            archived_tags = []
            for tag_data in report.get('archived_tags', []):
                tag = ArchivedModelTagInfo(
                    object_id=tag_data['object_id'],
                    image_type=tag_data['image_type'],
                    tag=tag_data['tag'],
                    full_image=tag_data['full_image'],
                    size_bytes=tag_data.get('size_bytes', 0),
                    context=tag_data.get('context', {})
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
        description="Find and optionally delete archived model tags in Docker registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find archived model tags (dry-run)
  python delete_archived_model_tags.py --registry-url docker-registry:5000 --repository dominodatalab

  # Override registry settings
  python delete_archived_model_tags.py --registry-url registry.example.com --repository my-repo

  # Custom output file
  python delete_archived_model_tags.py --output archived-model-tags.json

  # Delete archived model tags directly (requires confirmation)
  python delete_archived_model_tags.py --apply

  # Delete archived model tags from pre-generated file
  python delete_archived_model_tags.py --apply --input archived-model-tags.json

  # Force deletion without confirmation
  python delete_archived_model_tags.py --apply --force
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
        help='Output file path (default: reports/archived-model-tags.json)'
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete archived model tags and clean up MongoDB (default: dry-run)'
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
    
    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
    # Get configuration
    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    output_file = args.output or config_manager.get_archived_model_tags_report_path()
    
    try:
        # Determine operation mode
        is_delete_mode = args.apply
        use_input_file = args.input is not None
        
        logger.info("=" * 60)
        if is_delete_mode:
            logger.info("   Deleting archived model tags")
        else:
            logger.info("   Finding archived model tags")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")
        
        if use_input_file:
            logger.info(f"Input file: {args.input}")
        else:
            logger.info(f"Output file: {output_file}")
        
        # Create finder
        finder = ArchivedModelTagsFinder(registry_url, repository)
        
        # Handle different operation modes
        if use_input_file:
            # Mode 1: Delete from pre-generated file
            logger.info(f"Loading archived tags from {args.input}...")
            archived_tags = finder.load_archived_tags_from_file(args.input)
            archived_ids = []  # Not relevant for deletion mode
            
            if not archived_tags:
                logger.warning(f"No archived tags found in {args.input}")
                sys.exit(0)
                
        else:
            # Mode 2: Find archived tags (and optionally delete them)
            logger.info("Fetching archived ObjectIDs from MongoDB...")
            archived_ids = finder.fetch_archived_object_ids()
            
            if not archived_ids:
                logger.info("No archived model ObjectIDs found")
                # Still create an empty report
                empty_report = {
                    'summary': {
                        'total_archived_object_ids': 0,
                        'archived_model_ids': 0,
                        'archived_version_ids': 0,
                        'total_matching_tags': 0,
                        'freed_space_bytes': 0,
                        'freed_space_mb': 0,
                        'freed_space_gb': 0,
                        'tags_by_image_type': {'environment': 0, 'model': 0},
                        'object_ids_with_tags': 0,
                        'object_ids_without_tags': 0
                    },
                    'archived_object_ids': [],
                    'archived_model_ids': [],
                    'archived_version_ids': [],
                    'archived_tags': [],
                    'grouped_by_object_id': {},
                    'metadata': {
                        'registry_url': registry_url,
                        'repository': repository,
                        'image_types_scanned': ['environment', 'model'],
                        'analysis_timestamp': datetime.now().isoformat()
                    }
                }
                save_json(output_file, empty_report)
                logger.info(f"Empty report written to {output_file}")
                sys.exit(0)
            
            logger.info("Finding matching Docker tags...")
            archived_tags = finder.find_matching_tags(archived_ids)
            
            if not archived_tags:
                logger.info("No matching Docker tags found for archived ObjectIDs")
                # Still create a report with the ObjectIDs but no tags
                report = finder.generate_report(archived_ids, [], freed_space_bytes=0)
                save_json(output_file, report)
                logger.info(f"Report written to {output_file}")
                sys.exit(0)
        
        # Handle deletion mode
        if is_delete_mode:
            if not archived_tags:
                logger.info("No archived tags to delete")
                sys.exit(0)
            
            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to delete {len(archived_tags)} archived model tags!")
                logger.warning("This will delete Docker images and clean up MongoDB records.")
                logger.warning("This action cannot be undone.")
                
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)
            
            logger.info(f"\n🗑️  Deleting {len(archived_tags)} archived model tags...")
            sys.stdout.flush()
            sys.stderr.flush()
            deletion_results = finder.delete_archived_tags(archived_tags)
            
            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_deleted = deletion_results.get('docker_images_deleted', 0)
            total_cleaned = deletion_results.get('mongo_records_cleaned', 0)
            logger.info(f"Total Docker images deleted: {total_deleted}")
            logger.info(f"Total MongoDB records cleaned: {total_cleaned}")
            
            logger.info("\n✅ Archived model tags deletion completed successfully!")
            
        else:
            # Find mode - calculate freed space and generate report
            logger.info("Calculating freed space for archived tags...")
            freed_space_bytes = finder.calculate_freed_space(archived_tags)
            
            logger.info("Generating report...")
            report = finder.generate_report(archived_ids, archived_tags, freed_space_bytes)
            
            # Save report
            save_json(output_file, report)
            
            # Print summary
            summary = report['summary']
            logger.info("\n" + "=" * 60)
            logger.info("   ARCHIVED MODEL TAGS ANALYSIS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total archived ObjectIDs: {summary['total_archived_object_ids']}")
            logger.info(f"  - Archived model IDs: {summary['archived_model_ids']}")
            logger.info(f"  - Archived version IDs: {summary['archived_version_ids']}")
            logger.info(f"Total matching tags: {summary['total_matching_tags']}")
            logger.info(f"Space that would be freed: {summary['freed_space_gb']:.2f} GB ({summary['freed_space_mb']:.2f} MB)")
            logger.info(f"Tags by image type:")
            for img_type, count in summary['tags_by_image_type'].items():
                logger.info(f"  {img_type}: {count} tags")
            logger.info(f"ObjectIDs with tags: {summary['object_ids_with_tags']}")
            logger.info(f"ObjectIDs without tags: {summary['object_ids_without_tags']}")
            
            logger.info(f"\nDetailed report saved to: {output_file}")
            
            if archived_tags:
                logger.warning(f"\n⚠️  Found {len(archived_tags)} archived model tags that may need cleanup!")
                logger.info("Review the detailed report to identify which Docker images are associated with archived models.")
                logger.info("Use --apply flag to delete these images and clean up MongoDB records.")
                logger.info("Or use --apply --input <file> to delete from a saved report.")
            else:
                logger.info("\n✅ No archived model tags found!")
            
            logger.info("\n✅ Archived model tags analysis completed successfully!")
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Operation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
