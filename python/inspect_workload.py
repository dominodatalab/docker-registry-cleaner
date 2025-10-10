#!/usr/bin/env python3
"""
Kubernetes Workload Inspection Tool

This script inspects running pods in a Kubernetes cluster and analyzes
container images to generate workload reports.
"""

import argparse
import concurrent.futures
import json
import sys
import os

from dataclasses import dataclass
from kubernetes import client, config
from pathlib import Path
from typing import Dict, List, Optional, Set

from config_manager import config_manager
from logging_utils import setup_logging, get_logger
from object_id_utils import read_typed_object_ids_from_file

logger = get_logger(__name__)


@dataclass
class PodInfo:
	"""Data class for pod information"""
	name: str
	namespace: str
	images: List[str]
	labels: Dict[str, str]
	workload_type: str = ""
	project_name: str = ""
	owner_username: str = ""

	def to_dict(self) -> Dict:
		"""Convert to dictionary for JSON serialization"""
		return {
			'name': self.name,
			'namespace': self.namespace,
			'images': self.images,
			'labels': self.labels,
			'workload_type': self.workload_type,
			'project_name': self.project_name,
			'owner_username': self.owner_username
		}


@dataclass
class ImageTagInfo:
	"""Data class for image tag information"""
	tag: str
	pods: Set[str]
	count: int
	labels: List[Dict[str, str]]
	workload_count: int = 0

	def to_dict(self) -> Dict:
		"""Convert to dictionary for JSON serialization"""
		return {
			'tag': self.tag,
			'pods': list(self.pods),
			'count': self.count,
			'labels': self.labels,
			'workload_count': self.workload_count
		}


