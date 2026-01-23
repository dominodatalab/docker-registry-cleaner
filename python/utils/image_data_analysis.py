#!/usr/bin/env python3
"""
Docker Image and Layer Analysis Tool

This script analyzes Docker registry images and extracts layer information,
using native Python data structures for efficient data management and analysis.

Data Model:
- Layers: dict mapping layer_id -> {size_bytes, ref_count}
- Images: dict mapping image_id -> {repository, tag, digest}
- Image-to-Layer Mapping: list of {image_id, layer_id, order_index}
"""

import argparse
import concurrent.futures
import sys

from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import SkopeoClient, config_manager
from utils.logging_utils import get_logger, setup_logging
from utils.object_id_utils import read_typed_object_ids_from_file
from utils.report_utils import save_json

logger = get_logger(__name__)


class ImageAnalyzer:
    """Analyzes Docker images and their layers using native Python data structures"""
    
    def __init__(self, registry_url: str, repository: str):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(config_manager, use_pod=config_manager.get_skopeo_use_pod())
        
        # Initialize data structures
        self.layers = {}  # layer_id -> {size_bytes, ref_count}
        self.images = {}  # image_id -> {repository, tag, digest}
        self.image_layers = []  # [{image_id, layer_id, order_index}, ...]
        
        self.logger = get_logger(__name__)
    
    def filter_tags_by_object_ids(self, tags: List[str], object_ids: Optional[List[str]] = None) -> List[str]:
        """Filter tags to only include those that start with one of the provided ObjectIDs"""
        if not object_ids:
            return tags
        
        filtered_tags = []
        for tag in tags:
            # Check if the tag starts with any of the provided ObjectIDs
            for obj_id in object_ids:
                if tag.startswith(obj_id):
                    filtered_tags.append(tag)
                    break
        
        return filtered_tags
    
    def _inspect_single_tag(self, image_type: str, tag: str) -> Optional[Dict]:
        """Inspect a single tag and return image data
        
        Returns:
            Dict with image_id, repository, tag, digest, and layers_data, or None if inspection fails
        """
        try:
            # Inspect image using standardized client
            image_info = self.skopeo_client.inspect_image(f"{self.repository}/{image_type}", tag)
            if not image_info:
                self.logger.error(f"Failed to inspect image {image_type}:{tag}")
                return None
            
            # Extract image metadata
            digest = image_info.get('Digest', '')
            image_id = f"{image_type}:{tag}"
            layers_data = image_info.get('LayersData', [])
            
            return {
                'image_id': image_id,
                'repository': f"{self.repository}/{image_type}",
                'tag': tag,
                'digest': digest,
                'layers_data': layers_data
            }
        except Exception as e:
            self.logger.error(f"Error inspecting {image_type}:{tag}: {e}")
            return None
    
    def analyze_image(self, image_type: str, object_ids: Optional[List[str]] = None, max_workers: int = 4) -> bool:
        """Analyze a single image type (e.g., 'environment', 'model') with parallel tag inspection
        
        Args:
            image_type: Type of image to analyze
            object_ids: Optional list of ObjectIDs to filter tags
            max_workers: Number of parallel workers for tag inspection
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get tags using standardized client
            tags = self.skopeo_client.list_tags(f"{self.repository}/{image_type}")

            # Skip internal/cache tags
            original_count = len(tags)
            tags = [t for t in tags if t != "buildcache"]
            if len(tags) != original_count:
                self.logger.info(f"Skipping {original_count - len(tags)} 'buildcache' tag(s) for {image_type}")
            
            # Filter tags by ObjectIDs if provided
            if object_ids:
                original_count = len(tags)
                tags = self.filter_tags_by_object_ids(tags, object_ids)
                filtered_count = len(tags)
                self.logger.info(f"Filtered tags for {image_type}: {filtered_count}/{original_count} tags match the provided ObjectIDs")
                
                if filtered_count == 0:
                    self.logger.warning(f"No tags found matching the provided ObjectIDs for image: {image_type}")
                    return False
            
            self.logger.info(f"Analyzing {len(tags)} tags for {image_type} (using {max_workers} workers)...")
            
            # Process tags in parallel
            tag_data_list = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tag inspection tasks
                future_to_tag = {
                    executor.submit(self._inspect_single_tag, image_type, tag): tag
                    for tag in tags
                }
                
                # Process completed tasks with progress tracking
                completed = 0
                total = len(tags)
                for future in concurrent.futures.as_completed(future_to_tag):
                    tag = future_to_tag[future]
                    completed += 1
                    
                    try:
                        tag_data = future.result()
                        if tag_data:
                            tag_data_list.append(tag_data)
                            
                            # Log progress every 10 tags or at the end
                            if completed % 10 == 0 or completed == total:
                                self.logger.info(f"  Progress: {completed}/{total} tags processed ({completed/total*100:.1f}%)")
                    except Exception as e:
                        self.logger.error(f"  Error processing {tag}: {e}")
            
            self.logger.info(f"Successfully inspected {len(tag_data_list)}/{len(tags)} tags")
            
            # Now process the collected data (must be sequential to maintain data integrity)
            for tag_data in tag_data_list:
                image_id = tag_data['image_id']
                
                # Add image to images dict
                self.images[image_id] = {
                    'repository': tag_data['repository'],
                    'tag': tag_data['tag'],
                    'digest': tag_data['digest']
                }
                
                # Extract layers
                for order_index, layer in enumerate(tag_data['layers_data']):
                    layer_id = layer['Digest']
                    layer_size = layer['Size']
                    
                    # Add or update layer in layers dict
                    if layer_id in self.layers:
                        # Layer exists, increment ref_count
                        self.layers[layer_id]['ref_count'] += 1
                    else:
                        # New layer
                        self.layers[layer_id] = {
                            'size_bytes': layer_size,
                            'ref_count': 1
                        }
                    
                    # Add image-to-layer mapping
                    self.image_layers.append({
                        'image_id': image_id,
                        'layer_id': layer_id,
                        'order_index': order_index
                    })
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to retrieve information for image: {image_type}")
            self.logger.error(f"Error: {e}")
            return False
    
    def get_image_total_size(self, image_id: str) -> int:
        """Calculate total size of an image (sum of all its layers).
        
        Args:
            image_id: Image ID to calculate size for
        
        Returns:
            Total bytes for all layers in the image
        """
        total_size = 0
        for mapping in self.image_layers:
            if mapping['image_id'] == image_id:
                layer_id = mapping['layer_id']
                layer_data = self.layers.get(layer_id)
                if layer_data:
                    total_size += layer_data['size_bytes']
        return int(total_size)
    
    def freed_space_if_deleted(self, image_ids: List[str]) -> int:
        """Calculate space that would be freed by deleting one or more images.
        
        Args:
            image_ids: List of image_ids to simulate deletion
        
        Returns:
            Total bytes that would be freed
        """
        # Count how many of the to-be-deleted images use each layer
        layer_ids_to_delete = [
            mapping['layer_id'] 
            for mapping in self.image_layers 
            if mapping['image_id'] in image_ids
        ]
        layers_to_delete = Counter(layer_ids_to_delete)
        
        total_freed = 0
        for layer_id, delete_count in layers_to_delete.items():
            # Get current ref_count for this layer
            layer_data = self.layers.get(layer_id)
            if not layer_data:
                continue
            
            current_ref = layer_data['ref_count']
            
            # If this layer would have 0 references after deletion, it will be freed
            if current_ref == delete_count:
                layer_size = layer_data['size_bytes']
                total_freed += layer_size
        
        return int(total_freed)
    
    def get_unused_images(self, used_tags: List[str]) -> List[Dict]:
        """Get images that are not in the used_tags list.
        
        Args:
            used_tags: List of tags that are currently in use
        
        Returns:
            List of unused image dicts with image_id added
        """
        used_tags_set = set(used_tags)
        unused_images = []
        for image_id, image_data in self.images.items():
            if image_data['tag'] not in used_tags_set:
                unused_images.append({
                    'image_id': image_id,
                    **image_data
                })
        return unused_images
    
    def generate_summary_stats(self) -> Dict:
        """Generate summary statistics about the analyzed images and layers"""
        total_images = len(self.images)
        total_layers = len(self.layers)
        total_size = sum(layer['size_bytes'] for layer in self.layers.values())
        
        # Layers used by only one image
        single_use_layers = [layer for layer in self.layers.values() if layer['ref_count'] == 1]
        single_use_size = sum(layer['size_bytes'] for layer in single_use_layers)
        
        # Shared layers (used by multiple images)
        shared_layers = [layer for layer in self.layers.values() if layer['ref_count'] > 1]
        shared_size = sum(layer['size_bytes'] for layer in shared_layers)
        
        # Calculate average reference count
        avg_ref_count = (
            sum(layer['ref_count'] for layer in self.layers.values()) / total_layers 
            if total_layers > 0 else 0
        )
        
        return {
            'total_images': total_images,
            'total_layers': total_layers,
            'total_size_gb': round(total_size / (1024**3), 2),
            'single_use_layers': len(single_use_layers),
            'single_use_size_gb': round(single_use_size / (1024**3), 2),
            'shared_layers': len(shared_layers),
            'shared_size_gb': round(shared_size / (1024**3), 2),
            'avg_layers_per_image': round(len(self.image_layers) / total_images, 2) if total_images > 0 else 0,
            'avg_ref_count': round(avg_ref_count, 2)
        }
    
    def get_images_by_tag_prefix(self, prefix: str) -> List[Dict]:
        """Get all images whose tags start with the given prefix (e.g., ObjectID)"""
        matching_images = []
        for image_id, image_data in self.images.items():
            if image_data['tag'].startswith(prefix):
                matching_images.append({
                    'image_id': image_id,
                    **image_data
                })
        return matching_images
    
    def export_to_legacy_format(self) -> Dict:
        """Export data in the legacy format for backward compatibility.
        
        Returns a dict mapping layer_id to {'size': int, 'tags': [str], 'environments': [str]}
        """
        legacy_data = {}
        
        for layer_id, layer_data in self.layers.items():
            # Get all image-layer mappings for this layer
            image_ids = [
                mapping['image_id'] 
                for mapping in self.image_layers 
                if mapping['layer_id'] == layer_id
            ]
            
            # Get the tags for these images (use sets for deduplication)
            tag_set = set()
            env_set = set()
            
            for image_id in image_ids:
                if image_id in self.images:
                    tag = self.images[image_id]['tag']
                    tag_set.add(tag)
                    # Extract environment ID (first part before '-')
                    env_id = tag.split('-')[0] if '-' in tag else tag
                    env_set.add(env_id)
            
            legacy_data[layer_id] = {
                'size': int(layer_data['size_bytes']),
                'tags': list(tag_set),
                'environments': list(env_set)
            }
        
        return legacy_data
    
    def save_reports(self):
        """Save analysis reports to files"""
        # Get output paths from config
        final_output_file = config_manager.get_image_analysis_path()
        tags_per_layer_output_file = config_manager.get_tags_per_layer_path()
        layers_and_sizes_output_file = config_manager.get_layers_and_sizes_path()
        filtered_layers_output_file = config_manager.get_filtered_layers_path()
        tag_sums_output_file = config_manager.get_tag_sums_path()
        images_report_output_file = config_manager.get_images_report_path()
        
        # Export to legacy format (for backward compatibility)
        # Use timestamp=True for auto-generated reports
        legacy_data = self.export_to_legacy_format()
        saved_path = save_json(final_output_file, legacy_data, timestamp=True)
        self.logger.info(f"Image analysis saved to: {saved_path}")
        
        # Tags per layer
        tags_per_layer = {
            layer_id: layer_data['ref_count'] 
            for layer_id, layer_data in self.layers.items()
        }
        saved_path = save_json(tags_per_layer_output_file, tags_per_layer, timestamp=True)
        self.logger.info(f"Tags per layer count saved to: {saved_path}")
        
        # Layers and sizes
        layers_and_sizes = {
            layer_id: int(layer_data['size_bytes'])
            for layer_id, layer_data in self.layers.items()
        }
        saved_path = save_json(layers_and_sizes_output_file, layers_and_sizes, timestamp=True)
        self.logger.info(f"Layers and sizes saved to: {saved_path}")
        
        # Filtered layers (ref_count == 1)
        filtered_legacy = {}
        for layer_id, layer_data in self.layers.items():
            if layer_data['ref_count'] == 1 and layer_id in legacy_data:
                filtered_legacy[layer_id] = legacy_data[layer_id]
        saved_path = save_json(filtered_layers_output_file, filtered_legacy, timestamp=True)
        self.logger.info(f"Filtered layers saved to: {saved_path}")
        
        # Tag sums (sum of single-use layer sizes per tag)
        tag_sums = {}
        for layer_id, data in filtered_legacy.items():
            for tag in data['tags']:
                if tag not in tag_sums:
                    tag_sums[tag] = {
                        'size': 0,
                        'environments': data['environments']
                    }
                tag_sums[tag]['size'] += data['size']
        saved_path = save_json(tag_sums_output_file, tag_sums, timestamp=True)
        self.logger.info(f"Tag sums saved to: {saved_path}")
        
        # Images report (comprehensive)
        images_report = {
            'summary': self.generate_summary_stats(),
            'layers': legacy_data
        }
        saved_path = save_json(f"{images_report_output_file}.json", images_report, timestamp=True)
        self.logger.info(f"Images report saved to: {saved_path}")


def main():
    setup_logging()
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Analyze Docker registry images and extract layer information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use config_manager defaults
  python image_data_analysis.py
  
  # Filter by ObjectIDs from file
  python image_data_analysis.py --file environments environment model
        """
    )
    
    parser.add_argument("--file", help="File containing ObjectIDs (first column) to filter images (requires prefixes: environment:, environmentRevision:, model:, or modelVersion:)")
    parser.add_argument("--max-workers", type=int, help="Maximum number of parallel workers (default: from config)")
    parser.add_argument("images", nargs="*", help="Images to analyze (default: environment, model)")
    
    args = parser.parse_args()
    
    # Use config_manager for registry and repository
    registry_url = config_manager.get_registry_url()
    repository = config_manager.get_repository()
    max_workers = args.max_workers or config_manager.get_max_workers()
    
    # Parse ObjectIDs (typed) from file if provided
    object_ids_map = None
    if args.file:
        object_ids_map = read_typed_object_ids_from_file(args.file)
        env_ids = set(object_ids_map.get('environment', [])) if object_ids_map else set()
        env_ids.update(object_ids_map.get('environment_revision', []))
        model_ids = set(object_ids_map.get('model', [])) if object_ids_map else set()
        model_ids.update(object_ids_map.get('model_version', []))
        # Store back into a map keyed by image name
        object_ids_map = {
            'environment': sorted(env_ids),
            'model': sorted(model_ids),
        }
        if not any(object_ids_map.values()):
            logger.error(f"No valid ObjectIDs found in file '{args.file}' (prefixes required: environment:, environmentRevision:, model:, modelVersion:)")
            sys.exit(1)
        logger.info(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids_map}")
    
    # Get a list of images from the command line arguments or use default images
    if args.images:
        images = args.images
    else:
        logger.info("No images provided for registry scanning, scanning default Domino images...")
        images = ["environment", "model"]
    
    logger.info("=" * 60)
    logger.info("   Container Registry Scanning")
    logger.info("=" * 60)
    logger.info(f"Registry: {registry_url}")
    logger.info(f"Repository: {repository}")
    logger.info(f"Images: {', '.join(images)}")
    logger.info(f"Max Workers: {max_workers}")
    if object_ids_map:
        logger.info(f"Filtering by ObjectIDs from file: {args.file}")
    logger.info("=" * 60)
    
    # Create analyzer
    analyzer = ImageAnalyzer(registry_url, repository)
    
    # Analyze each image type
    success_count = 0
    for image in images:
        # Pick typed IDs if provided
        per_image_oids = None
        if object_ids_map is not None:
            per_image_oids = object_ids_map.get(image, [])
        
        logger.info(f"\nAnalyzing image type: {image}")
        if analyzer.analyze_image(image, per_image_oids, max_workers=max_workers):
            success_count += 1
        logger.info("")
    
    if success_count == 0:
        logger.error("No image data found. Check your ObjectID filters or registry access.")
        sys.exit(1)
    
    # Generate and save reports
    logger.info("\n" + "=" * 60)
    logger.info("   Generating Reports")
    logger.info("=" * 60)
    
    analyzer.save_reports()
    
    # Print summary
    summary = analyzer.generate_summary_stats()
    logger.info("\n" + "=" * 60)
    logger.info("   Analysis Summary")
    logger.info("=" * 60)
    logger.info(f"Total Images: {summary['total_images']}")
    logger.info(f"Total Layers: {summary['total_layers']}")
    logger.info(f"Total Size: {summary['total_size_gb']} GB")
    logger.info(f"Single-Use Layers: {summary['single_use_layers']} ({summary['single_use_size_gb']} GB)")
    logger.info(f"Shared Layers: {summary['shared_layers']} ({summary['shared_size_gb']} GB)")
    logger.info(f"Average Layers per Image: {summary['avg_layers_per_image']}")
    logger.info(f"Average Reference Count: {summary['avg_ref_count']}")
    logger.info("=" * 60)
    
    logger.info("\n✅ Analysis complete!")


if __name__ == "__main__":
    main()