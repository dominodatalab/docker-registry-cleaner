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

  # Optional: Filter by ObjectIDs from file
  python delete_image.py --apply --file object_ids.txt

  # Optional: Custom report paths
  python delete_image.py --workload-report reports/workload.json --image-analysis reports/analysis.json

  # Optional: Skip MongoDB cleanup after deletion
  python delete_image.py --apply --skip-cleanup-mongo

  # MongoDB cleanup (automatically cleans up environment_revisions and/or model_versions collections)
  python delete_image.py --apply
"""

import argparse
import json
import os
import subprocess
import sys

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from backup_restore import process_backup
from config_manager import config_manager, SkopeoClient, ConfigManager
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
    
    def __init__(self, registry_url: str = None, repository: str = None, namespace: str = None):
        self.registry_url = registry_url or config_manager.get_registry_url()
        self.repository = repository or config_manager.get_repository()
        self.namespace = namespace or config_manager.get_platform_namespace()
        self.logger = get_logger(__name__)
        
        # Initialize Skopeo client for local execution (same as other delete scripts)
        # SkopeoClient now handles registry deletion enable/disable via enable_registry_deletion()
        self.skopeo_client = SkopeoClient(config_manager, use_pod=config_manager.get_skopeo_use_pod())
    
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

    def analyze_image_usage(self, workload_report: Dict, image_analysis: Dict, object_ids: Optional[List[str]] = None) -> WorkloadAnalysis:
        """Analyze which images are used vs unused based on workload and image analysis"""
        used_images = set()
        unused_images = set()
        total_size_saved = 0
        image_usage_stats = {}
        
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
        freed_bytes = 0
        for layer_id, layer_info in image_analysis.items():
            layer_tags = layer_info.get('tags', [])
            
            # Filter tags by ObjectIDs if provided
            if object_ids:
                original_tag_count = len(layer_tags)
                filtered_tags = []
                for tag in layer_tags:
                    for obj_id in object_ids:
                        if tag.startswith(obj_id):
                            filtered_tags.append(tag)
                            break
                layer_tags = filtered_tags
                if len(filtered_tags) < original_tag_count:
                    self.logger.info(f"Filtered layer {layer_id}: {len(filtered_tags)}/{original_tag_count} tags match ObjectIDs")
            
            # Track all observed tags
            for tag in layer_tags:
                all_tags.add(tag)
            
            # Freed space: sum sizes of layers that have no used tags
            if layer_tags and not any(tag in used_images for tag in layer_tags):
                freed_bytes += layer_info.get('size', 0)
        
        # Deletion candidates: all tags not referenced by workloads
        unused_images = all_tags - used_images
        total_size_saved = freed_bytes
        
        # Minimal per-tag stats (do not double-count layer sizes here)
        for tag in all_tags:
            image_usage_stats[tag] = {
                'size': 0,
                'layer_id': '',
                'status': 'used' if tag in used_images else 'unused',
                'pods_using': workload_map.get(tag, {}).get('pods', []) if tag in used_images and isinstance(workload_map, dict) else []
            }
        
        return WorkloadAnalysis(
            used_images=used_images,
            unused_images=unused_images,
            total_size_saved=total_size_saved,
            image_usage_stats=image_usage_stats
        )

    def generate_deletion_report(self, analysis: WorkloadAnalysis, output_file: str = "deletion-analysis.json") -> None:
        """Generate a detailed deletion analysis report"""
        report = {
            "summary": {
                "total_images_analyzed": len(analysis.used_images) + len(analysis.unused_images),
                "used_images": len(analysis.used_images),
                "unused_images": len(analysis.unused_images),
                "total_size_saved": analysis.total_size_saved,
                "total_size_saved_gb": analysis.total_size_saved / (1024**3)
            },
            "unused_images": []
        }
        
        # Add details for each unused image
        for image_tag in analysis.unused_images:
            stats = analysis.image_usage_stats.get(image_tag, {})
            report["unused_images"].append({
                "tag": image_tag,
                "size": stats.get('size', 0),
                "size_gb": stats.get('size', 0) / (1024**3),
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
            # Unused images are in format: repository/image:tag
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                parts = image_tag.split(':')
                if len(parts) == 2:
                    tags_to_backup.append(parts[1])
            
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
            # Extract repository and tag from image tag
            # Assuming format: repository/image:tag
            parts = image_tag.split(':')
            if len(parts) != 2:
                self.logger.warning(f"Invalid image tag format: {image_tag}")
                failed_deletions += 1
                continue
            
            repository_tag = parts[0]
            tag = parts[1]
            
            # Extract repository name (remove registry URL if present)
            if '/' in repository_tag:
                repository = repository_tag.split('/', 1)[1]  # Remove registry URL
            else:
                repository = repository_tag
            
            stats = analysis.image_usage_stats.get(image_tag, {})
            size = stats.get('size', 0)
            
            if dry_run:
                print(f"  Would delete: {image_tag} ({size / (1024**3):.2f} GB)")
                total_size_deleted += size
            else:
                print(f"  Deleting: {image_tag} ({size / (1024**3):.2f} GB)")
                
                # Use standardized Skopeo client for deletion
                if self.skopeo_client.delete_image(repository, tag):
                    print(f"    ✅ Deleted successfully")
                    successful_deletions += 1
                    total_size_deleted += size
                    deleted_tags.append(image_tag)
                else:
                    print(f"    ❌ Failed to delete")
                    failed_deletions += 1
        
        print(f"\n📊 Deletion Summary:")
        print(f"   {'Would delete' if dry_run else 'Successfully deleted'}: {successful_deletions} images")
        if not dry_run:
            print(f"   Failed deletions: {failed_deletions} images")
        print(f"   {'Would save' if dry_run else 'Saved'}: {total_size_deleted / (1024**3):.2f} GB")
        
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
    parser.add_argument("--file", help="File containing ObjectIDs (one per line) to filter images")
    parser.add_argument("--skip-cleanup-mongo", action="store_true", help="Skip MongoDB cleanup after deleting images (automatically cleans environment_revisions and/or model_versions based on image type)")
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
        any_ids = set(object_ids_map.get('any', [])) if object_ids_map else set()
        env_ids = list(any_ids.union(object_ids_map.get('environment', []))) if object_ids_map else []
        model_ids = list(any_ids.union(object_ids_map.get('model', []))) if object_ids_map else []
        if not (env_ids or model_ids):
            print(f"Error: No valid ObjectIDs found in file '{args.file}'")
            sys.exit(1)
        print(f"Filtering images by ObjectIDs from file '{args.file}': environment={len(env_ids)}, model={len(model_ids)}")
    
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
        deleter = IntelligentImageDeleter()
        
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
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
            
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
            
            # Disable deletion in registry (if running in Kubernetes)
            if not dry_run:
                deleter.disable_deletion_of_docker_images()
            
            # Clean up Mongo references for deleted tags
            if deleted_tags and not dry_run and not args.skip_cleanup_mongo:
                deleter.cleanup_mongo_references(deleted_tags)
            
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
                merged.update(object_ids_map.get('any', []))
                merged.update(object_ids_map.get('environment', []))
                merged.update(object_ids_map.get('model', []))
                merged_ids = sorted(merged)
                print(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
            analysis = deleter.analyze_image_usage(workload_report, image_analysis, merged_ids)

            # Prepare tags to backup
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                parts = image_tag.split(':')
                if len(parts) == 2:
                    tags_to_backup.append(parts[1])
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
                merged.update(object_ids_map.get('any', []))
                merged.update(object_ids_map.get('environment', []))
                merged.update(object_ids_map.get('model', []))
                merged_ids = sorted(merged)
                print(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
            analysis = deleter.analyze_image_usage(workload_report, image_analysis, merged_ids)
            
            # Generate deletion report
            deleter.generate_deletion_report(analysis, args.output_report)
            
            # Enable deletion in registry (if running in Kubernetes)
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
            
            # Delete unused images using SkopeoClient (same as other delete scripts)
            deleted_tags = deleter.delete_unused_images(
                analysis, 
                password, 
                dry_run=dry_run,
                backup=args.backup,
                s3_bucket=s3_bucket,
                region=s3_region
            )
            
            # Disable deletion in registry (if running in Kubernetes)
            if not dry_run:
                deleter.disable_deletion_of_docker_images()
            
            # Clean up Mongo references for deleted tags (enabled by default, can be skipped with --skip-cleanup-mongo)
            if deleted_tags and not dry_run and not args.skip_cleanup_mongo:
                deleter.cleanup_mongo_references(deleted_tags)
            
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