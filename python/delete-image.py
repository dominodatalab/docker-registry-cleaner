#!/usr/bin/env python3
"""
Docker Image Deletion Tool

This script deletes Docker images from registry based on environments file,
with intelligent analysis of workload usage and image layer information.
"""

import os
import time
import json
import subprocess
import sys
import yaml
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from kubernetes import client, config
import logging


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
    """Enhanced image deletion with workload analysis"""
    
    def __init__(self, registry_url: str = "docker-registry:5000", namespace: str = "domino-platform"):
        self.registry_url = registry_url
        self.namespace = namespace
        self.logger = self._setup_logging()
        
        # Initialize Kubernetes client
        try:
            config.load_kube_config()
            self.core_v1_client = client.CoreV1Api()
            self.apps_v1_client = client.AppsV1Api()
            self.logger.info("Kubernetes client initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise
    
    def _setup_logging(self) -> logging.Logger:
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        return logging.getLogger(__name__)
    
    def load_workload_report(self, report_path: str = "workload-report.json") -> Dict:
        """Load workload analysis report"""
        try:
            with open(report_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Workload report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse workload report: {e}")
            return {}
    
    def load_image_analysis_report(self, report_path: str = "final-report.json") -> Dict:
        """Load image analysis report"""
        try:
            with open(report_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Image analysis report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse image analysis report: {e}")
            return {}
    
    def analyze_image_usage(self, workload_report: Dict, image_analysis: Dict, object_ids: Optional[List[str]] = None) -> WorkloadAnalysis:
        """Analyze which images are used vs unused based on workload and image analysis"""
        used_images = set()
        unused_images = set()
        total_size_saved = 0
        image_usage_stats = {}
        
        # Get used images from workload report
        if 'image_tags' in workload_report:
            for tag_info in workload_report['image_tags'].values():
                if tag_info.get('count', 0) > 0:
                    used_images.add(tag_info['tag'])
        
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
        if 'layers' in image_analysis:
            for layer_id, layer_info in image_analysis['layers'].items():
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
                
                # Check if any tags in this layer are used
                layer_used = any(tag in used_images for tag in layer_tags)
                
                if not layer_used and layer_tags:
                    # This layer is unused
                    unused_images.update(layer_tags)
                    layer_size = layer_info.get('size', 0)
                    total_size_saved += layer_size
                    
                    # Track usage stats
                    for tag in layer_tags:
                        image_usage_stats[tag] = {
                            'size': layer_size,
                            'layer_id': layer_id,
                            'status': 'unused',
                            'pods_using': []
                        }
                else:
                    # This layer is used
                    for tag in layer_tags:
                        if tag in used_images:
                            image_usage_stats[tag] = {
                                'size': layer_info.get('size', 0),
                                'layer_id': layer_id,
                                'status': 'used',
                                'pods_using': workload_report.get('image_tags', {}).get(tag, {}).get('pods', [])
                            }
        
        return WorkloadAnalysis(
            used_images=used_images,
            unused_images=unused_images,
            total_size_saved=total_size_saved,
            image_usage_stats=image_usage_stats
        )
    
    def generate_deletion_report(self, analysis: WorkloadAnalysis, output_file: str = "deletion-analysis.json") -> None:
        """Generate a detailed report of what can be deleted"""
        report = {
            'summary': {
                'total_images_in_use': len(analysis.used_images),
                'total_images_unused': len(analysis.unused_images),
                'total_size_saved_bytes': analysis.total_size_saved,
                'total_size_saved_mb': analysis.total_size_saved / (1024 * 1024),
                'total_size_saved_gb': analysis.total_size_saved / (1024 * 1024 * 1024)
            },
            'used_images': list(analysis.used_images),
            'unused_images': list(analysis.unused_images),
            'layer_analysis': analysis.image_usage_stats
        }
        
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        self.logger.info(f"Deletion analysis report saved to: {output_file}")
        
        # Print summary
        print(f"\n📊 Deletion Analysis Summary:")
        print(f"   Images in use: {len(analysis.used_images)}")
        print(f"   Images unused: {len(analysis.unused_images)}")
        print(f"   Potential space saved: {analysis.total_size_saved / (1024**3):.2f} GB")
        print(f"   Detailed report: {output_file}")
    
    def create_skopeo_pod(self):
        """Create the Skopeo pod for Docker registry operations"""
        print("Creating Skopeo pod")
        
        # Read the pod.yaml file
        with open("pod.yaml", "r") as f:
            pod_data = yaml.safe_load(f)
        
        # Create the pod using Kubernetes client
        try:
            self.core_v1_client.create_namespaced_pod(
                namespace=self.namespace,
                body=pod_data
            )
            print(f"Created pod skopeo in namespace {self.namespace}")
        except client.exceptions.ApiException as e:
            if e.status == 409:  # Already exists
                print("Pod skopeo already exists")
            else:
                raise
        
        # Wait for the pod to be ready
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
            if e.status == 404:  # Not found
                print("Pod skopeo not found")
            else:
                raise
    
    def _wait_for_pod_ready(self, namespace: str, name: str, timeout: int = 300, interval: int = 10):
        """Wait for a pod to be ready"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                pod = self.core_v1_client.read_namespaced_pod(name=name, namespace=namespace)
                if pod.status.phase == "Running":
                    # Check if all containers are ready
                    ready = all(
                        container.ready for container in pod.status.container_statuses or []
                    )
                    if ready:
                        print(f"Pod {name} is ready")
                        return
                print(f"Waiting for pod {name} to be ready...")
                time.sleep(interval)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    print(f"Pod {name} not found, waiting...")
                    time.sleep(interval)
                else:
                    raise
        raise TimeoutError(f"Timeout waiting for pod {name} to be ready")
    
    def run_skopeo_command(self, cmd_args: List[str]) -> Optional[str]:
        """Run a skopeo command in the pod using Kubernetes client"""
        try:
            # Create the exec request
            exec_request = client.V1ExecAction(
                command=["skopeo"] + cmd_args
            )
            
            # Execute the command in the pod
            response = self.core_v1_client.connect_get_namespaced_pod_exec(
                name="skopeo",
                namespace=self.namespace,
                command=["skopeo"] + cmd_args,
                container="skopeo",
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False
            )
            
            # Read the response
            if response:
                return response
            else:
                self.logger.error(f"Empty response from skopeo command: {' '.join(cmd_args)}")
                return None
                
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.logger.error(f"Skopeo pod not found in namespace {self.namespace}")
            else:
                self.logger.error(f"API error executing skopeo command: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error executing skopeo command: {e}")
            return None
    
    def get_docker_tags(self, repository: str, password: str) -> List[str]:
        """Get all Docker tags for a repository"""
        cmd = [
            "list-tags", "--tls-verify=false", 
            f"--creds", f"domino-registry:{password}",
            f"docker://{self.registry_url}/{repository}"
        ]
        
        output = self.run_skopeo_command(cmd)
        if output:
            try:
                tags_data = json.loads(output)
                return tags_data.get('Tags', [])
            except json.JSONDecodeError:
                self.logger.error(f"Failed to parse tags for {repository}")
                return []
        return []
    
    def delete_image_tag(self, repository: str, tag: str, password: str) -> bool:
        """Delete a specific image tag"""
        cmd = [
            "delete", "--tls-verify=false",
            f"--creds", f"domino-registry:{password}",
            f"docker://{self.registry_url}/{repository}:{tag}"
        ]
        
        output = self.run_skopeo_command(cmd)
        return output is not None
    
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
            print("Updated StatefulSet with deletion enabled")
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error updating StatefulSet: {e}")
            return
        
        # Wait for the pod to be ready
        self._wait_for_pod_ready(self.namespace, "docker-registry-0")
    
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
        
        # Remove the environment variable
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
            print("Updated StatefulSet with deletion disabled")
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error updating StatefulSet: {e}")
            return
        
        # Wait for the pod to be ready
        self._wait_for_pod_ready(self.namespace, "docker-registry-0")
    
    def delete_unused_images(self, analysis: WorkloadAnalysis, password: str, dry_run: bool = True) -> None:
        """Delete unused images based on workload analysis"""
        if dry_run:
            print("🔍 DRY RUN MODE: Analyzing what would be deleted")
        else:
            print("🗑️  DELETE MODE: Actually deleting unused images")
        
        deleted_count = 0
        total_size_deleted = 0
        
        # Group unused images by repository
        repositories = {}
        for image_tag in analysis.unused_images:
            # Extract repository from image tag (assuming format: repo/image:tag)
            if ':' in image_tag:
                repo_part, tag = image_tag.rsplit(':', 1)
                if '/' in repo_part:
                    repo = repo_part.split('/')[0]
                    image = repo_part.split('/')[1]
                else:
                    repo = "dominodatalab"
                    image = repo_part
            else:
                repo = "dominodatalab"
                image = image_tag
                tag = "latest"
            
            if repo not in repositories:
                repositories[repo] = {}
            if image not in repositories[repo]:
                repositories[repo][image] = []
            repositories[repo][image].append(tag)
        
        # Delete images from each repository
        for repo, images in repositories.items():
            for image, tags in images.items():
                repository = f"{repo}/{image}"
                
                for tag in tags:
                    if dry_run:
                        print(f"[DRY RUN] Would delete {repository}:{tag}")
                    else:
                        print(f"Deleting {repository}:{tag}")
                        if self.delete_image_tag(repository, tag, password):
                            deleted_count += 1
                            # Find the size of this tag in the analysis
                            for layer_info in analysis.image_usage_stats.values():
                                if tag in layer_info['tags']:
                                    total_size_deleted += layer_info['size']
                                    break
                        else:
                            print(f"Failed to delete {repository}:{tag}")
        
        if dry_run:
            print(f"\n🔍 DRY RUN SUMMARY:")
            print(f"   Would delete {len(analysis.unused_images)} image tags")
            print(f"   Would save {analysis.total_size_saved / (1024**3):.2f} GB")
        else:
            print(f"\n✅ DELETION SUMMARY:")
            print(f"   Deleted {deleted_count} image tags")
            print(f"   Saved {total_size_deleted / (1024**3):.2f} GB")


def read_object_ids_from_file(file_path: str) -> List[str]:
    """Read ObjectIDs from a file, extracting the first column"""
    object_ids = []
    try:
        with open(file_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    parts = line.split()
                    if parts:
                        obj_id = parts[0]  # First column is the ObjectID
                        if len(obj_id) == 24:
                            try:
                                int(obj_id, 16)  # Validate hexadecimal
                                object_ids.append(obj_id)
                            except ValueError:
                                print(f"Warning: Invalid ObjectID '{obj_id}' on line {line_num}")
                        else:
                            print(f"Warning: ObjectID '{obj_id}' on line {line_num} is not 24 characters")
        return object_ids
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found")
        return []
    except Exception as e:
        print(f"Error reading file '{file_path}': {e}")
        return []


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Intelligent Docker image deletion with workload analysis")
    parser.add_argument("password", nargs="?", help="Password for registry access")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes and delete images (default is dry-run)")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")
    parser.add_argument("--workload-report", default="workload-report.json", help="Path to workload analysis report")
    parser.add_argument("--image-analysis", default="final-report.json", help="Path to image analysis report")
    parser.add_argument("--output-report", default="deletion-analysis.json", help="Path for deletion analysis report")
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
    args = parse_arguments()
    
    # Parse ObjectIDs from file if provided
    object_ids = None
    if args.file:
        object_ids = read_object_ids_from_file(args.file)
        if not object_ids:
            print(f"Error: No valid ObjectIDs found in file '{args.file}'")
            sys.exit(1)
        print(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids}")
    
    # Get password
    password = args.password or os.environ.get('SKOPEO_PASSWORD')
    if not password:
        print("Error: No password provided. Use SKOPEO_PASSWORD environment variable or provide as argument.")
        sys.exit(1)
    
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