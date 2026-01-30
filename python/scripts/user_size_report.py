#!/usr/bin/env python3
"""
Docker Image Size Report by User

This script generates a report showing which users are associated with the most
Docker images by total size, helping identify who is consuming the most registry space.

The "owner" / user shown for each image is:
  - **Environment images**: the author of the environment revision (metadata.authorId
    in environment_revisions), i.e. the person who created that revision.
  - **Model images**: the creator of the model version (metadata.createdBy in
    model_versions), i.e. the person who created that version.

The Login ID column is the loginId.id of that user from the users collection.

Usage examples:
  # Generate report (auto-generates image analysis and MongoDB reports if missing)
  python user_size_report.py
  
  # Force regeneration of all reports
  python user_size_report.py --generate-reports
"""

import argparse
import json
import re
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
from utils.image_metadata import extract_model_tag_from_version_doc, lookup_user_names_and_logins
from utils.image_usage import ImageUsageService
from utils.logging_utils import get_logger, setup_logging
from utils.object_id_utils import normalize_object_id
from utils.report_utils import save_json, ensure_all_reports, sizeof_fmt

logger = get_logger(__name__)




def extract_owners_from_usage_info(
    tag: str,
    usage_info: Dict,
    mongodb_reports: Dict[str, List[Dict]],
    tag_to_model_version_created_by: Dict[str, Tuple[str, str, str]] = None,
    tag_to_author_map: Dict[str, Tuple[str, str]] = None
) -> Set[Tuple[str, str]]:
    """Extract owner (user_id, user_name) pairs for a given tag from usage info.
    
    Args:
        tag: Docker image tag
        usage_info: Usage info dict for the tag
        mongodb_reports: Full MongoDB reports for additional lookups
        tag_to_model_version_created_by: Mapping from Docker tag -> (version_id, createdBy_id, createdBy_name) from model_versions
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
        normalized_id = normalize_object_id(author_id)
        if normalized_id:
            owners.add((normalized_id, author_name))
    
    # Check if this is a model tag and we have createdBy from model_versions
    if tag_to_model_version_created_by is None:
        tag_to_model_version_created_by = {}
    
    model_version_info = tag_to_model_version_created_by.get(tag)
    if model_version_info:
        # This is a model version image with a known creator
        version_id, created_by_id, created_by_name = model_version_info
        normalized_id = normalize_object_id(created_by_id)
        if normalized_id:
            owners.add((normalized_id, created_by_name))
    
    # From runs: use project_owner_id and project_owner_name
    for run in usage_info.get('runs', []):
        owner_id = run.get('project_owner_id', '')
        owner_name = run.get('project_owner_name', '')
        if owner_id and owner_id != 'unknown':
            normalized_id = normalize_object_id(owner_id)
            if normalized_id:
                owners.add((normalized_id, owner_name or 'Unknown'))
    
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
                        normalized_id = normalize_object_id(owner_id)
                        if normalized_id:
                            owners.add((normalized_id, user_name or 'Unknown'))
                    elif user_name and user_name != 'unknown':
                        # Fallback: use workspace_id as identifier if we can't find ownerId
                        owners.add(('workspace:' + str(workspace_id), user_name))
                    break
    
    # From models: fallback to pipeline data if we didn't get it from direct query
    # (This handles cases where the tag might not be in model_versions or query failed)
    if not model_version_info:
        for model in usage_info.get('models', []):
            model_owner = model.get('model_owner', '')  # Name from pipeline
            model_created_by = model.get('model_created_by', '')  # Name from pipeline
            version_id = model.get('version_id') or model.get('model_version_id', '')
            
            if model_owner and model_owner != 'unknown':
                # Fallback: use model_owner name with version_id as identifier
                if version_id and version_id != 'unknown':
                    owners.add(('model_version:' + str(version_id), model_owner))
                else:
                    model_id = model.get('model_id', '')
                    if model_id:
                        owners.add(('model:' + str(model_id), model_owner))
    
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
                    normalized_id = normalize_object_id(owner_id)
                    if normalized_id:
                        owners.add((normalized_id, owner_name))
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
                        normalized_id = normalize_object_id(owner_id)
                        if normalized_id:
                            owners.add((normalized_id, 'Unknown'))
                    break
    
    return owners


def build_tag_to_model_version_created_by_mapping(analyzer: ImageAnalyzer) -> Dict[str, Tuple[str, str]]:
    """Build a mapping from Docker tag to (model_version_id, createdBy) by querying MongoDB.
    
    Model versions have their owner in metadata.createdBy, and the Docker tag is stored
    in metadata.builds.slug.image.tag.
    
    Model tags can have formats like:
    - Simple: `<modelId>-<version>`
    - Extended: `<modelId>-<version>-<timestamp>_<uniqueId>`
    
    We try exact match first, then prefix matching for extended formats.
    
    Args:
        analyzer: ImageAnalyzer instance with analyzed images
    
    Returns:
        Dict mapping tag -> (model_version_id as string, createdBy ObjectId as string, createdBy_name)
    """
    from utils.tag_matching import model_tags_match, extract_model_tag_prefix
    
    tag_to_version_created_by = {}
    
    # Collect all model tags from the analyzer
    model_tags = set()
    for image_id, image_data in analyzer.images.items():
        if image_id.startswith('model:'):
            tag = image_data['tag']
            model_tags.add(tag)
    
    if not model_tags:
        return tag_to_version_created_by
    
    # Query MongoDB to get model_version_id and createdBy for each tag
    try:
        from utils.mongo_utils import get_mongo_client
        from utils.config_manager import config_manager
        from bson import ObjectId
        
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]
        
        # First, try exact match
        model_versions_exact = db.model_versions.find(
            {'metadata.builds.slug.image.tag': {'$in': list(model_tags)}},
            {'_id': 1, 'metadata.builds.slug.image.tag': 1, 'metadata.createdBy': 1, 'modelId.value': 1}
        )
        
        # Track which tags we've matched
        matched_tags = set()
        tag_to_version_id = {}
        tag_to_created_by_id = {}
        created_by_ids = set()
        
        # Process exact matches
        for version_doc in model_versions_exact:
            stored_tag = extract_model_tag_from_version_doc(version_doc)
            version_id = version_doc.get('_id')
            metadata = version_doc.get('metadata', {})
            created_by = metadata.get('createdBy')
            
            if stored_tag and stored_tag in model_tags and version_id and created_by:
                matched_tags.add(stored_tag)
                version_id_str = normalize_object_id(version_id)
                created_by_id = created_by
                created_by_ids.add(created_by)
                tag_to_version_id[stored_tag] = version_id_str
                tag_to_created_by_id[stored_tag] = created_by_id
        
        # For unmatched tags, try prefix matching using utility function
        unmatched_tags = model_tags - matched_tags
        
        if unmatched_tags:
            logger.debug(f"Trying prefix matching for {len(unmatched_tags)} unmatched model tags")
            
            # Query all model_versions and match by prefix
            all_model_versions = db.model_versions.find(
                {'metadata.builds.slug.image.tag': {'$exists': True, '$ne': None}},
                {'_id': 1, 'metadata.builds.slug.image.tag': 1, 'metadata.createdBy': 1}
            )
            
            for version_doc in all_model_versions:
                stored_tag = extract_model_tag_from_version_doc(version_doc)
                version_id = version_doc.get('_id')
                metadata = version_doc.get('metadata', {})
                created_by = metadata.get('createdBy')
                
                if not stored_tag or not version_id or not created_by:
                    continue
                
                # Use utility function to match tags
                for registry_tag in unmatched_tags:
                    if registry_tag not in matched_tags and model_tags_match(registry_tag, stored_tag):
                        matched_tags.add(registry_tag)
                        version_id_str = normalize_object_id(version_id)
                        created_by_id = created_by
                        created_by_ids.add(created_by)
                        tag_to_version_id[registry_tag] = version_id_str
                        tag_to_created_by_id[registry_tag] = created_by_id
                        logger.debug(f"Matched registry tag '{registry_tag}' to stored tag '{stored_tag}'")
        
        # Look up user names for all createdBy IDs
        created_by_id_to_name, _ = lookup_user_names_and_logins(created_by_ids)
        
        # Build final mapping
        for tag, version_id_str in tag_to_version_id.items():
            created_by_id = tag_to_created_by_id.get(tag)
            if created_by_id:
                normalized_created_by = normalize_object_id(created_by_id)
                created_by_name = created_by_id_to_name.get(normalized_created_by, 'Unknown')
                tag_to_version_created_by[tag] = (version_id_str, normalized_created_by, created_by_name)
        
        if unmatched_tags - matched_tags:
            logger.info(f"Could not match {len(unmatched_tags - matched_tags)} model tags to MongoDB model_versions")
        
        mongo_client.close()
    except Exception as e:
        logger.warning(f"Could not query MongoDB for model version owners: {e}")
        import traceback
        logger.debug(traceback.format_exc())
    
    return tag_to_version_created_by


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
        author_id_to_name, _ = lookup_user_names_and_logins(author_ids)
        
        # Build final mapping with names (normalize author_id_str to ensure consistency)
        for tag, author_id_str in tag_to_author_id.items():
            normalized_author_id = normalize_object_id(author_id_str)
            author_name = author_id_to_name.get(normalized_author_id, 'Unknown')
            tag_to_author[tag] = (normalized_author_id, author_name)
        
        mongo_client.close()
    except Exception as e:
        logger.warning(f"Could not query MongoDB for environment revision authors: {e}")
    
    return tag_to_author


def build_user_login_id_mapping(user_ids: Set[str]) -> Dict[str, str]:
    """Build a mapping from user_id to loginId by querying MongoDB users collection.
    
    Args:
        user_ids: Set of user IDs (ObjectIds as strings) to look up
    
    Returns:
        Dict mapping user_id -> loginId.id (or empty string if not found)
    """
    # Filter out non-ObjectId formats
    filtered_ids = set()
    for user_id_str in user_ids:
        # Skip non-ObjectId formats (like 'unknown', 'workspace:...', etc.)
        if not user_id_str.startswith('unknown') and ':' not in user_id_str:
            filtered_ids.add(user_id_str)
    
    # Use shared utility function
    _, user_id_to_login = lookup_user_names_and_logins(filtered_ids)
    return user_id_to_login


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
    
    # Build tag -> (version_id, createdBy_id, createdBy_name) mapping for model versions
    # Model versions have their owner in metadata.createdBy
    tag_to_model_version_created_by = build_tag_to_model_version_created_by_mapping(analyzer)
    
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
        owners = extract_owners_from_usage_info(tag, usage_info, mongodb_reports, tag_to_model_version_created_by, tag_to_author_map)
        
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
        'login_id': '',
        'image_count': 0,
        'total_size_bytes': 0,
        'total_size_gb': 0.0,
        'freed_space_bytes': 0,
        'freed_space_gb': 0.0,
        'freed_space_environment_bytes': 0,
        'freed_space_environment_gb': 0.0,
        'freed_space_model_bytes': 0,
        'freed_space_model_gb': 0.0,
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
            # Normalize owner_id to string to merge duplicates (handles $oid format)
            normalized_id = normalize_object_id(owner_id)
            if not normalized_id:
                continue
            
            user_key = normalized_id  # Use normalized user_id as key
            
            if user_stats[user_key]['user_id'] == '':
                user_stats[user_key]['user_id'] = normalized_id
                user_stats[user_key]['user_name'] = owner_name
            
            user_stats[user_key]['image_count'] += 1
            user_stats[user_key]['total_size_bytes'] += total_size_bytes
            user_stats[user_key]['freed_space_bytes'] += freed_space_bytes
            
            # Track freed space by image type
            if image_type == 'environment':
                user_stats[user_key]['freed_space_environment_bytes'] += freed_space_bytes
            elif image_type == 'model':
                user_stats[user_key]['freed_space_model_bytes'] += freed_space_bytes
            
            user_stats[user_key]['images'].append({
                'image_id': image_id,
                'image_type': image_type,
                'tag': tag,
                'total_size_bytes': total_size_bytes,
                'total_size_gb': round(total_size_bytes / (1024**3), 2),
                'freed_space_bytes': freed_space_bytes,
                'freed_space_gb': round(freed_space_bytes / (1024**3), 2)
            })
    
    # Collect all user IDs to look up login IDs
    all_user_ids = set(user_stats.keys())
    user_id_to_login = build_user_login_id_mapping(all_user_ids)
    
    # Convert to list and calculate GB values, add login IDs
    users_list = []
    for user_key, stats in user_stats.items():
        stats['total_size_gb'] = round(stats['total_size_bytes'] / (1024**3), 2)
        stats['freed_space_gb'] = round(stats['freed_space_bytes'] / (1024**3), 2)
        stats['freed_space_environment_gb'] = round(stats['freed_space_environment_bytes'] / (1024**3), 2)
        stats['freed_space_model_gb'] = round(stats['freed_space_model_bytes'] / (1024**3), 2)
        stats['login_id'] = user_id_to_login.get(user_key, '')
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
    logger.info("-" * 165)
    logger.info(f"{'Rank':<6} {'User ID':<30} {'User Name':<30} {'Login ID':<20} {'Images':<10} {'Total Size':<15} {'Freed if Deleted':<20} {'Environments':<15} {'Models':<15}")
    logger.info("-" * 165)
    
    for idx, user in enumerate(users[:20], 1):
        user_id_display = user['user_id'][:27] + "..." if len(user['user_id']) > 30 else user['user_id']
        user_name_display = user['user_name'][:27] + "..." if len(user['user_name']) > 30 else user['user_name']
        login_id_display = user.get('login_id', '')[:17] + "..." if len(user.get('login_id', '')) > 20 else user.get('login_id', '')
        logger.info(
            f"{idx:<6} {user_id_display:<30} {user_name_display:<30} {login_id_display:<20} "
            f"{user['image_count']:<10} {sizeof_fmt(user['total_size_bytes']):<15} "
            f"{sizeof_fmt(user['freed_space_bytes']):<20} "
            f"{sizeof_fmt(user.get('freed_space_environment_bytes', 0)):<15} "
            f"{sizeof_fmt(user.get('freed_space_model_bytes', 0)):<15}"
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
        
        logger.info(f"Registry: {registry_url}")
        logger.info(f"Repository: {repository}")
        logger.info(f"Image Types: {', '.join(args.image_types)}")
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
            if analyzer.analyze_image(image_type, object_ids=None, max_workers=args.max_workers):
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
