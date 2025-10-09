#!/usr/bin/env python3
"""
Docker Image and Layer Analysis Tool

This script analyzes Docker registry images and extracts layer information,
using pandas DataFrames for efficient data management and analysis.

Data Model:
- Layers DataFrame: layer_id, size_bytes, ref_count
- Images DataFrame: image_id, repository, tag, digest
- Image-to-Layer Mapping: image_id, layer_id, order_index
"""

import argparse
import pandas as pd
import sys

from typing import List, Optional, Dict

from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger
from object_id_utils import read_typed_object_ids_from_file
from report_utils import save_json

logger = get_logger(__name__)


class ImageAnalyzer:
    """Analyzes Docker images and their layers using pandas DataFrames"""
    
    def __init__(self, registry_url: str, repository: str):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(config_manager, use_pod=False)
        
        # Initialize DataFrames
        self.layers_df = pd.DataFrame(columns=["layer_id", "size_bytes", "ref_count"])
        self.images_df = pd.DataFrame(columns=["image_id", "repository", "tag", "digest"])
        self.image_layers_df = pd.DataFrame(columns=["image_id", "layer_id", "order_index"])
        
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
    
    def analyze_image(self, image_type: str, object_ids: Optional[List[str]] = None) -> bool:
        """Analyze a single image type (e.g., 'environment', 'model')
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get tags using standardized client
            tags = self.skopeo_client.list_tags(f"{self.repository}/{image_type}")
            
            # Filter tags by ObjectIDs if provided
            if object_ids:
                original_count = len(tags)
                tags = self.filter_tags_by_object_ids(tags, object_ids)
                filtered_count = len(tags)
                self.logger.info(f"Filtered tags for {image_type}: {filtered_count}/{original_count} tags match the provided ObjectIDs")
                
                if filtered_count == 0:
                    self.logger.warning(f"No tags found matching the provided ObjectIDs for image: {image_type}")
                    return False
            
            self.logger.info(f"Analyzing {len(tags)} tags for {image_type}...")
            
            for tag in tags:
                self.logger.info(f"  Processing tag: {tag}...")
                
                # Inspect image using standardized client
                image_info = self.skopeo_client.inspect_image(f"{self.repository}/{image_type}", tag)
                if not image_info:
                    self.logger.error(f"Failed to inspect image {image_type}:{tag}")
                    continue
                
                # Extract image metadata
                digest = image_info.get('Digest', '')
                image_id = f"{image_type}:{tag}"
                
                # Add image to images DataFrame
                new_image = pd.DataFrame([{
                    'image_id': image_id,
                    'repository': f"{self.repository}/{image_type}",
                    'tag': tag,
                    'digest': digest
                }])
                self.images_df = pd.concat([self.images_df, new_image], ignore_index=True)
                
                # Extract layers
                layers_data = image_info.get('LayersData', [])
                
                for order_index, layer in enumerate(layers_data):
                    layer_id = layer['Digest']
                    layer_size = layer['Size']
                    
                    # Add or update layer in layers DataFrame
                    if layer_id in self.layers_df['layer_id'].values:
                        # Layer exists, increment ref_count
                        self.layers_df.loc[self.layers_df['layer_id'] == layer_id, 'ref_count'] += 1
                    else:
                        # New layer
                        new_layer = pd.DataFrame([{
                            'layer_id': layer_id,
                            'size_bytes': layer_size,
                            'ref_count': 1
                        }])
                        self.layers_df = pd.concat([self.layers_df, new_layer], ignore_index=True)
                    
                    # Add image-to-layer mapping
                    new_mapping = pd.DataFrame([{
                        'image_id': image_id,
                        'layer_id': layer_id,
                        'order_index': order_index
                    }])
                    self.image_layers_df = pd.concat([self.image_layers_df, new_mapping], ignore_index=True)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to retrieve information for image: {image_type}")
            self.logger.error(f"Error: {e}")
            return False
    
    def freed_space_if_deleted(self, image_ids: List[str]) -> int:
        """Calculate space that would be freed by deleting one or more images.
        
        Args:
            image_ids: List of image_ids to simulate deletion
        
        Returns:
            Total bytes that would be freed
        """
        # Count how many of the to-be-deleted images use each layer
        layers_to_delete = self.image_layers_df[
            self.image_layers_df['image_id'].isin(image_ids)
        ]['layer_id'].value_counts()
        
        total_freed = 0
        for layer_id, delete_count in layers_to_delete.items():
            # Get current ref_count for this layer
            current_ref = self.layers_df[self.layers_df['layer_id'] == layer_id]['ref_count'].values[0]
            
            # If this layer would have 0 references after deletion, it will be freed
            if current_ref == delete_count:
                layer_size = self.layers_df[self.layers_df['layer_id'] == layer_id]['size_bytes'].values[0]
                total_freed += layer_size
        
        return int(total_freed)
    
    def get_unused_images(self, used_tags: List[str]) -> pd.DataFrame:
        """Get images that are not in the used_tags list.
        
        Args:
            used_tags: List of tags that are currently in use
        
        Returns:
            DataFrame of unused images
        """
        return self.images_df[~self.images_df['tag'].isin(used_tags)]
    
    def generate_summary_stats(self) -> Dict:
        """Generate summary statistics about the analyzed images and layers"""
        total_images = len(self.images_df)
        total_layers = len(self.layers_df)
        total_size = int(self.layers_df['size_bytes'].sum())
        
        # Layers used by only one image
        single_use_layers = self.layers_df[self.layers_df['ref_count'] == 1]
        single_use_size = int(single_use_layers['size_bytes'].sum())
        
        # Shared layers (used by multiple images)
        shared_layers = self.layers_df[self.layers_df['ref_count'] > 1]
        shared_size = int(shared_layers['size_bytes'].sum())
        
        return {
            'total_images': total_images,
            'total_layers': total_layers,
            'total_size_bytes': total_size,
            'total_size_gb': round(total_size / (1024**3), 2),
            'single_use_layers': len(single_use_layers),
            'single_use_size_bytes': single_use_size,
            'single_use_size_gb': round(single_use_size / (1024**3), 2),
            'shared_layers': len(shared_layers),
            'shared_size_bytes': shared_size,
            'shared_size_gb': round(shared_size / (1024**3), 2),
            'avg_layers_per_image': round(len(self.image_layers_df) / total_images, 2) if total_images > 0 else 0,
            'avg_ref_count': round(self.layers_df['ref_count'].mean(), 2) if total_layers > 0 else 0
        }
    
    def get_images_by_tag_prefix(self, prefix: str) -> pd.DataFrame:
        """Get all images whose tags start with the given prefix (e.g., ObjectID)"""
        return self.images_df[self.images_df['tag'].str.startswith(prefix)]
    
    def export_to_legacy_format(self) -> Dict:
        """Export data in the legacy format for backward compatibility.
        
        Returns a dict mapping layer_id to {'size': int, 'tags': [str], 'environments': [str]}
        """
        legacy_data = {}
        
        for _, layer in self.layers_df.iterrows():
            layer_id = layer['layer_id']
            
            # Get all image-layer mappings for this layer
            mappings = self.image_layers_df[self.image_layers_df['layer_id'] == layer_id]
            
            # Get the tags for these images
            image_ids = mappings['image_id'].tolist()
            tags = []
            environments = []
            
            for image_id in image_ids:
                image_row = self.images_df[self.images_df['image_id'] == image_id]
                if not image_row.empty:
                    tag = image_row.iloc[0]['tag']
                    tags.append(tag)
                    # Extract environment ID (first part before '-')
                    env_id = tag.split('-')[0] if '-' in tag else tag
                    environments.append(env_id)
            
            legacy_data[layer_id] = {
                'size': int(layer['size_bytes']),
                'tags': tags,
                'environments': environments
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
        legacy_data = self.export_to_legacy_format()
        save_json(final_output_file, legacy_data)
        self.logger.info(f"Image analysis saved to: {final_output_file}")
        
        # Tags per layer
        tags_per_layer = {
            layer_id: data['ref_count'] 
            for layer_id, data in self.layers_df.set_index('layer_id').to_dict('index').items()
        }
        save_json(tags_per_layer_output_file, tags_per_layer)
        self.logger.info(f"Tags per layer count saved to: {tags_per_layer_output_file}")
        
        # Layers and sizes
        layers_and_sizes = {
            layer_id: int(data['size_bytes'])
            for layer_id, data in self.layers_df.set_index('layer_id').to_dict('index').items()
        }
        save_json(layers_and_sizes_output_file, layers_and_sizes)
        self.logger.info(f"Layers and sizes saved to: {layers_and_sizes_output_file}")
        
        # Filtered layers (ref_count == 1)
        single_use_layers = self.layers_df[self.layers_df['ref_count'] == 1]
        filtered_legacy = {}
        for _, layer in single_use_layers.iterrows():
            layer_id = layer['layer_id']
            if layer_id in legacy_data:
                filtered_legacy[layer_id] = legacy_data[layer_id]
        save_json(filtered_layers_output_file, filtered_legacy)
        self.logger.info(f"Filtered layers saved to: {filtered_layers_output_file}")
        
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
        save_json(tag_sums_output_file, tag_sums)
        self.logger.info(f"Tag sums saved to: {tag_sums_output_file}")
        
        # Images report (comprehensive)
        images_report = {
            'summary': self.generate_summary_stats(),
            'layers': legacy_data
        }
        save_json(f"{images_report_output_file}.json", images_report)
        
        # Also save a text summary
        summary = images_report['summary']
        with open(f"{images_report_output_file}.txt", 'w') as f:
            f.write("=" * 60 + "\n")
            f.write("Docker Image Analysis Summary\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Total Images: {summary['total_images']}\n")
            f.write(f"Total Layers: {summary['total_layers']}\n")
            f.write(f"Total Size: {summary['total_size_gb']} GB ({summary['total_size_bytes']} bytes)\n\n")
            f.write(f"Single-Use Layers: {summary['single_use_layers']}\n")
            f.write(f"Single-Use Size: {summary['single_use_size_gb']} GB ({summary['single_use_size_bytes']} bytes)\n\n")
            f.write(f"Shared Layers: {summary['shared_layers']}\n")
            f.write(f"Shared Size: {summary['shared_size_gb']} GB ({summary['shared_size_bytes']} bytes)\n\n")
            f.write(f"Average Layers per Image: {summary['avg_layers_per_image']}\n")
            f.write(f"Average Reference Count: {summary['avg_ref_count']}\n")
        
        self.logger.info(f"Images report saved to: {images_report_output_file}.txt and .json")


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
  
  # Override registry and repository
  python image_data_analysis.py --registry-url docker-registry:5000 --repository dominodatalab
  
  # Filter by ObjectIDs from file
  python image_data_analysis.py --file environments environment model
        """
    )
    
    parser.add_argument("--registry-url", help=f"Container registry URL (default: from config)")
    parser.add_argument("--repository", help=f"Container repository name (default: from config)")
    parser.add_argument("--file", help="File containing ObjectIDs (first column) to filter images")
    parser.add_argument("images", nargs="*", help="Images to analyze (default: environment, model)")
    
    args = parser.parse_args()
    
    # Use config_manager defaults if not provided
    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    
    # Parse ObjectIDs (typed) from file if provided
    object_ids_map = None
    if args.file:
        object_ids_map = read_typed_object_ids_from_file(args.file)
        # Build per-image lists, including 'any'
        any_ids = set(object_ids_map.get('any', []))
        env_ids = list(any_ids.union(object_ids_map.get('environment', []))) if object_ids_map else None
        model_ids = list(any_ids.union(object_ids_map.get('model', []))) if object_ids_map else None
        # Store back into a map keyed by image name
        object_ids_map = {
            'environment': env_ids or [],
            'model': model_ids or [],
        }
        if not any(object_ids_map.values()):
            logger.error(f"No valid ObjectIDs found in file '{args.file}'")
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
        if analyzer.analyze_image(image, per_image_oids):
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