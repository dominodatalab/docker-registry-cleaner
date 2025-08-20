#!/usr/bin/env python3
"""
Intelligent Docker Image Deletion Tool

This script analyzes workload usage patterns and safely deletes unused Docker images
from the registry while preserving all actively used ones.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from kubernetes import client, config
from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger
from object_id_utils import read_object_ids_from_file
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
    
    def __init__(self, registry_url: str = None, namespace: str = None):
        self.registry_url = registry_url or config_manager.get_registry_url()
        self.namespace = namespace or config_manager.get_platform_namespace()
        self.logger = get_logger(__name__)
        
        # Initialize Kubernetes clients
        try:
            config.load_kube_config()
            self.apps_v1_client = client.AppsV1Api()
            self.core_v1_client = client.CoreV1Api()
            self.logger.info("Kubernetes clients initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize Kubernetes clients: {e}")
            raise
        
        # Initialize standardized Skopeo client
        self.skopeo_client = SkopeoClient(config_manager, use_pod=True, namespace=self.namespace)
    
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

    def create_skopeo_pod(self):
        """Create the Skopeo pod for Docker registry operations"""
        print("Creating Skopeo pod")
        
        try:
            # Check if pod already exists
            try:
                self.core_v1_client.read_namespaced_pod(
                    name="skopeo",
                    namespace=self.namespace
                )
                print("Pod skopeo already exists")
                return
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise
        
        except Exception as e:
            self.logger.error(f"Error checking existing pod: {e}")
            return
        
        # Create the pod
        try:
            pod_manifest = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "skopeo",
                    "namespace": self.namespace
                },
                "spec": {
                    "containers": [{
                        "name": "skopeo",
                        "image": "quay.io/skopeo/stable:latest",
                        "command": ["sleep", "infinity"]
                    }]
                }
            }
            
            self.core_v1_client.create_namespaced_pod(
                namespace=self.namespace,
                body=pod_manifest
            )
            print(f"Created pod skopeo in namespace {self.namespace}")
            
        except Exception as e:
            self.logger.error(f"Error creating Skopeo pod: {e}")
            return
        
        # Wait for pod to be ready
        print("Waiting for pod skopeo to be ready...")
        self._wait_for_pod_ready(self.namespace, "skopeo")
        print("Skopeo pod is ready")

    def delete_skopeo_pod(self):
        """Delete the Skopeo pod"""
        print("Deleting Skopeo pod")
        
        try:
            self.core_v1_client.delete_namespaced_pod(
                name="skopeo",
                namespace=self.namespace
            )
            print("Deleted pod skopeo")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                print("Pod skopeo not found")
            else:
                self.logger.error(f"Error deleting Skopeo pod: {e}")

    def _wait_for_pod_ready(self, namespace: str, name: str, timeout: int = 300, interval: int = 10):
        """Wait for a pod to be ready"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                pod = self.core_v1_client.read_namespaced_pod(
                    name=name,
                    namespace=namespace
                )
                
                if pod.status.phase == "Running":
                    return
                    
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    pass  # Pod not found yet
                else:
                    self.logger.error(f"Error checking pod status: {e}")
            
            time.sleep(interval)
        
        raise TimeoutError(f"Timeout waiting for pod {name} to be ready")

    def enable_deletion_of_docker_images(self):
        """Enable deletion of Docker images"""
        print("Enabling deletion of Docker images")
        
        try:
            sts_data = self.apps_v1_client.read_namespaced_stateful_set(
                name="docker-registry", 
                namespace=self.namespace
            )
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error reading StatefulSet: {e}")
            return
        
        # Check if the environment variable already exists
        env_exists = False
        for env in sts_data.spec.template.spec.containers[0].env or []:
            if env.name == "REGISTRY_STORAGE_DELETE_ENABLED":
                env.value = "true"
                env_exists = True
                break
        
        # Add the environment variable if it doesn't exist
        if not env_exists:
            new_env = client.V1EnvVar(name="REGISTRY_STORAGE_DELETE_ENABLED", value="true")
            if sts_data.spec.template.spec.containers[0].env is None:
                sts_data.spec.template.spec.containers[0].env = []
            sts_data.spec.template.spec.containers[0].env.append(new_env)
        
        # Update the StatefulSet
        try:
            self.apps_v1_client.patch_namespaced_stateful_set(
                name="docker-registry",
                namespace=self.namespace,
                body=sts_data
            )
            print("Enabled deletion of Docker images")
        except Exception as e:
            self.logger.error(f"Error enabling deletion: {e}")

    def disable_deletion_of_docker_images(self):
        """Disable deletion of Docker images"""
        print("Disabling deletion of Docker images")
        
        try:
            sts_data = self.apps_v1_client.read_namespaced_stateful_set(
                name="docker-registry", 
                namespace=self.namespace
            )
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error reading StatefulSet: {e}")
            return
        
        # Remove the environment variable if it exists
        if sts_data.spec.template.spec.containers[0].env:
            sts_data.spec.template.spec.containers[0].env = [
                env for env in sts_data.spec.template.spec.containers[0].env
                if env.name != "REGISTRY_STORAGE_DELETE_ENABLED"
            ]
        
        # Update the StatefulSet
        try:
            self.apps_v1_client.patch_namespaced_stateful_set(
                name="docker-registry",
                namespace=self.namespace,
                body=sts_data
            )
            print("Disabled deletion of Docker images")
        except Exception as e:
            self.logger.error(f"Error disabling deletion: {e}")

    def delete_unused_images(self, analysis: WorkloadAnalysis, password: str, dry_run: bool = True) -> None:
        """Delete unused images based on analysis"""
        if not analysis.unused_images:
            print("No unused images found to delete.")
            return
        
        print(f"\n🗑️  {'DRY RUN: ' if dry_run else ''}Deleting {len(analysis.unused_images)} unused images...")
        
        total_size_deleted = 0
        successful_deletions = 0
        failed_deletions = 0
        
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
                else:
                    print(f"    ❌ Failed to delete")
                    failed_deletions += 1
        
        print(f"\n📊 Deletion Summary:")
        print(f"   {'Would delete' if dry_run else 'Successfully deleted'}: {successful_deletions} images")
        if not dry_run:
            print(f"   Failed deletions: {failed_deletions} images")
        print(f"   {'Would save' if dry_run else 'Saved'}: {total_size_deleted / (1024**3):.2f} GB")


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Intelligent Docker image deletion with workload analysis")
    parser.add_argument("password", nargs="?", help="Password for registry access")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes and delete images (default is dry-run)")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")
    parser.add_argument("--workload-report", default=config_manager.get_workload_report_path(), help="Path to workload analysis report")
    parser.add_argument("--image-analysis", default=config_manager.get_image_analysis_path(), help="Path to image analysis report")
    parser.add_argument("--output-report", default=config_manager.get_deletion_analysis_path(), help="Path for deletion analysis report")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip workload analysis and use traditional environments file")
    parser.add_argument("--file", help="File containing ObjectIDs (first column) to filter images")
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
    
    # Parse ObjectIDs from file if provided
    object_ids = None
    if args.file:
        object_ids = read_object_ids_from_file(args.file)
        if not object_ids:
            print(f"Error: No valid ObjectIDs found in file '{args.file}'")
            sys.exit(1)
        print(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids}")
    
    # Get password if provided (optional). If absent, operations will attempt without auth.
    password = args.password or os.environ.get('REGISTRY_PASSWORD')
    
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
            if object_ids:
                print(f"   Filtering by ObjectIDs: {', '.join(object_ids)}")
            analysis = deleter.analyze_image_usage(workload_report, image_analysis, object_ids)
            
            # Generate deletion report
            deleter.generate_deletion_report(analysis, args.output_report)
            
            # Create Skopeo pod
            deleter.create_skopeo_pod()
            
            # Enable deletion
            deleter.enable_deletion_of_docker_images()
            
            # Delete unused images
            deleter.delete_unused_images(analysis, password, dry_run=dry_run)
            
            # Disable deletion
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
            print("  python delete-image.py --apply [password]")
        else:
            print("\n✅ DELETION COMPLETED")
            print("Images have been deleted from the registry.")
        
    except KeyboardInterrupt:
        print("\n⚠️  Deletion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Deletion failed: {e}")
        sys.exit(1)
    finally:
        # Clean up Skopeo pod
        try:
            deleter.delete_skopeo_pod()
        except Exception as e:
            print(f"Warning: Failed to clean up Skopeo pod: {e}")


if __name__ == '__main__':
    main()