#!/usr/bin/env python3
"""
Docker Registry Tag Usage Report Generator

This script analyzes Docker tag usage by comparing tags in the registry against
workspace and model environment usage to identify unused tags and potential space savings.

Workflow:
- Auto-generate required reports if they don't exist (or use --generate-reports to force)
  - Extract model and workspace environment usage from MongoDB
  - Analyze Docker images and generate tag sums
- Load tag sums data
- Compare tags against workspace and model usage
- Report unused tags and potential space savings

Usage examples:
  # Generate report (auto-generates metadata if missing)
  python reports.py
  
  # Force regeneration of metadata reports
  python reports.py --generate-reports
"""

import argparse
import json

from pathlib import Path
from typing import Dict

import extract_metadata
from config_manager import config_manager
from image_data_analysis import ImageAnalyzer
from logging_utils import setup_logging, get_logger

logger = get_logger(__name__)


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    """Format bytes into human-readable size"""
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


def generate_required_reports() -> None:
    """Generate required metadata reports by calling extract_metadata and image_data_analysis"""
    logger.info("Generating required metadata reports...")
    
    # Generate metadata from MongoDB
    logger.info("Extracting metadata from MongoDB...")
    try:
        extract_metadata.run("all")  # Run both model and workspace queries
        logger.info("✓ Metadata extraction completed")
    except Exception as e:
        logger.error(f"Failed to extract metadata: {e}")
        raise
    
    # Generate image analysis and tag sums
    logger.info("Analyzing Docker images and generating tag sums...")
    try:
        registry_url = config_manager.get_registry_url()
        repository = config_manager.get_repository()
        analyzer = ImageAnalyzer(registry_url, repository)
        
        # Analyze both environment and model images
        for image_type in ['environment', 'model']:
            logger.info(f"Analyzing {image_type} images...")
            success = analyzer.analyze_image(image_type)
            if not success:
                logger.warning(f"Failed to analyze {image_type} images")
        
        # Save reports (generates tag-sums.json and other reports)
        analyzer.save_reports()
        logger.info("✓ Image analysis completed")
    except Exception as e:
        logger.error(f"Failed to analyze images: {e}")
        raise


def load_metadata_files() -> tuple:
    """Load metadata files"""
    output_dir = config_manager.get_output_dir()
    
    # Load tag sums
    tag_sums_path = config_manager.get_tag_sums_path()
    if not Path(tag_sums_path).exists():
        logger.error(f"Tag sums file not found: {tag_sums_path}")
        raise FileNotFoundError(f"Tag sums file not found: {tag_sums_path}")
    
    with open(tag_sums_path) as f:
        tag_data = json.load(f)
    logger.info(f"Loaded {len(tag_data)} tags from tag sums")
    
    # Load workspace environment usage
    workspace_usage_path = Path(output_dir) / "workspace_env_usage_output.json"
    if not workspace_usage_path.exists():
        logger.error(f"Workspace usage file not found: {workspace_usage_path}")
        raise FileNotFoundError(f"Workspace usage file not found: {workspace_usage_path}")
    
    with open(workspace_usage_path, 'r') as f:
        workspace_data = f.read()
    logger.info("Loaded workspace environment usage data")
    
    # Load model environment usage
    model_usage_path = Path(output_dir) / "model_env_usage_output.json"
    if not model_usage_path.exists():
        logger.error(f"Model usage file not found: {model_usage_path}")
        raise FileNotFoundError(f"Model usage file not found: {model_usage_path}")
    
    with open(model_usage_path, 'r') as f:
        model_data = f.read()
    logger.info("Loaded model environment usage data")
    
    return tag_data, workspace_data, model_data


def analyze_tag_usage(tag_data: Dict, workspace_data: str, model_data: str) -> None:
    """Analyze tag usage and report findings"""
    total_size = 0
    unused_tags = []
    workspace_tags = []
    model_tags = []
    
    logger.info("\nAnalyzing tag usage...")
    logger.info("=" * 60)
    
    for key in tag_data.keys():
        if key in workspace_data:
            workspace_tags.append(key)
            logger.info(f"✓ {key} is in use in a workspace")
        elif key in model_data:
            model_tags.append(key)
            logger.info(f"✓ {key} is in use in a model")
        else:
            size = tag_data[key]["size"]
            human_readable_size = sizeof_fmt(size)
            total_size += size
            unused_tags.append((key, size, human_readable_size))
            logger.info(f"✗ {key} - {human_readable_size} (unused)")
    
    # Print summary
    logger.info("\n" + "=" * 60)
    logger.info("   TAG USAGE SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total tags analyzed: {len(tag_data)}")
    logger.info(f"Tags in use by workspaces: {len(workspace_tags)}")
    logger.info(f"Tags in use by models: {len(model_tags)}")
    logger.info(f"Unused tags: {len(unused_tags)}")
    
    human_readable_total_size = sizeof_fmt(total_size)
    logger.info("\n" + "=" * 60)
    logger.info(f"💾 You could free up {human_readable_total_size} by deleting unused Docker tags.")
    logger.info("=" * 60)
    
    if unused_tags:
        logger.info("\nUnused tags details:")
        for tag, size, human_size in sorted(unused_tags, key=lambda x: x[1], reverse=True):
            logger.info(f"  {tag}: {human_size}")


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Generate Docker registry tag usage reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate report (auto-generates metadata if missing)
  python reports.py
  
  # Force regeneration of metadata reports
  python reports.py --generate-reports
        """
    )
    
    parser.add_argument(
        '--generate-reports',
        action='store_true',
        help='Generate required metadata reports (extract_metadata) before analysis'
    )
    
    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
    try:
        logger.info("=" * 60)
        logger.info("   Docker Registry Tag Usage Report")
        logger.info("=" * 60)
        
        # Check if metadata reports need to be generated
        output_dir = config_manager.get_output_dir()
        tag_sums_path = Path(config_manager.get_tag_sums_path())
        reports_exist = all([
            (Path(output_dir) / "model_env_usage_output.json").exists(),
            (Path(output_dir) / "workspace_env_usage_output.json").exists(),
            tag_sums_path.exists()
        ])
        
        # Generate reports if requested or if they don't exist
        if args.generate_reports or not reports_exist:
            if not reports_exist:
                logger.info("Required metadata reports not found. Generating them now...")
            generate_required_reports()
        
        # Load metadata files
        tag_data, workspace_data, model_data = load_metadata_files()
        
        # Analyze and report
        analyze_tag_usage(tag_data, workspace_data, model_data)
        
        logger.info("\n✅ Report generation completed successfully!")
        
    except FileNotFoundError as e:
        logger.error(f"\n❌ Missing required file: {e}")
        import sys
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Report generation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        import sys
        sys.exit(1)


if __name__ == "__main__":
    main()
