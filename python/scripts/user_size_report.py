#!/usr/bin/env python3
"""
Docker Image Size Report by User

This script generates a report showing which users own the most Docker images
by total size, helping identify who is consuming the most registry space.

Usage examples:
  # Generate report (auto-generates image analysis and MongoDB reports if missing)
  python user_size_report.py
  
  # Force regeneration of all reports
  python user_size_report.py --generate-reports
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.image_data_analysis import ImageAnalyzer
from utils.image_usage import ImageUsageService
from utils.logging_utils import get_logger, setup_logging
from utils.report_utils import save_json, ensure_all_reports

logger = get_logger(__name__)


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    """Format bytes into human-readable size"""
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


def extract_owners_from_usage_info(
    tag: str,
    usage_info: Dict,
    mongodb_reports: Dict[str, List[Dict]],
    model_created_by_map: Dict[str, str] = None,
    tag_to_author_map: Dict[str, Tuple[str, str]] = None
) -> Set[Tuple[str, str]]:
    """Extract owner (user_id, user_name) pairs for a given tag from usage info.
    
    Args:
        tag: Docker image tag
        usage_info: Usage info dict for the tag
        mongodb_reports: Full MongoDB reports for additional lookups
        model_created_by_map: Mapping from model_id -> createdBy ObjectId
        tag_to_author_map: Mapping from Docker tag -> authorId from environment_revisions
    
    Returns:
        Set of (user_id, user_name) tuples
    """
    owners = set()
    
    # First, check if this is an environment tag and we have authorId from environment_revisions
    if tag_to_author_map is None:
        tag_to_author_map = {}
    
    author_info = tag_to_author_map.get(tag)
    if author_info:
        # This is an environment image with a known author
        author_id, author_name = author_info
        owners.add((author_id, author_name))
    
    # From runs: use project_owner_id and project_owner_name
    for run in usage_info.get('runs', []):
        owner_id = run.get('project_owner_id', '')
        owner_name = run.get('project_owner_name', '')
        if owner_id and owner_id != 'unknown':
            owners.add((str(owner_id), owner_name or 'Unknown'))
    
    # From workspaces: look up workspace owner from MongoDB reports
    # Workspace records in MongoDB have ownerId field
    for workspace in usage_info.get('workspaces', []):
        workspace_id = workspace.get('workspace_id', '')
        user_name = workspace.get('user_name', '')
        
        # Look up workspace owner_id from MongoDB reports
        if workspace_id and workspace_id != 'unknown':
            for ws_record in mongodb_reports.get('workspaces', []):
                ws_id = ws_record.get('workspace_id') or ws_record.get('_id', '')
                if str(ws_id) == str(workspace_id):
                    # Workspace records should have ownerId in the original data
                    # But the pipeline may not include it, so use user_name as fallback
                    owner_id = ws_record.get('ownerId', '')
                    if owner_id:
                        owners.add((str(owner_id), user_name or 'Unknown'))
                    elif user_name and user_name != 'unknown':
                        # Fallback: use workspace_id as identifier if we can't find ownerId
                        owners.add(('workspace:' + str(workspace_id), user_name))
                    break
    
    # From models: use metadata.createdBy (ObjectId) from MongoDB
    # Models have their owner in metadata.createdBy which references the users collection
    if model_created_by_map is None:
        model_created_by_map = {}
    
    for model in usage_info.get('models', []):
        model_owner = model.get('model_owner', '')  # Name from pipeline
        model_created_by = model.get('model_created_by', '')  # Name from pipeline
        model_id = model.get('model_id', '')
        
        if model_id:
            model_id_str = str(model_id)
            # Look up createdBy ObjectId from the pre-built mapping
            created_by_id = model_created_by_map.get(model_id_str)
            
            if created_by_id:
                # Use model_created_by name if available, otherwise model_owner, otherwise 'Unknown'
                owner_name = model_created_by or model_owner or 'Unknown'
                owners.add((created_by_id, owner_name))
            elif model_owner and model_owner != 'unknown':
                # Fallback: use model_owner name with model_id as identifier
                owners.add(('model:' + model_id_str, model_owner))
    
    # From projects: use owner_id
    for project in usage_info.get('projects', []):
        owner_id = project.get('ownerId', '')
        if owner_id and owner_id != 'unknown':
            # Look up owner name from projects
            owner_name = 'Unknown'
            for proj_record in mongodb_reports.get('projects', []):
                proj_id = proj_record.get('project_id') or proj_record.get('_id', '')
                if str(proj_id) == str(project.get('_id', '')):
                    # Projects have owner_id, but pipeline may not include owner name
                    # Use owner_id
                    owners.add((str(owner_id), owner_name))
                    break
    
    # From scheduler_jobs: need to look up project owner
    for job in usage_info.get('scheduler_jobs', []):
        project_id = job.get('projectId', '')
        if project_id and project_id != 'unknown':
            # Look up project owner from projects
            for proj_record in mongodb_reports.get('projects', []):
                proj_id = proj_record.get('project_id') or proj_record.get('_id', '')
                if str(proj_id) == str(project_id):
                    owner_id = proj_record.get('owner_id', '')
                    if owner_id:
                        owners.add((str(owner_id), 'Unknown'))
                    break
    
    return owners


def build_model_created_by_mapping(mongodb_reports: Dict[str, List[Dict]]) -> Dict[str, str]:
    """Build a mapping from model_id to createdBy ObjectId by querying MongoDB.
    
    Args:
        mongodb_reports: MongoDB usage reports
    
    Returns:
        Dict mapping model_id -> createdBy ObjectId (as string)
    """
    model_to_created_by = {}
    
    # Collect all unique model IDs from the reports
    model_ids = set()
    for model_record in mongodb_reports.get('models', []):
        model_id = model_record.get('model_id') or model_record.get('_id', '')
        if model_id:
            model_ids.add(str(model_id))
    
    if not model_ids:
        return model_to_created_by
    
    # Query MongoDB in batch to get metadata.createdBy for all models
    try:
        from utils.mongo_utils import get_mongo_client
        from utils.config_manager import config_manager
        from bson import ObjectId
        
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]
        
        # Build list of ObjectIds (filtering out invalid ones)
        object_ids = []
        id_to_str = {}
        for model_id_str in model_ids:
            try:
                obj_id = ObjectId(model_id_str)
                object_ids.append(obj_id)
                id_to_str[str(obj_id)] = model_id_str
            except (ValueError, TypeError):
                continue
        
        if object_ids:
            # Query all models at once
            models = db.models.find(
                {'_id': {'$in': object_ids}},
                {'_id': 1, 'metadata.createdBy': 1}
            )
            
            for model_doc in models:
                model_id_str = id_to_str.get(str(model_doc['_id']), str(model_doc['_id']))
                created_by = model_doc.get('metadata', {}).get('createdBy')
                if created_by:
                    model_to_created_by[model_id_str] = str(created_by)
        
        mongo_client.close()
    except Exception as e:
        logger.warning(f"Could not query MongoDB for model owners: {e}")
    
    return model_to_created_by


def build_tag_to_author_mapping(analyzer: ImageAnalyzer) -> Dict[str, Tuple[str, str]]:
    """Build a mapping from Docker tag to (authorId, authorName) by querying environment_revisions.
    
    Environment revisions store the author in metadata.authorId.
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
    
    Returns:
        Dict mapping tag -> (authorId ObjectId as string, authorName)
    """
    tag_to_author = {}
    
    # Collect all environment tags from the analyzer
    environment_tags = set()
    for image_id, image_data in analyzer.images.items():
        if image_id.startswith('environment:'):
            tag = image_data['tag']
            environment_tags.add(tag)
    
    if not environment_tags:
        return tag_to_author
    
    # Query MongoDB to get authorId for each tag
    try:
        from utils.mongo_utils import get_mongo_client
        from utils.config_manager import config_manager
        from bson import ObjectId
        
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]
        
        # Query environment_revisions for tags that match
        # Note: tags in environment_revisions are stored in metadata.dockerImageName.tag
        revisions = db.environment_revisions.find(
            {'metadata.dockerImageName.tag': {'$in': list(environment_tags)}},
            {'metadata.dockerImageName.tag': 1, 'metadata.authorId': 1}
        )
        
        # Collect author IDs to look up names
        author_ids = set()
        tag_to_author_id = {}
        
        for rev_doc in revisions:
            tag = rev_doc.get('metadata', {}).get('dockerImageName', {}).get('tag')
            author_id = rev_doc.get('metadata', {}).get('authorId')
            if tag and author_id:
                author_id_str = str(author_id)
                author_ids.add(author_id)
                tag_to_author_id[tag] = author_id_str
        
        # Look up user names for all author IDs
        author_id_to_name = {}
        if author_ids:
            users = db.users.find(
                {'_id': {'$in': list(author_ids)}},
                {'_id': 1, 'fullName': 1}
            )
            for user_doc in users:
                author_id_to_name[str(user_doc['_id'])] = user_doc.get('fullName', 'Unknown')
        
        # Build final mapping with names
        for tag, author_id_str in tag_to_author_id.items():
            author_name = author_id_to_name.get(author_id_str, 'Unknown')
            tag_to_author[tag] = (author_id_str, author_name)
        
        mongo_client.close()
    except Exception as e:
        logger.warning(f"Could not query MongoDB for environment revision authors: {e}")
    
    return tag_to_author


def build_tag_to_owners_mapping(
    analyzer: ImageAnalyzer,
    mongodb_reports: Dict[str, List[Dict]]
) -> Dict[str, Set[Tuple[str, str]]]:
    """Build a mapping from image tags to their owners.
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
        mongodb_reports: MongoDB usage reports
    
    Returns:
        Dict mapping tag -> set of (user_id, user_name) tuples
    """
    service = ImageUsageService()
    _, all_usage_info = service.extract_docker_tags_with_usage_info(mongodb_reports)
    
    # Build model_id -> createdBy mapping once
    model_created_by_map = build_model_created_by_mapping(mongodb_reports)
    
    # Build tag -> authorId mapping for environment revisions
    tag_to_author_map = build_tag_to_author_mapping(analyzer)
    
    tag_to_owners = defaultdict(set)
    unknown_tags_info = []  # Track why tags are unknown
    
    # For each image tag, find its owners
    for image_id, image_data in analyzer.images.items():
        tag = image_data['tag']
        
        # Get usage info for this tag
        usage_info = all_usage_info.get(tag, {})
        
        # Check if tag is referenced in MongoDB at all
        has_usage = bool(usage_info and any(
            usage_info.get('runs') or 
            usage_info.get('workspaces') or 
            usage_info.get('models') or 
            usage_info.get('projects') or 
            usage_info.get('scheduler_jobs') or 
            usage_info.get('organizations') or 
            usage_info.get('app_versions')
        ))
        
        # Extract owners from usage info (pass both mappings)
        owners = extract_owners_from_usage_info(tag, usage_info, mongodb_reports, model_created_by_map, tag_to_author_map)
        
        if owners:
            tag_to_owners[tag].update(owners)
        else:
            # If no owner found, mark as "Unknown"
            tag_to_owners[tag].add(('unknown', 'Unknown'))
            
            # Track why it's unknown for logging
            if not has_usage:
                unknown_tags_info.append((tag, 'not_in_mongodb'))
            else:
                unknown_tags_info.append((tag, 'owner_info_missing'))
    
    # Log summary of unknown tags
    if unknown_tags_info:
        not_in_mongo = [t for t, reason in unknown_tags_info if reason == 'not_in_mongodb']
        missing_owner = [t for t, reason in unknown_tags_info if reason == 'owner_info_missing']
        
        logger.info(f"\nFound {len(unknown_tags_info)} images with unknown owners:")
        if not_in_mongo:
            logger.info(f"  - {len(not_in_mongo)} images not referenced in MongoDB (orphaned/unused images)")
        if missing_owner:
            logger.info(f"  - {len(missing_owner)} images referenced in MongoDB but owner information unavailable")
    
    return dict(tag_to_owners)


def generate_user_size_report(
    analyzer: ImageAnalyzer,
    mongodb_reports: Dict[str, List[Dict]],
    image_types: List[str] = None
) -> Dict:
    """Generate a report of image sizes grouped by user/owner.
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
        mongodb_reports: MongoDB usage reports
        image_types: List of image types to include (default: all)
    
    Returns:
        Dict with report data including users sorted by total size
    """
    if image_types is None:
        image_types = ['environment', 'model']
    
    # Build mapping from tags to owners
    logger.info("Mapping images to their owners...")
    tag_to_owners = build_tag_to_owners_mapping(analyzer, mongodb_reports)
    
    # Group images by owner and calculate sizes
    user_stats = defaultdict(lambda: {
        'user_id': '',
        'user_name': '',
        'image_count': 0,
        'total_size_bytes': 0,
        'total_size_gb': 0.0,
        'freed_space_bytes': 0,
        'freed_space_gb': 0.0,
        'images': []
    })
    
    for image_id, image_data in analyzer.images.items():
        # Extract image type from image_id (format: "image_type:tag")
        image_type = image_id.split(':')[0] if ':' in image_id else 'unknown'
        
        if image_type not in image_types:
            continue
        
        tag = image_data['tag']
        
        # Get owners for this tag
        owners = tag_to_owners.get(tag, {('unknown', 'Unknown')})
        
        # Calculate sizes
        total_size_bytes = analyzer.get_image_total_size(image_id)
        freed_space_bytes = analyzer.freed_space_if_deleted([image_id])
        
        # Add to each owner's stats
        for owner_id, owner_name in owners:
            user_key = owner_id  # Use user_id as key
            
            if user_stats[user_key]['user_id'] == '':
                user_stats[user_key]['user_id'] = owner_id
                user_stats[user_key]['user_name'] = owner_name
            
            user_stats[user_key]['image_count'] += 1
            user_stats[user_key]['total_size_bytes'] += total_size_bytes
            user_stats[user_key]['freed_space_bytes'] += freed_space_bytes
            
            user_stats[user_key]['images'].append({
                'image_id': image_id,
                'image_type': image_type,
                'tag': tag,
                'total_size_bytes': total_size_bytes,
                'total_size_gb': round(total_size_bytes / (1024**3), 2),
                'freed_space_bytes': freed_space_bytes,
                'freed_space_gb': round(freed_space_bytes / (1024**3), 2)
            })
    
    # Convert to list and calculate GB values
    users_list = []
    for user_key, stats in user_stats.items():
        stats['total_size_gb'] = round(stats['total_size_bytes'] / (1024**3), 2)
        stats['freed_space_gb'] = round(stats['freed_space_bytes'] / (1024**3), 2)
        users_list.append(stats)
    
    # Sort by total size (descending)
    users_list.sort(key=lambda x: x['total_size_bytes'], reverse=True)
    
    # Calculate totals
    total_size = sum(u['total_size_bytes'] for u in users_list)
    total_freed = sum(u['freed_space_bytes'] for u in users_list)
    
    report_data = {
        'summary': {
            'total_users': len(users_list),
            'total_images': sum(u['image_count'] for u in users_list),
            'total_size_bytes': total_size,
            'total_size_gb': round(total_size / (1024**3), 2),
            'total_freed_if_all_deleted_bytes': total_freed,
            'total_freed_if_all_deleted_gb': round(total_freed / (1024**3), 2),
            'image_types': image_types,
            'generated_at': datetime.now().isoformat()
        },
        'users': users_list
    }
    
    return report_data


def print_report_summary(report_data: Dict) -> None:
    """Print a human-readable summary of the report"""
    summary = report_data['summary']
    users = report_data['users']
    
    logger.info("\n" + "=" * 80)
    logger.info("   Docker Image Size Report by User")
    logger.info("=" * 80)
    logger.info(f"Total Users: {summary['total_users']}")
    logger.info(f"Total Images: {summary['total_images']}")
    logger.info(f"Total Size: {sizeof_fmt(summary['total_size_bytes'])} ({summary['total_size_gb']} GB)")
    logger.info(f"Total Space Freed (if all deleted): {sizeof_fmt(summary['total_freed_if_all_deleted_bytes'])} ({summary['total_freed_if_all_deleted_gb']} GB)")
    logger.info("=" * 80)
    
    # Print top 20 users
    logger.info("\nTop 20 Users by Total Image Size:")
    logger.info("-" * 80)
    logger.info(f"{'Rank':<6} {'User ID':<30} {'User Name':<30} {'Images':<10} {'Total Size':<15} {'Freed if Deleted':<20}")
    logger.info("-" * 80)
    
    for idx, user in enumerate(users[:20], 1):
        user_id_display = user['user_id'][:27] + "..." if len(user['user_id']) > 30 else user['user_id']
        user_name_display = user['user_name'][:27] + "..." if len(user['user_name']) > 30 else user['user_name']
        logger.info(
            f"{idx:<6} {user_id_display:<30} {user_name_display:<30} "
            f"{user['image_count']:<10} {sizeof_fmt(user['total_size_bytes']):<15} "
            f"{sizeof_fmt(user['freed_space_bytes']):<20}"
        )
    
    if len(users) > 20:
        logger.info(f"\n... and {len(users) - 20} more users")
    
    logger.info("\n" + "=" * 80)
    logger.info("Note: 'Freed if Deleted' accounts for shared layers - only unique")
    logger.info("      layers are counted. Total size includes all layers (shared + unique).")
    logger.info("      Ownership is determined from runs, workspaces, models, and projects.")
    logger.info("")
    logger.info("      Images with 'Unknown' owner may be:")
    logger.info("      - Not referenced in MongoDB (orphaned/unused images)")
    logger.info("      - Referenced in MongoDB but owner information is unavailable")
    logger.info("=" * 80)


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Generate Docker image size report grouped by user",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate report (auto-generates reports if missing)
  python user_size_report.py
  
  # Force regeneration of all reports
  python user_size_report.py --generate-reports
  
  # Specify output file
  python user_size_report.py --output custom-report.json
        """
    )
    
    parser.add_argument(
        '--generate-reports',
        action='store_true',
        help='Force regeneration of image analysis and MongoDB reports before generating user report'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path for the report (default: user-size-report.json in reports directory)'
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
        logger.info("   Docker Image Size Report by User")
        logger.info("=" * 80)
        
        # Check if reports need to be generated
        if args.generate_reports:
            logger.info("Forcing regeneration of all reports...")
            ensure_all_reports(max_age_hours=0)  # Force regeneration
        else:
            # Check if reports exist and are fresh
            from utils.report_utils import is_report_fresh
            tag_sums_exists = is_report_fresh('tag-sums.json')
            mongodb_usage_exists = is_report_fresh('mongodb_usage_report.json')
            reports_exist = tag_sums_exists and mongodb_usage_exists
            
            if not reports_exist:
                logger.info("Required reports not found or stale. Generating them now...")
                ensure_all_reports()
        
        # Get configuration
        registry_url = config_manager.get_registry_url()
        repository = config_manager.get_repository()
        max_workers = args.max_workers or config_manager.get_max_workers()
        
        logger.info(f"Registry: {registry_url}")
        logger.info(f"Repository: {repository}")
        logger.info(f"Image Types: {', '.join(args.image_types)}")
        logger.info(f"Max Workers: {max_workers}")
        logger.info("=" * 80)
        
        # Load MongoDB reports
        logger.info("\nLoading MongoDB usage reports...")
        service = ImageUsageService()
        mongodb_reports = service.load_mongodb_usage_reports()
        
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
        logger.info("   Generating User Size Report")
        logger.info("=" * 80)
        
        report_data = generate_user_size_report(analyzer, mongodb_reports, args.image_types)
        
        # Save report
        if args.output:
            output_path = args.output
        else:
            # Use default path from config or reports directory
            reports_dir = Path(config_manager.get_output_dir())
            output_path = str(reports_dir / "user-size-report.json")
        
        saved_path = save_json(output_path, report_data, timestamp=True)
        logger.info(f"\nReport saved to: {saved_path}")
        
        # Print summary
        print_report_summary(report_data)
        
        logger.info("\n✅ User size report generation completed successfully!")
        
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
