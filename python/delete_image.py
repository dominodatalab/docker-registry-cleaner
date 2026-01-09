#!/usr/bin/env python3
"""
Intelligent Docker Image Deletion Tool

This script analyzes workload usage patterns and safely deletes unused Docker images
from the registry while preserving all actively used ones.

Configuration:
  Registry password is sourced from (in priority order):
  1. --password command-line flag (highest priority)
  2. REGISTRY_PASSWORD environment variable
  3. config.yaml registry.password field (lowest priority)

Usage examples:
  # Delete a specific image (dry-run)
  python delete_image.py dominodatalab/environment:abc-123

  # Delete a specific image (apply)
  python delete_image.py dominodatalab/environment:abc-123 --apply

  # Analyze unused images (dry-run)
  python delete_image.py

  # Delete unused images directly
  python delete_image.py --apply

  # Force deletion without confirmation
  python delete_image.py --apply --force

  # Provide Docker Registry password manually (overrides env and config)
  python delete_image.py --apply --password mypassword

  # Back up images to S3 before deletion
  python delete_image.py --apply --backup

  # Optional: Back up images to S3 with custom bucket and region
  python delete_image.py --apply --backup --s3-bucket my-backup-bucket --region us-east-1

  # Optional: Filter by ObjectIDs from file (requires prefixes: environment:, environmentRevision:, model:, or modelVersion:)
  python delete_image.py --apply --file object_ids.txt

  # Optional: Custom report paths
  python delete_image.py --workload-report reports/workload.json --image-analysis reports/analysis.json

# Optional: Clean up MongoDB references after deletion (disabled by default)
python delete_image.py --apply --mongo-cleanup
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from backup_restore import process_backup
from config_manager import config_manager, SkopeoClient, ConfigManager
from image_data_analysis import ImageAnalyzer
from logging_utils import setup_logging, get_logger
from object_id_utils import read_typed_object_ids_from_file
from report_utils import save_json


@dataclass
class WorkloadAnalysis:
    """Data class for workload analysis results"""
    used_images: Set[str]
    unused_images: Set[str]
    total_size_saved: int
    image_usage_stats: Dict[str, Dict]


@dataclass
class LayerAnalysis:
    """Data class for layer analysis results"""
    layer_id: str
    size: int
    tags: List[str]
    environments: List[str]
    is_used: bool


class IntelligentImageDeleter:
    """Main class for intelligent Docker image deletion"""
    
    def __init__(self, registry_url: str = None, repository: str = None, namespace: str = None,
                 enable_docker_deletion: bool = False, registry_statefulset: str = None):
        self.registry_url = registry_url or config_manager.get_registry_url()
        self.repository = repository or config_manager.get_repository()
        self.namespace = namespace or config_manager.get_platform_namespace()
        self.logger = get_logger(__name__)
        
        # Initialize Skopeo client for local execution (same as other delete scripts)
        # SkopeoClient now handles registry deletion enable/disable via enable_registry_deletion()
        self.skopeo_client = SkopeoClient(
            config_manager, 
            use_pod=config_manager.get_skopeo_use_pod(),
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset=registry_statefulset
        )
    
    def load_workload_report(self, report_path: str = "workload-report.json") -> Dict:
        """Load workload analysis report from JSON file"""
        try:
            with open(report_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Workload report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in workload report: {e}")
            return {}
    
    def load_image_analysis_report(self, report_path: str = "final-report.json") -> Dict:
        """Load image analysis report from JSON file"""
        try:
            with open(report_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Image analysis report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in image analysis report: {e}")
            return {}

    def analyze_image_usage(self, workload_report: Dict, image_analysis: Dict, object_ids: Optional[List[str]] = None, object_ids_map: Optional[Dict[str, List[str]]] = None) -> WorkloadAnalysis:
        """Analyze which images are used vs unused based on workload and image analysis
        
        Args:
            workload_report: Workload analysis report
            image_analysis: Image analysis report
            object_ids: List of ObjectIDs to filter by (merged from all types)
            object_ids_map: Dict mapping image types to ObjectID lists (e.g., {'environment': [...], 'model': [...]})
        """
        used_images = set()
        unused_images = set()
        total_size_saved = 0
        image_usage_stats = {}
        
        # Build mapping from ObjectID to image type if object_ids_map is provided
        oid_to_type = {}
        if object_ids_map:
            for img_type, oids in object_ids_map.items():
                # Map both 'environment' and 'environment_revision' to 'environment'
                # Map both 'model' and 'model_version' to 'model'
                repo_type = 'environment' if img_type in ('environment', 'environment_revision') else 'model'
                for oid in oids:
                    oid_to_type[oid] = repo_type
        
        # Get used images from workload report
        # Support both formats:
        # - { "image_tags": { tag: {...} } }
        # - { tag: {...} }
        workload_map = workload_report.get('image_tags', workload_report)
        for tag, tag_info in workload_map.items():
            if tag_info.get('count', 0) > 0:
                used_images.add(tag)
        
        # Filter by ObjectIDs if provided
        if object_ids:
            original_used_count = len(used_images)
            filtered_used_images = set()
            for image in used_images:
                for obj_id in object_ids:
                    if image.startswith(obj_id):
                        filtered_used_images.add(image)
                        break
            used_images = filtered_used_images
            self.logger.info(f"Filtered used images: {len(used_images)}/{original_used_count} match ObjectIDs")
        
        # Analyze image layers from image analysis
        # Support format produced by image_data_analysis (mapping of layer_id -> { size, tags, environments })
        all_tags = set()
        for layer_id, layer_info in image_analysis.items():
            layer_tags = layer_info.get('tags', [])
            
            # Filter tags by ObjectIDs if provided and determine image type
            if object_ids:
                original_tag_count = len(layer_tags)
                filtered_tags = []
                for tag in layer_tags:
                    matched = False
                    matched_type = None
                    for obj_id in object_ids:
                        if tag.startswith(obj_id):
                            filtered_tags.append(tag)
                            matched = True
                            # Determine image type from object_ids_map if available
                            if object_ids_map:
                                matched_type = oid_to_type.get(obj_id)
                            break
                    # Store tag with type prefix if we know the type
                    if matched and matched_type:
                        all_tags.add(f"{matched_type}:{tag}")
                    elif matched:
                        all_tags.add(tag)
                layer_tags = filtered_tags
                if len(filtered_tags) < original_tag_count:
                    self.logger.info(f"Filtered layer {layer_id}: {len(filtered_tags)}/{original_tag_count} tags match ObjectIDs")
            else:
                # No filtering - just add all tags (we don't know the type)
                for tag in layer_tags:
                    all_tags.add(tag)
            
            # NOTE: We don't calculate freed_bytes here anymore - it's calculated separately
            # using ImageAnalyzer to properly account for shared layers
        
        # Deletion candidates: all tags not referenced by workloads
        # For tags with type prefix, compare just the tag part
        unused_images = set()
        for full_tag in all_tags:
            # Extract just the tag name for comparison with used_images
            if ':' in full_tag:
                tag_name = full_tag.split(':', 1)[1]
            else:
                tag_name = full_tag
            
            if tag_name not in used_images:
                unused_images.add(full_tag)
        
        # Calculate freed space correctly using ImageAnalyzer (accounts for shared layers)
        # This also returns individual tag sizes
        total_size_saved, individual_tag_sizes = self._calculate_freed_space_correctly(unused_images, object_ids_map)
        
        # Build per-tag stats with accurate sizes
        for full_tag in all_tags:
            # Extract tag name for stats lookup
            tag_name = full_tag.split(':', 1)[1] if ':' in full_tag else full_tag
            # Get individual size from calculation (0 if not found)
            tag_size = individual_tag_sizes.get(full_tag, 0)
            image_usage_stats[full_tag] = {
                'size': tag_size,
                'layer_id': '',
                'status': 'used' if tag_name in used_images else 'unused',
                'pods_using': workload_map.get(tag_name, {}).get('pods', []) if tag_name in used_images and isinstance(workload_map, dict) else []
            }
        
        return WorkloadAnalysis(
            used_images=used_images,
            unused_images=unused_images,
            total_size_saved=total_size_saved,
            image_usage_stats=image_usage_stats
        )

    def _calculate_freed_space_correctly(self, unused_images: Set[str], object_ids_map: Optional[Dict[str, List[str]]] = None) -> Tuple[int, Dict[str, int]]:
        """Calculate freed space correctly using ImageAnalyzer, accounting for shared layers.
        
        This method analyzes ALL images (not just unused ones) to get accurate reference counts,
        then calculates what would be freed by deleting the unused images. Only layers that would
        have zero references after deletion are counted.
        
        Args:
            unused_images: Set of unused image tags (format: "type:tag" or just "tag")
            object_ids_map: Optional map of image types to ObjectID lists (for logging)
        
        Returns:
            Tuple of (total_bytes_freed, dict mapping image_tag to individual_bytes_freed)
            Returns (0, {}) if calculation fails
        """
        if not unused_images:
            return 0, {}
        
        try:
            max_workers = config_manager.get_max_workers()
            self.logger.info(f"Calculating accurate freed space using ImageAnalyzer (analyzing ALL images)...")
            self.logger.info("This ensures shared layers between unused and used images are properly accounted for.")
            
            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)
            
            # CRITICAL: Analyze ALL images (not just unused ones) to get accurate reference counts
            # This ensures that shared layers between unused and used images are properly accounted for
            image_types = ['environment', 'model']
            for image_type in image_types:
                self.logger.info(f"Analyzing ALL {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=None, max_workers=max_workers)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")
            
            # Build list of image_ids from unused images
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = []
            skipped_tags = []
            tag_mapping = {}  # Map image_id back to original full_tag for size reporting
            for full_tag in unused_images:
                if ':' in full_tag:
                    # Format: "type:tag"
                    image_ids.append(full_tag)
                    tag_mapping[full_tag] = full_tag
                else:
                    # Legacy format: just tag name, we don't know the type
                    # Skip these or try both - for now, skip with a warning
                    skipped_tags.append(full_tag)
            
            if skipped_tags:
                self.logger.warning(f"Skipping {len(skipped_tags)} tags without type prefix for freed space calculation")
            
            if not image_ids:
                self.logger.warning("No tags with type information found, cannot calculate freed space accurately")
                return 0, {}
            
            # Calculate individual tag sizes first (layers unique to each image)
            self.logger.info(f"Calculating individual image sizes for {len(image_ids)} images...")
            individual_sizes = {}
            for image_id in image_ids:
                # Calculate what would be freed if only this image was deleted
                size_bytes = analyzer.freed_space_if_deleted([image_id])
                individual_sizes[tag_mapping[image_id]] = size_bytes
            
            # Calculate total freed space using ImageAnalyzer's method
            # This properly accounts for shared layers - only counts layers that would have
            # zero references after deletion (i.e., not used by any remaining images)
            total_freed = analyzer.freed_space_if_deleted(image_ids)
            
            self.logger.info(f"Total space that would be freed: {total_freed / (1024**3):.2f} GB")
            
            return total_freed, individual_sizes
            
        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return 0, {}

    def generate_deletion_report(self, analysis: WorkloadAnalysis, output_file: str = "deletion-analysis.json") -> None:
        """Generate a detailed deletion analysis report"""
        report = {
            "summary": {
                "total_images_analyzed": len(analysis.used_images) + len(analysis.unused_images),
                "used_images": len(analysis.used_images),
                "unused_images": len(analysis.unused_images),
                "total_size_saved": analysis.total_size_saved,
                "total_size_saved_gb": round(analysis.total_size_saved / (1024**3), 2)
            },
            "unused_images": []
        }
        
        # Add details for each unused image
        for image_tag in analysis.unused_images:
            stats = analysis.image_usage_stats.get(image_tag, {})
            size_bytes = stats.get('size', 0)
            report["unused_images"].append({
                "tag": image_tag,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024**3), 2),
                "layer_id": stats.get('layer_id', ''),
                "status": stats.get('status', 'unused'),
                "pods_using": stats.get('pods_using', [])
            })
        
        try:
            save_json(output_file, report)
            self.logger.info(f"Deletion analysis report saved to: {output_file}")
        except Exception as e:
            self.logger.error(f"Failed to save deletion report: {e}")
        
        # Print summary
        print(f"\n📊 Deletion Analysis Summary:")
        print(f"   Total images analyzed: {report['summary']['total_images_analyzed']}")
        print(f"   Images in use: {report['summary']['used_images']}")
        print(f"   Images unused: {report['summary']['unused_images']}")
        print(f"   Potential space saved: {report['summary']['total_size_saved_gb']:.2f} GB")

    def save_deletion_results(self, analysis: WorkloadAnalysis, deleted_tags: List[str], 
                              successful_deletions: int, failed_deletions: int, 
                              total_size_deleted: int, dry_run: bool, output_file: str = None) -> str:
        """Save deletion results to a JSON file.
        
        Args:
            analysis: WorkloadAnalysis object with image usage information
            deleted_tags: List of successfully deleted image tags
            successful_deletions: Number of successful deletions
            failed_deletions: Number of failed deletions
            total_size_deleted: Total size of deleted images (bytes)
            dry_run: Whether this was a dry run
            output_file: Optional path for output file (defaults to config-managed path)
        
        Returns:
            Path to the saved JSON file
        """
        if output_file is None:
            if dry_run:
                output_file = config_manager.get_output_dir() + "/deletion-dry-run-results.json"
            else:
                output_file = config_manager.get_output_dir() + "/deletion-results.json"
        
        # Build results report
        results = {
            "dry_run": dry_run,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "summary": {
                "total_images_analyzed": len(analysis.used_images) + len(analysis.unused_images),
                "used_images": len(analysis.used_images),
                "unused_images": len(analysis.unused_images),
                "successful_deletions": successful_deletions,
                "failed_deletions": failed_deletions,
                "total_size_deleted_bytes": total_size_deleted,
                "total_size_deleted_gb": round(total_size_deleted / (1024**3), 2),
                "total_size_saved_bytes": analysis.total_size_saved,
                "total_size_saved_gb": round(analysis.total_size_saved / (1024**3), 2)
            },
            "deleted_images": [],
            "failed_deletions": []
        }
        
        # Add successfully deleted images
        # deleted_tags are in format "repository/type:tag", need to match with image_tag format "type:tag"
        deleted_tags_normalized = set()
        for dt in deleted_tags:
            # Extract the "type:tag" part from "repository/type:tag"
            if '/' in dt and ':' in dt:
                parts = dt.split('/', 1)
                if ':' in parts[1]:
                    deleted_tags_normalized.add(parts[1])  # Add "type:tag"
            elif ':' in dt:
                deleted_tags_normalized.add(dt)  # Already in "type:tag" format
            else:
                deleted_tags_normalized.add(dt)  # Legacy format
        
        for image_tag in sorted(analysis.unused_images):
            stats = analysis.image_usage_stats.get(image_tag, {})
            size_bytes = stats.get('size', 0)
            
            # Check if this image was successfully deleted
            # For dry_run, all unused images are considered "would be deleted"
            is_deleted = (image_tag in deleted_tags_normalized) if not dry_run else True
            
            image_result = {
                "tag": image_tag,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024**3), 2),
                "status": "deleted" if is_deleted else "failed"
            }
            
            if image_result["status"] == "deleted":
                results["deleted_images"].append(image_result)
            else:
                results["failed_deletions"].append(image_result)
        
        try:
            save_json(output_file, results)
            self.logger.info(f"Deletion results saved to: {output_file}")
            return output_file
        except Exception as e:
            self.logger.error(f"Failed to save deletion results: {e}")
            return output_file

    def enable_deletion_of_docker_images(self):
        """Enable deletion of Docker images in the registry"""
        print("Enabling deletion of Docker images in registry...")
        success = self.skopeo_client.enable_registry_deletion(namespace=self.namespace)
        if success:
            print("✓ Deletion enabled in registry")
        else:
            self.logger.warning("Failed to enable registry deletion - continuing anyway")

    def disable_deletion_of_docker_images(self):
        """Disable deletion of Docker images in the registry"""
        print("Disabling deletion of Docker images in registry...")
        success = self.skopeo_client.disable_registry_deletion(namespace=self.namespace)
        if success:
            print("✓ Deletion disabled in registry")
        else:
            self.logger.warning("Failed to disable registry deletion - continuing anyway")

    def delete_unused_images(self, analysis: WorkloadAnalysis, password: str, dry_run: bool = True, backup: bool = False, s3_bucket: str = None, region: str = 'us-west-2') -> List[str]:
        """Delete unused images based on analysis. Returns list of successfully deleted image tags.
        
        Args:
            analysis: WorkloadAnalysis object containing used/unused image information
            password: Registry password (optional)
            dry_run: If True, only simulate deletion without actually deleting
            backup: Whether to backup images to S3 before deletion
            s3_bucket: S3 bucket name for backups
            region: AWS region for S3 and ECR operations
        """
        if not analysis.unused_images:
            print("No unused images found to delete.")
            return []
        
        # Backup images to S3 if requested (only in non-dry-run mode)
        if not dry_run and backup and s3_bucket:
            print(f"\n📦 Backing up {len(analysis.unused_images)} images to S3 bucket: {s3_bucket}")
            
            # Extract tags from unused images
            # Unused images are in format: type:tag (e.g., "environment:abc123...") or just "tag" (legacy)
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                # Handle format: "type:tag" or just "tag"
                if ':' in image_tag:
                    tag = image_tag.split(':', 1)[1]
                else:
                    tag = image_tag
                tags_to_backup.append(tag)
            
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
                print(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as backup_err:
                print(f"❌ Backup failed: {backup_err}")
                print("Aborting deletion to prevent data loss")
                raise
        
        print(f"\n🗑️  {'DRY RUN: ' if dry_run else ''}Deleting {len(analysis.unused_images)} unused images...")
        
        total_size_deleted = 0
        successful_deletions = 0
        failed_deletions = 0
        deleted_tags = []
        
        for image_tag in analysis.unused_images:
            # Extract repository type and tag from image tag
            # Format can be:
            # - "type:tag" (e.g., "environment:abc123def456...")
            # - "tag" (legacy format, try both repositories)
            parts = image_tag.split(':', 1)
            if len(parts) == 2:
                # Format: "type:tag"
                repo_type, tag = parts
                repository = f"{self.repository}/{repo_type}"
            elif len(parts) == 1:
                # Legacy format: just tag name, try both environment and model
                tag = parts[0]
                # Try environment first, then model if it fails
                # We'll try both in the deletion loop
                repository = None
            else:
                self.logger.warning(f"Invalid image tag format: {image_tag}")
                failed_deletions += 1
                continue
            
            stats = analysis.image_usage_stats.get(image_tag, {})
            size = stats.get('size', 0)
            
            if dry_run:
                if repository:
                    print(f"  Would delete: {repository}:{tag} ({size / (1024**3):.2f} GB)")
                else:
                    print(f"  Would delete: environment:{tag} or model:{tag} ({size / (1024**3):.2f} GB)")
                successful_deletions += 1
                total_size_deleted += size
            else:
                if repository:
                    print(f"  Deleting: {repository}:{tag} ({size / (1024**3):.2f} GB)")
                    # Use standardized Skopeo client for deletion
                    if self.skopeo_client.delete_image(repository, tag):
                        print(f"    ✅ Deleted successfully")
                        successful_deletions += 1
                        total_size_deleted += size
                        deleted_tags.append(f"{repository}:{tag}")
                    else:
                        print(f"    ❌ Failed to delete")
                        failed_deletions += 1
                else:
                    # Try both environment and model repositories
                    deleted = False
                    for repo_type in ['environment', 'model']:
                        try_repo = f"{self.repository}/{repo_type}"
                        print(f"  Trying to delete: {try_repo}:{tag}")
                        if self.skopeo_client.delete_image(try_repo, tag):
                            print(f"    ✅ Deleted successfully from {try_repo}")
                            successful_deletions += 1
                            total_size_deleted += size
                            deleted_tags.append(f"{try_repo}:{tag}")
                            deleted = True
                            break
                    if not deleted:
                        print(f"    ❌ Failed to delete from both environment and model repositories")
                        failed_deletions += 1
        
        print(f"\n📊 Deletion Summary:")
        print(f"   {'Would delete' if dry_run else 'Successfully deleted'}: {successful_deletions} images")
        if not dry_run:
            print(f"   Failed deletions: {failed_deletions} images")
        # Use total_size_saved from analysis for accurate freed space (accounts for shared layers)
        # This is calculated correctly using ImageAnalyzer
        summary_size = analysis.total_size_saved if analysis.total_size_saved > 0 else total_size_deleted
        print(f"   {'Would save' if dry_run else 'Saved'}: {summary_size / (1024**3):.2f} GB")
        
        # Save results to JSON file
        results_file = self.save_deletion_results(
            analysis=analysis,
            deleted_tags=deleted_tags,
            successful_deletions=successful_deletions,
            failed_deletions=failed_deletions,
            total_size_deleted=total_size_deleted,
            dry_run=dry_run
        )
        print(f"   Results saved to: {results_file}")
        
        return deleted_tags

    def cleanup_mongo_references(self, deleted_tags: List[str]) -> None:
        """Clean up Mongo references for deleted image tags by calling mongo_cleanup.py
        
        Automatically determines whether to clean environment_revisions or model_versions
        collections based on the image type in the tag.
        
        Args:
            deleted_tags: List of Docker image tags that were deleted (format: repository/type:tag)
        """
        if not deleted_tags:
            return
        
        print(f"\n🗄️  Cleaning up Mongo references for {len(deleted_tags)} deleted tags...")
        
        # Separate tags by image type
        environment_tags = []
        model_tags = []
        
        for tag in deleted_tags:
            # Extract image type from tag format: repository/type:tag
            # e.g., "dominodatalab/environment:abc123" or "dominodatalab/model:xyz789"
            if '/environment:' in tag:
                environment_tags.append(tag)
            elif '/model:' in tag:
                model_tags.append(tag)
            else:
                self.logger.warning(f"Could not determine image type for tag: {tag}")
        
        script_path = os.path.join(os.path.dirname(__file__), "mongo_cleanup.py")
        
        # Clean up environment_revisions collection
        if environment_tags:
            self._cleanup_collection(environment_tags, "environment_revisions", script_path)
        
        # Clean up model_versions collection
        if model_tags:
            self._cleanup_collection(model_tags, "model_versions", script_path)
        
        if environment_tags or model_tags:
            print("✅ Mongo references cleaned up successfully")
    
    def _cleanup_collection(self, tags: List[str], collection_name: str, script_path: str) -> None:
        """Helper method to clean up a specific MongoDB collection.
        
        Args:
            tags: List of tags to clean up
            collection_name: MongoDB collection name
            script_path: Path to mongo_cleanup.py script
        """
        temp_file = os.path.join(config_manager.get_output_dir(), f"deleted_tags_{collection_name}_temp.txt")
        try:
            with open(temp_file, 'w') as f:
                for tag in tags:
                    f.write(f"{tag}\n")
            
            print(f"  Cleaning {len(tags)} tags from {collection_name}...")
            cmd = [sys.executable, script_path, "delete", "--file", temp_file, "--collection", collection_name]
            
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                if result.stdout:
                    print(f"    {result.stdout}")
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Failed to clean up {collection_name}: {e}")
                if e.stdout:
                    print(f"    stdout: {e.stdout}")
                if e.stderr:
                    print(f"    stderr: {e.stderr}")
                print(f"    ⚠️  Cleanup of {collection_name} failed - you may need to clean up references manually")
        
        finally:
            # Clean up temporary file
            try:
                os.remove(temp_file)
            except OSError:
                pass  # Ignore cleanup errors


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Intelligent Docker image deletion with workload analysis")
    parser.add_argument("image", nargs="?", help="Specific image to delete (format: repository/type:tag, e.g., dominodatalab/environment:abc-123)")
    parser.add_argument("--password", help="Password for registry access (optional; defaults to REGISTRY_PASSWORD env var or config.yaml)")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes and delete images (default is dry-run)")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")
    parser.add_argument("--workload-report", default=config_manager.get_workload_report_path(), help="Path to workload analysis report")
    parser.add_argument("--image-analysis", default=config_manager.get_image_analysis_path(), help="Path to image analysis report")
    parser.add_argument("--output-report", default=config_manager.get_deletion_analysis_path(), help="Path for deletion analysis report")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip workload analysis and use traditional environments file")
    parser.add_argument("--file", help="File containing ObjectIDs (one per line) to filter images (supports prefixes: environment:, environmentRevision:, model:, modelVersion:, or bare IDs)")
    parser.add_argument("--mongo-cleanup", action="store_true", help="Also clean up MongoDB records after deleting images (disabled by default)")
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
        '--registry-statefulset',
        default='docker-registry',
        help='Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)'
    )
    return parser.parse_args()


def confirm_deletion():
    """Ask for user confirmation before deleting images"""
    print("\n" + "="*60)
    print("⚠️  WARNING: You are about to DELETE Docker images from the registry!")
    print("="*60)
    print("This action cannot be undone.")
    print("Make sure you have reviewed the analysis output above.")
    print("="*60)
    
    while True:
        response = input("Are you sure you want to proceed with deletion? (yes/no): ").lower().strip()
        if response in ['yes', 'y']:
            return True
        elif response in ['no', 'n']:
            return False
        else:
            print("Please enter 'yes' or 'no'.")


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
    # Get S3 configuration from args or config
    s3_bucket = args.s3_bucket or config_manager.get_s3_bucket()
    s3_region = args.region or config_manager.get_s3_region()
    
    # Validate backup arguments
    if args.backup and not s3_bucket:
        print("❌ Error: --s3-bucket is required when --backup is set")
        print("   You can provide it via --s3-bucket flag, S3_BUCKET env var, or config.yaml")
        sys.exit(1)
    
    # Parse ObjectIDs (typed) from file if provided
    object_ids_map = None
    if args.file:
        object_ids_map = read_typed_object_ids_from_file(args.file)
        env_ids = list(object_ids_map.get('environment', [])) if object_ids_map else []
        env_rev_ids = list(object_ids_map.get('environment_revision', [])) if object_ids_map else []
        model_ids = list(object_ids_map.get('model', [])) if object_ids_map else []
        model_ver_ids = list(object_ids_map.get('model_version', [])) if object_ids_map else []
        if not (env_ids or env_rev_ids or model_ids or model_ver_ids):
            print(f"Error: No valid ObjectIDs found in file '{args.file}' (prefixes required: environment:, environmentRevision:, model:, modelVersion:)")
            sys.exit(1)
        print(f"Filtering images by ObjectIDs from file '{args.file}': environment={len(env_ids)}, environmentRevision={len(env_rev_ids)}, model={len(model_ids)}, modelVersion={len(model_ver_ids)}")
    
    # Get password with priority: CLI arg > env var > config
    password = args.password or os.environ.get('REGISTRY_PASSWORD') or config_manager.get_registry_password()
    
    # Default to dry-run unless --apply is specified
    dry_run = not args.apply
    
    if dry_run:
        print("🔍 DRY RUN MODE (default)")
        print("Images will NOT be deleted. Use --apply to actually delete images.")
    else:
        print("🗑️  DELETE MODE")
        print("Images WILL be deleted!")
        
        # Require confirmation unless --force is used
        if not args.force:
            if not confirm_deletion():
                print("Deletion cancelled by user.")
                sys.exit(0)
        else:
            print("⚠️  Force mode enabled - skipping confirmation prompt")
    
    try:
        # Create deleter
        deleter = IntelligentImageDeleter(
            enable_docker_deletion=args.enable_docker_deletion,
            registry_statefulset=args.registry_statefulset
        )
        
        # Handle direct image deletion if image argument is provided
        if args.image:
            print(f"🎯 Deleting specific image: {args.image}")
            
            # Parse image format: repository/type:tag
            if ':' not in args.image:
                print(f"❌ Error: Invalid image format. Expected format: repository/type:tag (e.g., dominodatalab/environment:abc-123)")
                sys.exit(1)
            
            parts = args.image.split(':')
            repository_tag = parts[0]
            tag = parts[1]
            
            # Extract repository name (remove registry URL if present)
            if '/' in repository_tag:
                repository = repository_tag.split('/', 1)[1]  # Remove registry URL
            else:
                repository = repository_tag
            
            # Enable deletion in registry (if running in Kubernetes)
            registry_enabled = False
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
                registry_enabled = True
            
            try:
                # Delete the image
                deleted_tags = []
                if dry_run:
                    print(f"  Would delete: {args.image}")
                    deleted_tags = [args.image]
                else:
                    print(f"  Deleting: {args.image}")
                    if deleter.skopeo_client.delete_image(repository, tag):
                        print(f"    ✅ Deleted successfully")
                        deleted_tags = [args.image]
                    else:
                        print(f"    ❌ Failed to delete")
                
                # Clean up Mongo references for deleted tags
                if deleted_tags and not dry_run and not args.skip_cleanup_mongo:
                    deleter.cleanup_mongo_references(deleted_tags)
            
            finally:
                # Always disable deletion in registry if it was enabled
                if registry_enabled:
                    deleter.disable_deletion_of_docker_images()
            
            return
        
        # Backup-only mode when --backup is provided without --apply
        if (not args.apply) and args.backup:
            # In backup-only mode, target the set of unused images from analysis
            print("\n📦 BACKUP-ONLY MODE: Images will be backed up to S3 without deletion.")
            if not args.force:
                resp = input("Proceed with backup only (no deletions)? (yes/no): ").strip().lower()
                if resp not in ['yes', 'y']:
                    print("Operation cancelled by user")
                    sys.exit(0)

            # Load reports for analysis
            print("📊 Loading workload and image analysis reports...")
            workload_report = deleter.load_workload_report(args.workload_report)
            image_analysis = deleter.load_image_analysis_report(args.image_analysis)
            if not workload_report or not image_analysis:
                print("❌ Missing analysis reports. Run inspect-workload.py and image-data-analysis.py first.")
                sys.exit(1)
            merged_ids = None
            if object_ids_map:
                merged = set()
                merged.update(object_ids_map.get('environment', []))
                merged.update(object_ids_map.get('environment_revision', []))
                merged.update(object_ids_map.get('model', []))
                merged.update(object_ids_map.get('model_version', []))
                merged_ids = sorted(merged)
                print(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
            analysis = deleter.analyze_image_usage(workload_report, image_analysis, merged_ids, object_ids_map)

            # Prepare tags to backup
            # Unused images are in format: type:tag (e.g., "environment:abc123...") or just "tag" (legacy)
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                # Handle format: "type:tag" or just "tag"
                if ':' in image_tag:
                    tag = image_tag.split(':', 1)[1]
                else:
                    tag = image_tag
                tags_to_backup.append(tag)
            if not tags_to_backup:
                print("No unused images found to back up.")
                sys.exit(0)

            full_repo = f"{deleter.registry_url}/{deleter.repository}"
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
                    failed_tags_file=None
                )
                print(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as e:
                print(f"❌ Backup failed: {e}")
                sys.exit(1)
            print("\n✅ Backup-only operation completed successfully!")
            return

        if not args.skip_analysis:
            # Load analysis reports
            print("📊 Loading workload and image analysis reports...")
            workload_report = deleter.load_workload_report(args.workload_report)
            image_analysis = deleter.load_image_analysis_report(args.image_analysis)
            
            if not workload_report or not image_analysis:
                print("❌ Missing analysis reports. Run inspect-workload.py and image-data-analysis.py first.")
                print("   Or use --skip-analysis to use traditional environments file method.")
                sys.exit(1)
            
            # Analyze image usage
            print("🔍 Analyzing image usage patterns...")
            # For deletion, merge all typed IDs to a single set since we evaluate tags after registry prefix removal
            merged_ids = None
            if object_ids_map:
                merged = set()
                merged.update(object_ids_map.get('environment', []))
                merged.update(object_ids_map.get('environment_revision', []))
                merged.update(object_ids_map.get('model', []))
                merged.update(object_ids_map.get('model_version', []))
                merged_ids = sorted(merged)
                print(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
            analysis = deleter.analyze_image_usage(workload_report, image_analysis, merged_ids, object_ids_map)
            
            # Generate deletion report
            deleter.generate_deletion_report(analysis, args.output_report)
            
            # Enable deletion in registry (if running in Kubernetes)
            registry_enabled = False
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
                registry_enabled = True
            
            try:
                # Delete unused images using SkopeoClient (same as other delete scripts)
                deleted_tags = deleter.delete_unused_images(
                    analysis, 
                    password, 
                    dry_run=dry_run,
                    backup=args.backup,
                    s3_bucket=s3_bucket,
                    region=s3_region
                )
                
                # Clean up Mongo references for deleted tags (opt-in via --mongo-cleanup)
                if deleted_tags and not dry_run and args.mongo_cleanup:
                    deleter.cleanup_mongo_references(deleted_tags)
            
            finally:
                # Always disable deletion in registry if it was enabled
                if registry_enabled:
                    deleter.disable_deletion_of_docker_images()
            
        else:
            # Use traditional environments file method
            print("📋 Using traditional environments file method...")
            # ... existing environments file logic would go here
            print("Traditional method not yet implemented. Use workload analysis instead.")
        
        if dry_run:
            print("\n✅ DRY RUN COMPLETED")
            print("No images were deleted.")
            print("To actually delete images, run with --apply flag:")
            print("  python delete_image.py --apply")
        else:
            print("\n✅ DELETION COMPLETED")
            print("Images have been deleted from the registry.")
        
    except KeyboardInterrupt:
        print("\n⚠️  Deletion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Deletion failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()