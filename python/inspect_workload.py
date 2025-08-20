#!/usr/bin/env python3
"""
Kubernetes Workload Inspection Tool

This script inspects running pods in a Kubernetes cluster and analyzes
container images to generate workload reports.
"""

import argparse
import json
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional, Set
import concurrent.futures
from dataclasses import dataclass
from tabulate import tabulate
import tqdm
from kubernetes import client, config
from logging_utils import setup_logging, get_logger
from object_id_utils import read_object_ids_from_file
from config_manager import config_manager

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
	
	def __init__(self, registry_url: str, prefix_to_remove: str, namespace: str = "domino-compute"):
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
			
			# Process completed tasks with progress bar
			with tqdm.tqdm(total=len(all_pods), desc="Analyzing pods") as pbar:
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
						pbar.update(1)
		
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
		
		# Prepare table data
		headers = ["Tag", "Num of Pods", "Workload Count", "Pods Info"]
		rows = []
		
		for tag, info in image_tags.items():
			pod_names = ', '.join(sorted(info.pods))
			count = info.count
			workload_count = info.workload_count
			labels_str = json.dumps(info.labels, indent=2)
			
			rows.append([tag, count, workload_count, labels_str])
		
		# Sort by number of pods (descending)
		rows.sort(key=lambda x: x[1], reverse=True)
		
		# Generate table
		table = tabulate(rows, headers=headers, tablefmt="grid")
		
		# Save files
		output_path = Path(output_file)
		
		# Ensure parent directory exists and save outputs
		output_path.parent.mkdir(parents=True, exist_ok=True)
		# Save table format
		with open(f"{output_path}.txt", "w") as f:
			f.write(table)
		
		# Save JSON format
		json_data = {tag: info.to_dict() for tag, info in image_tags.items()}
		with open(f"{output_path}.json", "w") as f:
			json.dump(json_data, f, indent=2)
		
		self.logger.info(f"Workload report saved to {output_path}.txt and {output_path}.json")
		
		# Print summary
		total_pods = sum(info.count for info in image_tags.values())
		total_images = len(image_tags)
		total_workloads = sum(info.workload_count for info in image_tags.values())
		self.logger.info("\n📊 Workload Analysis Summary:")
		self.logger.info(f"   Total unique images: {total_images}")
		self.logger.info(f"   Total pods analyzed: {total_pods}")
		self.logger.info(f"   Total workloads: {total_workloads}")
		self.logger.info(f"   Reports saved to: {output_path}.txt and {output_path}.json")


def parse_arguments():
	"""Parse command line arguments"""
	parser = argparse.ArgumentParser(
		description="Inspect Kubernetes workloads and analyze container images",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""
Examples:
  # Basic usage
  python inspect_workload.py --registry-url registry.example.com --prefix-to-remove registry.example.com/

  # Custom namespace and pod prefixes
  python inspect_workload.py --registry-url registry.example.com --prefix-to-remove registry.example.com/ --namespace my-namespace --pod-prefixes app- job-

  # Custom output file
  python inspect_workload.py --registry-url registry.example.com --prefix-to-remove registry.example.com/ --output-file my-workload-report

  # Parallel processing
  python inspect_workload.py --registry-url registry.example.com --prefix-to-remove registry.example.com/ --max-workers 8

  # Filter by ObjectIDs from file
  python inspect_workload.py --registry-url registry.example.com --prefix-to-remove registry.example.com/ --file environments
		"""
	)
	
	parser.add_argument(
		'--registry-url', 
		required=True,
		help='Container registry URL'
	)
	
	parser.add_argument(
		'--prefix-to-remove', 
		required=True,
		help='Prefix to remove from image tags'
	)
	
	parser.add_argument(
		'--namespace',
		default='domino-compute',
		help='Kubernetes namespace (default: domino-compute)'
	)
	
	parser.add_argument(
		'--pod-prefixes',
		nargs='+',
		default=['model-', 'run-'],
		help='Pod name prefixes to filter (default: model- run-)'
	)
	
	parser.add_argument(
		'--output-file',
		default='reports/workload-report',
		help='Output file base path (default: reports/workload-report)'
	)
	
	parser.add_argument(
		'--max-workers',
		type=int,
		default=4,
		help='Maximum number of parallel workers (default: 4)'
	)
	
	parser.add_argument(
		'--file',
		help='File containing ObjectIDs (first column) to filter images'
	)
	
	return parser.parse_args()


def main():
	"""Main function"""
	setup_logging()
	args = parse_arguments()
	
	# Parse ObjectIDs from file if provided
	object_ids = None
	if args.file:
		object_ids = read_object_ids_from_file(args.file)
		if not object_ids:
			logger.error(f"No valid ObjectIDs found in file '{args.file}'")
			sys.exit(1)
		logger.info(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids}")
	
	try:
		# Create inspector
		inspector = WorkloadInspector(
			registry_url=args.registry_url,
			prefix_to_remove=args.prefix_to_remove,
			namespace=args.namespace
		)
		
		logger.info("----------------------------------")
		logger.info("   Kubernetes cluster scanning")
		if object_ids:
			logger.info(f"   Filtering by ObjectIDs: {', '.join(object_ids)}")
		logger.info("----------------------------------")
		
		# Analyze pods
		image_tags = inspector.analyze_pods_parallel(
			pod_prefixes=args.pod_prefixes,
			max_workers=args.max_workers,
			object_ids=object_ids
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