class WorkloadInspector:
	"""Main class for inspecting Kubernetes workloads"""
	
	def __init__(self, registry_url: str, prefix_to_remove: str, namespace: str):
		self.registry_url = registry_url
		self.prefix_to_remove = prefix_to_remove
		self.namespace = namespace
		self.logger = get_logger(__name__)
		
		# Initialize Kubernetes client
		try:
			# Detect in-cluster environment: common indicators are env vars or SA token path
			in_cluster = bool(
				os.environ.get("KUBERNETES_SERVICE_HOST")
				or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
			)
			if in_cluster:
				config.load_incluster_config()
				self.logger.info("Kubernetes client initialized with in-cluster config")
			else:
				config.load_kube_config()
				self.logger.info("Kubernetes client initialized from local kubeconfig")
			self.core_v1_client = client.CoreV1Api()
		except Exception as e:
			# Fallback: if local failed, try in-cluster, and vice versa
			try:
				config.load_incluster_config()
				self.core_v1_client = client.CoreV1Api()
				self.logger.info("Kubernetes client fallback to in-cluster config succeeded")
			except Exception as e2:
				self.logger.error(f"Failed to initialize Kubernetes client: {e}; fallback error: {e2}")
				raise
	
	def get_running_pods(self, prefix: Optional[str] = None) -> List[str]:
		"""Get running pods in the namespace, optionally filtered by prefix"""
		try:
			# Get pods from the namespace
			pods = self.core_v1_client.list_namespaced_pod(
				namespace=self.namespace,
				field_selector="status.phase=Running"
			)
			
			pod_names = [pod.metadata.name for pod in pods.items]
			
			if prefix:
				return [pod for pod in pod_names if pod.startswith(prefix)]
			else:
				return pod_names
				
		except client.exceptions.ApiException as e:
			self.logger.error(f"Failed to get pods from namespace {self.namespace}: {e}")
			return []
		except Exception as e:
			self.logger.error(f"Unexpected error getting pods: {e}")
			return []
	
	def get_pod_info(self, pod_name: str) -> Optional[PodInfo]:
		"""Get detailed information for a specific pod"""
		try:
			# Get pod details using Kubernetes client
			pod = self.core_v1_client.read_namespaced_pod(
				name=pod_name,
				namespace=self.namespace
			)
			
			# Extract container images
			images = []
			for container in pod.spec.containers:
				images.append(container.image)
			
			# Extract labels
			labels = pod.metadata.labels or {}
			
			# Extract key information
			workload_type = labels.get('dominodatalab.com/workload-type', '')
			project_name = labels.get('dominodatalab.com/project-name', '')
			owner_username = labels.get('dominodatalab.com/project-owner-username', '')
			
			return PodInfo(
				name=pod_name,
				namespace=self.namespace,
				images=images,
				labels=labels,
				workload_type=workload_type,
				project_name=project_name,
				owner_username=owner_username
			)
			
		except client.exceptions.ApiException as e:
			if e.status == 404:
				self.logger.warning(f"Pod {pod_name} not found in namespace {self.namespace}")
			else:
				self.logger.error(f"API error getting info for pod {pod_name}: {e}")
			return None
		except Exception as e:
			self.logger.error(f"Error getting info for pod {pod_name}: {e}")
			return None
	
	def filter_images_by_registry(self, images: List[str]) -> List[str]:
		"""Filter images to only include those from the target registry"""
		return [img for img in images if img.startswith(self.registry_url)]

	def filter_images_by_object_ids(self, images: List[str], object_ids: Optional[List[str]] = None) -> List[str]:
		"""Filter images to only include those that start with one of the provided ObjectIDs"""
		if not object_ids:
			return images
		
		filtered_images = []
		for image in images:
			# Remove the registry prefix to get just the tag
			tag = self.remove_prefix_from_image(image)
			# Check if the tag starts with any of the provided ObjectIDs
			for obj_id in object_ids:
				if tag.startswith(obj_id):
					filtered_images.append(image)
					break
		
		return filtered_images

	def remove_prefix_from_image(self, image: str) -> str:
		"""Remove the registry prefix from an image tag"""
		if image.startswith(self.prefix_to_remove):
			return image[len(self.prefix_to_remove):]
		return image

	def analyze_pods_parallel(self, pod_prefixes: List[str], max_workers: int = 4, object_ids: Optional[List[str]] = None) -> Dict[str, ImageTagInfo]:
		"""Analyze pods in parallel and collect image tag information"""
		image_tags: Dict[str, ImageTagInfo] = {}
		
		# Get all running pods
		all_pods = []
		for prefix in pod_prefixes:
			pods = self.get_running_pods(prefix)
			all_pods.extend(pods)
		
		if not all_pods:
			self.logger.warning("No running pods found with the specified prefixes")
			return image_tags
		
		self.logger.info(f"Found {len(all_pods)} running pods to analyze")
		
		# Process pods in parallel
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			# Submit all pod analysis tasks
			future_to_pod = {
				executor.submit(self.get_pod_info, pod_name): pod_name 
				for pod_name in all_pods
			}
			
			# Process completed tasks
			completed = 0
			for future in concurrent.futures.as_completed(future_to_pod):
				pod_name = future_to_pod[future]
				try:
					pod_info = future.result()
					if pod_info:
						# Filter images by ObjectIDs if provided
						if object_ids:
							original_count = len(pod_info.images)
							pod_info.images = self.filter_images_by_object_ids(pod_info.images, object_ids)
							filtered_count = len(pod_info.images)
							if filtered_count < original_count:
								self.logger.info(f"Filtered {pod_name}: {filtered_count}/{original_count} images match ObjectIDs")
						
						self._process_pod_info(pod_info, image_tags)
				except Exception as e:
					self.logger.error(f"Error processing pod {pod_name}: {e}")
				finally:
					completed += 1
					if completed % 10 == 0:
						self.logger.info(f"Processed {completed}/{len(all_pods)} pods")
		
		return image_tags
	
	def _process_pod_info(self, pod_info: PodInfo, image_tags: Dict[str, ImageTagInfo]) -> None:
		"""Process pod information and update image tags"""
		# Filter images by registry
		filtered_images = self.filter_images_by_registry(pod_info.images)
		
		for image in filtered_images:
			clean_tag = self.remove_prefix_from_image(image)
			
			if clean_tag not in image_tags:
				image_tags[clean_tag] = ImageTagInfo(
					tag=clean_tag,
					pods=set(),
					count=0,
					labels=[],
					workload_count=0
				)
			
			# Add pod to image tag info
			if pod_info.name not in image_tags[clean_tag].pods:
				image_tags[clean_tag].pods.add(pod_info.name)
				image_tags[clean_tag].count += 1
				
				# Add label information
				label_info = {
					'Pod Name': pod_info.name,
					'dominodatalab.com/project-name': pod_info.project_name,
					'dominodatalab.com/project-owner-username': pod_info.owner_username,
					'dominodatalab.com/workload-type': pod_info.workload_type
				}
				
				if label_info not in image_tags[clean_tag].labels:
					image_tags[clean_tag].labels.append(label_info)
				
				# Count workload types
				if pod_info.workload_type:
					image_tags[clean_tag].workload_count += 1
	
	def generate_report(self, image_tags: Dict[str, ImageTagInfo], output_file: str) -> None:
		"""Generate workload report"""
		self.logger.info("Generating workload report...")
		
		# Save JSON report
		output_path = Path(output_file)
		
		# Ensure parent directory exists and save outputs
		output_path.parent.mkdir(parents=True, exist_ok=True)
		
		# Save JSON format
		json_data = {tag: info.to_dict() for tag, info in image_tags.items()}
		with open(f"{output_path}.json", "w") as f:
			json.dump(json_data, f, indent=2)
		
		self.logger.info(f"Workload report saved to {output_path}.json")
		
		# Print summary
		total_pods = sum(info.count for info in image_tags.values())
		total_images = len(image_tags)
		total_workloads = sum(info.workload_count for info in image_tags.values())
		self.logger.info("\n📊 Workload Analysis Summary:")
		self.logger.info(f"   Total unique images: {total_images}")
		self.logger.info(f"   Total pods analyzed: {total_pods}")
		self.logger.info(f"   Total workloads: {total_workloads}")
		self.logger.info(f"   Report saved to: {output_path}.json")


