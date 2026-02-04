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
import sys
from pathlib import Path
from typing import Dict

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.logging_utils import get_logger, setup_logging
from utils.report_utils import ensure_all_reports, sizeof_fmt

logger = get_logger(__name__)


def generate_required_reports() -> None:
    """Generate required metadata reports by calling extract_metadata and image_data_analysis"""
    logger.info("Generating required metadata reports...")
    ensure_all_reports()


def load_metadata_files() -> tuple:
    """Load metadata files.

    Supports both timestamped and non-timestamped report files.
    If exact file doesn't exist, finds the most recent timestamped version.
    """
    from utils.report_utils import get_latest_report, get_reports_dir

    # Load tag sums
    tag_sums_path = config_manager.get_tag_sums_path()
    tag_sums_file = Path(tag_sums_path)

    # If exact file doesn't exist, try to find latest timestamped version
    if not tag_sums_file.exists():
        reports_dir = get_reports_dir()
        stem = tag_sums_file.stem
        suffix = tag_sums_file.suffix
        pattern = f"{stem}-*-*-*-*-*-*{suffix}"
        latest = get_latest_report(pattern, reports_dir)
        if latest:
            tag_sums_file = latest
            logger.info(f"Using latest timestamped report: {tag_sums_file.name}")

    if not tag_sums_file.exists():
        logger.error(f"Tag sums file not found: {tag_sums_path}")
        raise FileNotFoundError(f"Tag sums file not found: {tag_sums_path}")

    with open(tag_sums_file) as f:
        tag_data = json.load(f)
    logger.info(f"Loaded {len(tag_data)} tags from tag sums")

    # Load MongoDB usage reports from consolidated file
    from utils.image_usage import ImageUsageService

    service = ImageUsageService()
    reports = service.load_usage_reports()

    # Convert to JSON strings for compatibility with existing analyze_tag_usage function
    workspace_data = json.dumps(reports.get("workspaces", []))
    model_data = json.dumps(reports.get("models", []))

    logger.info(f"Loaded {len(reports.get('workspaces', []))} workspace environment records")
    logger.info(f"Loaded {len(reports.get('models', []))} model environment records")

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
        for tag, _size, human_size in sorted(unused_tags, key=lambda x: x[1], reverse=True):
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
        """,
    )

    parser.add_argument(
        "--generate-reports",
        action="store_true",
        help="Generate required metadata reports (extract_metadata) before analysis",
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
        # Use is_report_fresh which handles timestamped reports
        from utils.report_utils import is_report_fresh

        tag_sums_exists = is_report_fresh("tag-sums.json")
        mongodb_usage_exists = is_report_fresh("mongodb_usage_report.json")
        reports_exist = tag_sums_exists and mongodb_usage_exists

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
        from utils.logging_utils import log_exception

        log_exception(logger, "Error in main", exc_info=e)
        import sys

        sys.exit(1)


if __name__ == "__main__":
    main()
