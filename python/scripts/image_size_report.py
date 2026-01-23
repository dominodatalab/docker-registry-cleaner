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
from utils.logging_utils import get_logger, setup_logging
from utils.report_utils import save_json, ensure_image_analysis_reports

logger = get_logger(__name__)


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    """Format bytes into human-readable size"""
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


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
    
    # Collect all images
    images_list = []
    total_size = 0
    all_image_ids = []
    
    for image_id, image_data in analyzer.images.items():
        # Extract image type from image_id (format: "image_type:tag")
        image_type = image_id.split(':')[0] if ':' in image_id else 'unknown'
        
        if image_type not in image_types:
            continue
        
        # Calculate total size (sum of all layers)
        total_size_bytes = analyzer.get_image_total_size(image_id)
        
        # Calculate space freed if this image is deleted
        freed_space_bytes = analyzer.freed_space_if_deleted([image_id])
        
        images_list.append({
            'image_id': image_id,
            'image_type': image_type,
            'tag': image_data['tag'],
            'repository': image_data['repository'],
            'digest': image_data.get('digest', ''),
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
    logger.info("-" * 80)
    logger.info(f"{'Rank':<6} {'Image Type':<15} {'Tag':<40} {'Total Size':<15} {'Freed if Deleted':<20}")
    logger.info("-" * 80)
    
    for idx, img in enumerate(images[:20], 1):
        tag_display = img['tag'][:37] + "..." if len(img['tag']) > 40 else img['tag']
        logger.info(
            f"{idx:<6} {img['image_type']:<15} {tag_display:<40} "
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
        max_workers = args.max_workers or config_manager.get_max_workers()
        
        logger.info(f"Registry: {registry_url}")
        logger.info(f"Repository: {repository}")
        logger.info(f"Image Types: {', '.join(args.image_types)}")
        logger.info(f"Max Workers: {max_workers}")
        logger.info("=" * 80)
        
        # Create analyzer
        logger.info("\nAnalyzing Docker images...")
        analyzer = ImageAnalyzer(registry_url, repository)
        
        # Analyze each image type
        success_count = 0
        for image_type in args.image_types:
            logger.info(f"\nAnalyzing {image_type} images...")
            if analyzer.analyze_image(image_type, object_ids=None, max_workers=max_workers):
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