def parse_arguments():
	"""Parse command line arguments"""
	parser = argparse.ArgumentParser(
		description="Inspect Kubernetes workloads and analyze container images",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""
Examples:
  # Basic usage (uses config values)
  python inspect_workload.py

  # Custom namespace and pod prefixes
  python inspect_workload.py --namespace my-namespace --pod-prefixes app- job-

  # Custom output file
  python inspect_workload.py --output-file my-workload-report

  # Parallel processing
  python inspect_workload.py --max-workers 8

  # Filter by ObjectIDs from file
  python inspect_workload.py --file environments
		"""
	)
	
	
	parser.add_argument(
		'--prefix-to-remove', 
		help='Prefix to remove from image tags (defaults to registry-url + /)'
	)
	
	parser.add_argument(
		'--namespace',
		help='Kubernetes namespace (defaults to config value)'
	)
	
	parser.add_argument(
		'--pod-prefixes',
		nargs='+',
		help='Pod name prefixes to filter (defaults to config value)'
	)
	
	parser.add_argument(
		'--output-file',
		help='Output file base path (defaults to config value)'
	)
	
	parser.add_argument(
		'--max-workers',
		type=int,
		help='Maximum number of parallel workers (defaults to config value)'
	)
	
	parser.add_argument(
		'--file',
		help='File containing ObjectIDs (one per line) to filter images'
	)
	
	return parser.parse_args()


def main():
	"""Main function"""
	setup_logging()
	args = parse_arguments()
	
	# Get registry URL from config
	registry_url = config_manager.get_registry_url()
	
	# Get prefix to remove from args or derive from registry URL
	prefix_to_remove = args.prefix_to_remove or f"{registry_url}/"
	
	# Get namespace from args or config
	namespace = args.namespace or config_manager.get_compute_namespace()
	
	# Get pod prefixes from args or config
	pod_prefixes = args.pod_prefixes or config_manager.get_pod_prefixes()
	
	# Get max workers from args or config
	max_workers = args.max_workers or config_manager.get_max_workers()
	
	logger.info(f"Using registry URL: {registry_url}")
	logger.info(f"Using prefix to remove: {prefix_to_remove}")
	logger.info(f"Using namespace: {namespace}")
	logger.info(f"Using pod prefixes: {pod_prefixes}")
	logger.info(f"Using max workers: {max_workers}")
	
	# Parse ObjectIDs (typed) from file if provided
	object_ids_map = None
	if args.file:
		object_ids_map = read_typed_object_ids_from_file(args.file)
		any_ids = set(object_ids_map.get('any', [])) if object_ids_map else set()
		env_ids = list(any_ids.union(object_ids_map.get('environment', []))) if object_ids_map else []
		model_ids = list(any_ids.union(object_ids_map.get('model', []))) if object_ids_map else []
		if not (env_ids or model_ids):
			logger.error(f"No valid ObjectIDs found in file '{args.file}'")
			sys.exit(1)
		logger.info(f"Filtering by ObjectIDs from file '{args.file}' (environment={len(env_ids)}, model={len(model_ids)})")
	
	try:
		# Create inspector
		inspector = WorkloadInspector(
			registry_url=registry_url,
			prefix_to_remove=prefix_to_remove,
			namespace=namespace
		)
		
		logger.info("----------------------------------")
		logger.info("   Kubernetes cluster scanning")
		if object_ids_map:
			total_ids = sum(len(ids) for ids in object_ids_map.values())
			logger.info(f"   Filtering by {total_ids} ObjectIDs from file '{args.file}'")
		logger.info("----------------------------------")
		
		# Combine environment and model IDs for filtering
		combined_object_ids = None
		if object_ids_map:
			any_ids = set(object_ids_map.get('any', []))
			env_ids = list(any_ids.union(object_ids_map.get('environment', [])))
			model_ids = list(any_ids.union(object_ids_map.get('model', [])))
			combined_object_ids = list(set(env_ids + model_ids))
		
		# Analyze pods
		image_tags = inspector.analyze_pods_parallel(
			pod_prefixes=pod_prefixes,
			max_workers=max_workers,
			object_ids=combined_object_ids
		)
		
		if not image_tags:
			logger.warning("No image tags found. Check your registry URL, pod prefixes, and ObjectID filters.")
			sys.exit(0)
		
		# Generate report using config-managed path base (strip .json suffix)
		workload_json = Path(config_manager.get_workload_report_path())
		workload_base = str(workload_json.parent / workload_json.stem)
		inspector.generate_report(image_tags, workload_base)
		
		logger.info("\n✅ Workload inspection completed successfully!")
		
	except KeyboardInterrupt:
		logger.warning("\n⚠️  Workload inspection interrupted by user")
		sys.exit(1)
	except Exception as e:
		logger.error(f"\n❌ Workload inspection failed: {e}")
		sys.exit(1)


if __name__ == "__main__":
	main()
