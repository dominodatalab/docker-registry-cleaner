#!/usr/bin/env python3
"""
Docker Image Size Report Generator

This script generates a report of the largest images in the Docker registry,
showing both the total size of all layers for each image and how much space
would be freed if that image were deleted (accounting for shared layers).

Usage examples:
  # Generate report (auto-generates image analysis if missing)
  python image_size_report.py
  
  # Force regeneration of image analysis before generating report
  python image_size_report.py --generate-reports
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.image_data_analysis import ImageAnalyzer
from utils.image_metadata import build_model_tag_to_metadata_mapping, build_environment_tag_to_metadata_mapping
from utils.logging_utils import get_logger, setup_logging
from utils.object_id_utils import normalize_object_id
from utils.report_utils import save_json, ensure_image_analysis_reports, sizeof_fmt

logger = get_logger(__name__)


def build_image_metadata_mapping(analyzer: ImageAnalyzer) -> Dict[str, Dict]:
    """Build a mapping from image tags to their metadata (name and owner info).
    
    For model versions: gets model name from models collection and owner from metadata.createdBy
    For environment revisions: gets environment name from environments_v2 and owner from metadata.authorId
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
    
    Returns:
        Dict mapping tag -> {'image_name': str, 'user_name': str, 'owner_login_id': str}
    """
    tag_to_metadata = {}
    
    # Separate model and environment tags
    model_tags = set()
    environment_tags = set()
    
    for image_id, image_data in analyzer.images.items():
        tag = image_data['tag']
        image_type = image_id.split(':')[0] if ':' in image_id else 'unknown'
        if image_type == 'model':
            model_tags.add(tag)
        elif image_type == 'environment':
            environment_tags.add(tag)
    
    # Use shared utility functions
    if model_tags:
        model_metadata = build_model_tag_to_metadata_mapping(
            model_tags,
            include_model_name=True,
            include_owner_info=True
        )
        tag_to_metadata.update(model_metadata)
    
    if environment_tags:
        env_metadata = build_environment_tag_to_metadata_mapping(
            environment_tags,
            include_env_name=True,
            include_owner_info=True
        )
        tag_to_metadata.update(env_metadata)
    
    return tag_to_metadata


def generate_image_size_report(
    analyzer: ImageAnalyzer,
    image_types: List[str] = None
) -> Dict:
    """Generate a report of image sizes sorted by total size.
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
        image_types: List of image types to include (default: all)
    
    Returns:
        Dict with report data including sorted list of images
    """
    if image_types is None:
        image_types = ['environment', 'model']
    
    report_data = {
        'summary': {
            'total_images': 0,
            'total_size_bytes': 0,
            'total_size_gb': 0.0,
            'total_freed_if_all_deleted_bytes': 0,
            'total_freed_if_all_deleted_gb': 0.0,
            'image_types': image_types,
            'generated_at': datetime.now().isoformat()
        },
        'images': []
    }
    
    # Build metadata mapping (image names and owners)
    logger.info("Looking up image names and owners from MongoDB...")
    tag_to_metadata = build_image_metadata_mapping(analyzer)
    
    # Collect all images
    images_list = []
    total_size = 0
    all_image_ids = []
    
    for image_id, image_data in analyzer.images.items():
        # Extract image type from image_id (format: "image_type:tag")
        image_type = image_id.split(':')[0] if ':' in image_id else 'unknown'
        
        if image_type not in image_types:
            continue
        
        tag = image_data['tag']
        
        # Get metadata for this tag
        metadata = tag_to_metadata.get(tag, {})
        image_name = metadata.get('image_name', 'Unknown')
        user_name = metadata.get('user_name', 'Unknown')
        owner_login_id = metadata.get('owner_login_id', '')
        
        # Calculate total size (sum of all layers)
        total_size_bytes = analyzer.get_image_total_size(image_id)
        
        # Calculate space freed if this image is deleted
        freed_space_bytes = analyzer.freed_space_if_deleted([image_id])
        
        images_list.append({
            'image_id': image_id,
            'image_type': image_type,
            'tag': tag,
            'repository': image_data['repository'],
            'digest': image_data.get('digest', ''),
            'image_name': image_name,
            'user_name': user_name,
            'owner_login_id': owner_login_id,
            'total_size_bytes': total_size_bytes,
            'total_size_gb': round(total_size_bytes / (1024**3), 2),
            'freed_space_bytes': freed_space_bytes,
            'freed_space_gb': round(freed_space_bytes / (1024**3), 2),
            'shared_layers_size_bytes': total_size_bytes - freed_space_bytes,
            'shared_layers_size_gb': round((total_size_bytes - freed_space_bytes) / (1024**3), 2)
        })
        
        total_size += total_size_bytes
        all_image_ids.append(image_id)
    
    # Sort by total size (descending)
    images_list.sort(key=lambda x: x['total_size_bytes'], reverse=True)
    
    # Calculate total freed space if all images were deleted
    # This is different from sum of individual freed spaces because layers
    # shared across multiple images would only be freed once
    total_freed_if_all_deleted = analyzer.freed_space_if_deleted(all_image_ids)
    
    # Update summary
    report_data['summary'].update({
        'total_images': len(images_list),
        'total_size_bytes': total_size,
        'total_size_gb': round(total_size / (1024**3), 2),
        'total_freed_if_all_deleted_bytes': total_freed_if_all_deleted,
        'total_freed_if_all_deleted_gb': round(total_freed_if_all_deleted / (1024**3), 2)
    })
    
    report_data['images'] = images_list
    
    return report_data


def print_report_summary(report_data: Dict) -> None:
    """Print a human-readable summary of the report"""
    summary = report_data['summary']
    images = report_data['images']
    
    logger.info("\n" + "=" * 80)
    logger.info("   Docker Image Size Report Summary")
    logger.info("=" * 80)
    logger.info(f"Total Images: {summary['total_images']}")
    logger.info(f"Total Size: {sizeof_fmt(summary['total_size_bytes'])} ({summary['total_size_gb']} GB)")
    logger.info(f"Total Space Freed (if all deleted): {sizeof_fmt(summary['total_freed_if_all_deleted_bytes'])} ({summary['total_freed_if_all_deleted_gb']} GB)")
    logger.info("=" * 80)
    
    # Print top 20 largest images
    logger.info("\nTop 20 Largest Images (by total size):")
    logger.info("-" * 160)
    logger.info(f"{'Rank':<6} {'Image Type':<15} {'Image Name':<30} {'User Name':<30} {'Login ID':<20} {'Tag':<30} {'Total Size':<15} {'Freed if Deleted':<20}")
    logger.info("-" * 160)
    
    for idx, img in enumerate(images[:20], 1):
        tag_display = img['tag'][:27] + "..." if len(img['tag']) > 30 else img['tag']
        image_name_display = img.get('image_name', 'Unknown')[:27] + "..." if len(img.get('image_name', 'Unknown')) > 30 else img.get('image_name', 'Unknown')
        user_name_display = img.get('user_name', 'Unknown')[:27] + "..." if len(img.get('user_name', 'Unknown')) > 30 else img.get('user_name', 'Unknown')
        login_id_display = img.get('owner_login_id', '')[:17] + "..." if len(img.get('owner_login_id', '')) > 20 else img.get('owner_login_id', '')
        logger.info(
            f"{idx:<6} {img['image_type']:<15} {image_name_display:<30} {user_name_display:<30} {login_id_display:<20} {tag_display:<30} "
            f"{sizeof_fmt(img['total_size_bytes']):<15} {sizeof_fmt(img['freed_space_bytes']):<20}"
        )
    
    if len(images) > 20:
        logger.info(f"\n... and {len(images) - 20} more images")
    
    logger.info("\n" + "=" * 80)
    logger.info("Note: 'Freed if Deleted' accounts for shared layers - only unique")
    logger.info("      layers are counted. Total size includes all layers (shared + unique).")
    logger.info("=" * 80)


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Generate Docker image size report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate report (auto-generates image analysis if missing)
  python image_size_report.py
  
  # Force regeneration of image analysis before generating report
  python image_size_report.py --generate-reports
  
  # Specify output file
  python image_size_report.py --output custom-report.json
        """
    )
    
    parser.add_argument(
        '--generate-reports',
        action='store_true',
        help='Force regeneration of image analysis reports before generating size report'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path for the report (default: image-size-report.json in reports directory)'
    )
    
    parser.add_argument(
        '--image-types',
        nargs='+',
        default=['environment', 'model'],
        help='Image types to include in report (default: environment model)'
    )
    
    parser.add_argument(
        '--max-workers',
        type=int,
        help='Maximum number of parallel workers for image analysis (default: from config)'
    )
    
    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
    try:
        logger.info("=" * 80)
        logger.info("   Docker Image Size Report Generator")
        logger.info("=" * 80)
        
        # Check if image analysis reports need to be generated
        if args.generate_reports:
            logger.info("Forcing regeneration of image analysis reports...")
            ensure_image_analysis_reports(max_age_hours=0)  # Force regeneration
        else:
            # Check if reports exist and are fresh
            from utils.report_utils import is_report_fresh
            tag_sums_exists = is_report_fresh('tag-sums.json')
            layers_and_sizes_exists = is_report_fresh('layers-and-sizes.json')
            reports_exist = tag_sums_exists and layers_and_sizes_exists
            
            if not reports_exist:
                logger.info("Image analysis reports not found or stale. Generating them now...")
                ensure_image_analysis_reports()
        
        # Get configuration
        registry_url = config_manager.get_registry_url()
        repository = config_manager.get_repository()
        
        logger.info(f"Registry: {registry_url}")
        logger.info(f"Repository: {repository}")
        logger.info(f"Image Types: {', '.join(args.image_types)}")
        logger.info("=" * 80)
        
        # Create analyzer
        logger.info("\nAnalyzing Docker images...")
        analyzer = ImageAnalyzer(registry_url, repository)
        
        # Analyze each image type
        success_count = 0
        for image_type in args.image_types:
            logger.info(f"\nAnalyzing {image_type} images...")
            if analyzer.analyze_image(image_type, object_ids=None, max_workers=args.max_workers):
                success_count += 1
        
        if success_count == 0:
            logger.error("No image data found. Check your registry access.")
            sys.exit(1)
        
        # Generate report
        logger.info("\n" + "=" * 80)
        logger.info("   Generating Image Size Report")
        logger.info("=" * 80)
        
        report_data = generate_image_size_report(analyzer, args.image_types)
        
        # Save report
        if args.output:
            output_path = args.output
        else:
            # Use default path from config or reports directory
            reports_dir = Path(config_manager.get_output_dir())
            output_path = str(reports_dir / "image-size-report.json")
        
        saved_path = save_json(output_path, report_data, timestamp=True)
        logger.info(f"\nReport saved to: {saved_path}")
        
        # Print summary
        print_report_summary(report_data)
        
        logger.info("\n✅ Image size report generation completed successfully!")
        
    except FileNotFoundError as e:
        logger.error(f"\n❌ Missing required file: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Report generation failed: {e}")
        from utils.logging_utils import log_exception
        log_exception(logger, "Error in main", exc_info=e)
        sys.exit(1)


if __name__ == "__main__":
    main()
