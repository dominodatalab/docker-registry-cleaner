#!/usr/bin/env python3
"""
Find and optionally delete private environment tags owned by deactivated Keycloak users.

This script queries Keycloak for deactivated users, finds their private environments
in MongoDB, and identifies matching Docker tags in the registry. Can optionally 
delete the Docker images and clean up MongoDB records.

Workflow:
- Query Keycloak for deactivated users (enabled == False)
- Extract Domino user IDs from Keycloak user attributes
- Query MongoDB environments_v2 collection for private environments owned by these users
- Query environment_revisions collection for related revisions
- Find Docker tags containing these ObjectIDs in environment and model images
- Generate a comprehensive report of affected tags and their sizes
- Optionally delete Docker images and clean up MongoDB records (with --apply)

Usage examples:
  # Find private environments owned by deactivated users (dry-run)
  python delete_unused_private_env_tags.py
  
  # Delete private environments owned by deactivated users
  python delete_unused_private_env_tags.py --apply
  
  # Delete from pre-generated file
  python delete_unused_private_env_tags.py --apply --input deactivated-user-envs.json
"""

import argparse
import json
import os
import requests
import sys

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from keycloak import KeycloakAdmin
from bson import ObjectId

from config_manager import config_manager, SkopeoClient
from image_data_analysis import ImageAnalyzer
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json

# Disable SSL warnings for Keycloak
requests.packages.urllib3.disable_warnings()

logger = get_logger(__name__)


@dataclass
class DeactivatedUserEnvInfo:
    """Data class for deactivated user environment information"""
    object_id: str
    image_type: str
    tag: str
    full_image: str
    user_email: str
    user_id: str
    env_name: str = ""
    context: Dict = None

    def __post_init__(self):
        if self.context is None:
            self.context = {}


class DeactivatedUserEnvFinder:
    """Main class for finding and managing private environments owned by deactivated users"""
    
    def __init__(self, registry_url: str, repository: str):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(config_manager, use_pod=False)
        self.logger = get_logger(__name__)
        
        # Image types to scan
        self.image_types = ['environment', 'model']
    
    def get_keycloak_client(self) -> KeycloakAdmin:
        """Initialize Keycloak client"""
        kc_host = os.getenv("KEYCLOAK_HOST")
        if not kc_host:
            # Try legacy environment variables
            kc_addr = os.getenv("KEYCLOAK_HTTP_PORT_8443_TCP_ADDR")
            kc_port = os.getenv("KEYCLOAK_HTTP_PORT_8443_TCP_PORT")
            if kc_addr and kc_port:
                kc_host = f"https://{kc_addr}:{kc_port}/auth/"
            else:
                raise ValueError("KEYCLOAK_HOST environment variable not set")
        
        kc_username = os.getenv("KEYCLOAK_USERNAME")
        kc_password = os.getenv("KEYCLOAK_PASSWORD")
        
        if not kc_username or not kc_password:
            raise ValueError("KEYCLOAK_USERNAME and KEYCLOAK_PASSWORD environment variables must be set")
        
        return KeycloakAdmin(
            server_url=kc_host,
            username=kc_username,
            password=kc_password,
            realm_name="DominoRealm",
            user_realm_name="master",
            verify=False
        )
    
    def fetch_deactivated_user_env_ids(self) -> tuple[List[str], List[str], Dict[str, Dict]]:
        """Fetch environment and revision ObjectIDs for private environments owned by deactivated Keycloak users
        
        Returns:
            tuple of (environment_ids, revision_ids, user_mapping)
            where user_mapping maps ObjectID -> {email, user_id, env_name}
        """
        # Get deactivated users from Keycloak
        self.logger.info("Connecting to Keycloak...")
        try:
            kc = self.get_keycloak_client()
            kc_users = kc.get_users({})
        except Exception as e:
            self.logger.error(f"Failed to connect to Keycloak: {e}")
            raise
        
        # Find deactivated users with domino-user-id attribute
        deactivated_user_ids = {}
        for kc_user in kc_users:
            if not kc_user.get('enabled', True):  # User is deactivated
                try:
                    domino_user_id = kc_user['attributes']['domino-user-id'][0]
                    email = kc_user.get('email', 'unknown')
                    deactivated_user_ids[domino_user_id] = {
                        'email': email,
                        'keycloak_id': kc_user['id']
                    }
                    self.logger.info(f"Found deactivated user: {email} (Domino ID: {domino_user_id})")
                except (KeyError, IndexError):
                    # User doesn't have domino-user-id attribute
                    pass
        
        self.logger.info(f"Found {len(deactivated_user_ids)} deactivated users in Keycloak")
        
        if not deactivated_user_ids:
            return [], [], {}
        
        # Query MongoDB for private environments owned by these users
        mongo_client = get_mongo_client()
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            environments_collection = db["environments_v2"]
            
            # Convert domino user IDs to ObjectIds
            deactivated_owner_ids = [ObjectId(user_id) for user_id in deactivated_user_ids.keys()]
            
            # Find private environments owned by deactivated users
            query = {
                "ownerId": {"$in": deactivated_owner_ids},
                "visibility": "Private"
            }
            cursor = environments_collection.find(query, {"_id": 1, "ownerId": 1, "name": 1})
            
            environment_ids = []
            user_mapping = {}
            
            for doc in cursor:
                env_id = doc.get("_id")
                owner_id = doc.get("ownerId")
                env_name = doc.get("name", "")
                
                if env_id is not None and owner_id is not None:
                    env_id_str = str(env_id)
                    owner_id_str = str(owner_id)
                    environment_ids.append(env_id_str)
                    
                    # Map environment ID to user info
                    user_mapping[env_id_str] = {
                        'email': deactivated_user_ids[owner_id_str]['email'],
                        'user_id': owner_id_str,
                        'env_name': env_name
                    }
            
            self.logger.info(f"Found {len(environment_ids)} private environments owned by deactivated users")
            
            # Now check environment_revisions for documents with matching environmentId
            environment_revisions_collection = db["environment_revisions"]
            revision_ids = []
            
            if environment_ids:
                # Convert string IDs back to ObjectId for the query
                environment_object_ids = [ObjectId(env_id) for env_id in environment_ids]
                
                # Find environment revisions that belong to these environments
                revision_cursor = environment_revisions_collection.find(
                    {"environmentId": {"$in": environment_object_ids}}, 
                    {"_id": 1, "environmentId": 1}
                )
                
                for doc in revision_cursor:
                    rev_id = doc.get("_id")
                    env_id = doc.get("environmentId")
                    
                    if rev_id is not None and env_id is not None:
                        rev_id_str = str(rev_id)
                        env_id_str = str(env_id)
                        revision_ids.append(rev_id_str)
                        
                        # Map revision ID to user info (from parent environment)
                        if env_id_str in user_mapping:
                            user_mapping[rev_id_str] = user_mapping[env_id_str]
                
                self.logger.info(f"Found {len(revision_ids)} environment revisions for these environments")
            
            # Combine both sets of IDs
            all_ids = list(set(environment_ids + revision_ids))
            
            self.logger.info(f"Total ObjectIDs to search for: {len(all_ids)}")
            return environment_ids, revision_ids, user_mapping
            
        finally:
            mongo_client.close()
    
    def list_tags_for_image(self, image: str) -> List[str]:
        """List tags for a specific image using skopeo"""
        ref = f"docker://{self.registry_url}/{self.repository}/{image}"
        output = self.skopeo_client.run_skopeo_command("list-tags", [ref])
        
        if not output:
            return []
        
        try:
            payload = json.loads(output)
            return payload.get("Tags", []) or []
        except json.JSONDecodeError:
            self.logger.error(f"Failed to parse list-tags output for {ref}")
            return []
    
    def find_matching_tags(self, all_ids: List[str], user_mapping: Dict[str, Dict]) -> List[DeactivatedUserEnvInfo]:
        """Find Docker tags that contain ObjectIDs from deactivated user environments"""
        id_set = set(all_ids)
        matching_tags = []
        
        for image_type in self.image_types:
            self.logger.info(f"Scanning {image_type} images for deactivated user environment ObjectIDs...")
            tags = self.list_tags_for_image(image_type)
            self.logger.info(f"  Found {len(tags)} tags in {image_type}")
            
            for tag in tags:
                for obj_id in id_set:
                    if obj_id in tag:
                        full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                        user_info = user_mapping.get(obj_id, {'email': 'unknown', 'user_id': 'unknown', 'env_name': ''})
                        
                        tag_info = DeactivatedUserEnvInfo(
                            object_id=obj_id,
                            image_type=image_type,
                            tag=tag,
                            full_image=full_image,
                            user_email=user_info['email'],
                            user_id=user_info['user_id'],
                            env_name=user_info.get('env_name', ''),
                            context={
                                'repository': self.repository,
                                'image_type': image_type,
                                'tag': tag,
                                'full_image': full_image,
                                'user_email': user_info['email'],
                                'user_id': user_info['user_id'],
                                'env_name': user_info.get('env_name', '')
                            }
                        )
                        matching_tags.append(tag_info)
        
        self.logger.info(f"Found {len(matching_tags)} matching tags for deactivated user environments")
        return matching_tags
    
    def calculate_freed_space(self, deactivated_user_tags: List[DeactivatedUserEnvInfo]) -> int:
        """Calculate total space that would be freed by deleting deactivated user environment tags.
        
        This method uses ImageAnalyzer to properly account for shared layers.
        Only layers that would have no remaining references after deletion are counted.
        
        Args:
            deactivated_user_tags: List of tags to analyze
            
        Returns:
            Total bytes that would be freed
        """
        if not deactivated_user_tags:
            return 0
        
        try:
            self.logger.info("Analyzing Docker images to calculate freed space...")
            
            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)
            
            # Get unique ObjectIDs from tags
            unique_ids = list(set(tag.object_id for tag in deactivated_user_tags))
            
            # Analyze both environment and model images filtered by ObjectIDs
            for image_type in self.image_types:
                self.logger.info(f"Analyzing {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=unique_ids)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")
            
            # Build list of image_ids from tags
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = [f"{tag.image_type}:{tag.tag}" for tag in deactivated_user_tags]
            
            # Calculate freed space using ImageAnalyzer's method
            # This properly accounts for shared layers
            total_freed = analyzer.freed_space_if_deleted(image_ids)
            
            self.logger.info(f"Total space that would be freed: {total_freed / (1024**3):.2f} GB")
            
            return total_freed
            
        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return 0
    
    def delete_deactivated_user_envs(self, deactivated_user_tags: List[DeactivatedUserEnvInfo],
                                   environment_ids: List[str], revision_ids: List[str]) -> Dict[str, int]:
        """Delete Docker images and clean up MongoDB records for deactivated user environments"""
        if not deactivated_user_tags:
            self.logger.info("No tags to delete")
            return {'docker_images_deleted': 0, 'mongo_records_cleaned': 0}
        
        deletion_results = {
            'docker_images_deleted': 0,
            'mongo_records_cleaned': 0
        }
        
        try:
            # Enable deletion in registry if it's in the same Kubernetes cluster
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            if registry_in_cluster:
                self.logger.info("Registry is in-cluster, enabling deletion...")
                if not self.skopeo_client.enable_registry_deletion():
                    self.logger.warning("Failed to enable registry deletion - continuing anyway")
            
            # Delete Docker images directly using skopeo
            # Track which ObjectIDs were successfully deleted so we only clean up their MongoDB records
            self.logger.info(f"Deleting {len(deactivated_user_tags)} Docker images from registry...")
            
            deleted_count = 0
            failed_deletions = []
            successfully_deleted_object_ids = set()
            
            for tag_info in deactivated_user_tags:
                try:
                    self.logger.info(f"  Deleting: {tag_info.full_image} (user: {tag_info.user_email})")
                    success = self.skopeo_client.delete_image(
                        f"{self.repository}/{tag_info.image_type}",
                        tag_info.tag
                    )
                    if success:
                        deleted_count += 1
                        successfully_deleted_object_ids.add(tag_info.object_id)
                        self.logger.info(f"    ✓ Deleted successfully")
                    else:
                        failed_deletions.append(tag_info.full_image)
                        self.logger.warning(f"    ✗ Failed to delete - MongoDB record will NOT be cleaned")
                except Exception as e:
                    failed_deletions.append(tag_info.full_image)
                    self.logger.error(f"    ✗ Error deleting: {e} - MongoDB record will NOT be cleaned")
            
            deletion_results['docker_images_deleted'] = deleted_count
            
            # Disable deletion in registry if it was enabled
            if registry_in_cluster:
                self.logger.info("Disabling deletion in registry...")
                if not self.skopeo_client.disable_registry_deletion():
                    self.logger.warning("Failed to disable registry deletion")
            
            if failed_deletions:
                self.logger.warning(f"Failed to delete {len(failed_deletions)} Docker images:")
                for img in failed_deletions:
                    self.logger.warning(f"  - {img}")
                self.logger.warning("MongoDB records for failed deletions will be preserved.")
            
            # Clean up MongoDB records directly - ONLY for successfully deleted Docker images
            # Separate environment IDs from revision IDs for proper cleanup
            env_ids_to_clean = [eid for eid in environment_ids if eid in successfully_deleted_object_ids]
            rev_ids_to_clean = [rid for rid in revision_ids if rid in successfully_deleted_object_ids]
            
            skipped_env_ids = len(environment_ids) - len(env_ids_to_clean)
            skipped_rev_ids = len(revision_ids) - len(rev_ids_to_clean)
            
            if skipped_env_ids > 0 or skipped_rev_ids > 0:
                self.logger.info(f"Skipping MongoDB cleanup for {skipped_env_ids + skipped_rev_ids} ObjectIDs due to Docker deletion failures")
            
            mongo_client = get_mongo_client()
            try:
                db = mongo_client[config_manager.get_mongo_db()]
                
                # Clean up environment_revisions collection
                if rev_ids_to_clean:
                    self.logger.info(f"Cleaning up {len(rev_ids_to_clean)} environment_revisions records from MongoDB...")
                    revisions_collection = db["environment_revisions"]
                    
                    for obj_id_str in rev_ids_to_clean:
                        try:
                            obj_id = ObjectId(obj_id_str)
                            result = revisions_collection.delete_one({"_id": obj_id})
                            if result.deleted_count > 0:
                                self.logger.info(f"  ✓ Deleted environment_revision: {obj_id_str}")
                                deletion_results['mongo_records_cleaned'] += 1
                            else:
                                self.logger.warning(f"  ✗ Environment_revision not found: {obj_id_str}")
                        except Exception as e:
                            self.logger.error(f"  ✗ Error deleting environment_revision {obj_id_str}: {e}")
                
                # Clean up environments_v2 collection
                if env_ids_to_clean:
                    self.logger.info(f"Cleaning up {len(env_ids_to_clean)} environments_v2 records from MongoDB...")
                    environments_collection = db["environments_v2"]
                    
                    for obj_id_str in env_ids_to_clean:
                        try:
                            obj_id = ObjectId(obj_id_str)
                            result = environments_collection.delete_one({"_id": obj_id})
                            if result.deleted_count > 0:
                                self.logger.info(f"  ✓ Deleted environment: {obj_id_str}")
                                deletion_results['mongo_records_cleaned'] += 1
                            else:
                                self.logger.warning(f"  ✗ Environment not found: {obj_id_str}")
                        except Exception as e:
                            self.logger.error(f"  ✗ Error deleting environment {obj_id_str}: {e}")
            finally:
                mongo_client.close()
            
            self.logger.info("Deactivated user environment deletion completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error deleting deactivated user environments: {e}")
            raise
        
        return deletion_results
    
    def generate_report(self, environment_ids: List[str], revision_ids: List[str], 
                       deactivated_user_tags: List[DeactivatedUserEnvInfo],
                       freed_space_bytes: int = 0) -> Dict:
        """Generate a comprehensive report of deactivated user environments"""
        
        # Group by user email
        by_user = {}
        for tag in deactivated_user_tags:
            email = tag.user_email
            if email not in by_user:
                by_user[email] = {
                    'user_id': tag.user_id,
                    'tags': [],
                    'environments': set(),
                    'tag_count': 0
                }
            by_user[email]['tags'].append(tag)
            by_user[email]['tag_count'] += 1
            by_user[email]['environments'].add(tag.object_id)
        
        # Convert sets to lists for JSON serialization
        for email in by_user:
            by_user[email]['environments'] = list(by_user[email]['environments'])
            by_user[email]['environment_count'] = len(by_user[email]['environments'])
        
        # Group by image type
        by_image_type = {'environment': 0, 'model': 0}
        for tag in deactivated_user_tags:
            by_image_type[tag.image_type] += 1
        
        # Create summary statistics
        summary = {
            'total_deactivated_users': len(by_user),
            'total_environment_ids': len(environment_ids),
            'total_revision_ids': len(revision_ids),
            'total_matching_tags': len(deactivated_user_tags),
            'freed_space_bytes': freed_space_bytes,
            'freed_space_mb': round(freed_space_bytes / (1024 * 1024), 2),
            'freed_space_gb': round(freed_space_bytes / (1024 * 1024 * 1024), 2),
            'tags_by_image_type': by_image_type
        }
        
        # Prepare detailed data
        detailed_tags = []
        for tag in deactivated_user_tags:
            detailed_tags.append({
                'object_id': tag.object_id,
                'image_type': tag.image_type,
                'tag': tag.tag,
                'full_image': tag.full_image,
                'user_email': tag.user_email,
                'user_id': tag.user_id,
                'env_name': tag.env_name,
                'context': tag.context
            })
        
        report = {
            'summary': summary,
            'environment_ids': environment_ids,
            'revision_ids': revision_ids,
            'all_object_ids': list(set(environment_ids + revision_ids)),
            'tags': detailed_tags,
            'grouped_by_user': {
                email: {
                    'user_id': info['user_id'],
                    'tag_count': info['tag_count'],
                    'environment_count': info['environment_count'],
                    'environments': info['environments'],
                    'tags': [t.__dict__ for t in info['tags']]
                }
                for email, info in by_user.items()
            },
            'metadata': {
                'registry_url': self.registry_url,
                'repository': self.repository,
                'image_types_scanned': self.image_types,
                'analysis_timestamp': datetime.now().isoformat()
            }
        }
        
        return report
    
    def load_report_from_file(self, file_path: str) -> tuple[List[str], List[str], List[DeactivatedUserEnvInfo]]:
        """Load deactivated user environments from a pre-generated report file"""
        try:
            with open(file_path, 'r') as f:
                report = json.load(f)
            
            environment_ids = report.get('environment_ids', [])
            revision_ids = report.get('revision_ids', [])
            
            tags = []
            for tag_data in report.get('tags', []):
                tag = DeactivatedUserEnvInfo(
                    object_id=tag_data['object_id'],
                    image_type=tag_data['image_type'],
                    tag=tag_data['tag'],
                    full_image=tag_data['full_image'],
                    user_email=tag_data.get('user_email', 'unknown'),
                    user_id=tag_data.get('user_id', 'unknown'),
                    env_name=tag_data.get('env_name', ''),
                    context=tag_data.get('context', {})
                )
                tags.append(tag)
            
            self.logger.info(f"Loaded {len(tags)} tags from {file_path}")
            return environment_ids, revision_ids, tags
            
        except Exception as e:
            self.logger.error(f"Error loading report from {file_path}: {e}")
            raise


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Find and optionally delete private environments owned by deactivated Keycloak users",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find private environments owned by deactivated users (dry-run)
  python delete_unused_private_env_tags.py

  # Override registry settings
  python delete_unused_private_env_tags.py --registry-url registry.example.com --repository my-repo

  # Custom output file
  python delete_unused_private_env_tags.py --output deactivated-user-envs.json

  # Delete private environments owned by deactivated users (requires confirmation)
  python delete_unused_private_env_tags.py --apply

  # Delete from pre-generated file
  python delete_unused_private_env_tags.py --apply --input deactivated-user-envs.json

  # Force deletion without confirmation
  python delete_unused_private_env_tags.py --apply --force

Environment Variables Required:
  KEYCLOAK_HOST or (KEYCLOAK_HTTP_PORT_8443_TCP_ADDR and KEYCLOAK_HTTP_PORT_8443_TCP_PORT)
  KEYCLOAK_USERNAME
  KEYCLOAK_PASSWORD
        """
    )
    
    parser.add_argument(
        '--registry-url',
        help='Docker registry URL (default: from config)'
    )
    
    parser.add_argument(
        '--repository',
        help='Repository name (default: from config)'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path (default: reports/deactivated-user-envs.json)'
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete environments and images (default: dry-run)'
    )
    
    parser.add_argument(
        '--input',
        help='Input file containing pre-generated report to delete'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt when using --apply'
    )
    
    return parser.parse_args()


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()
    
    # Get configuration
    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    output_file = args.output or os.path.join(config_manager.get_output_dir(), "deactivated-user-envs.json")
    
    try:
        # Determine operation mode
        is_delete_mode = args.apply
        use_input_file = args.input is not None
        
        logger.info("=" * 60)
        if is_delete_mode:
            logger.info("   Deleting Private Environments (Deactivated Users)")
        else:
            logger.info("   Finding Private Environments (Deactivated Users)")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")
        
        if use_input_file:
            logger.info(f"Input file: {args.input}")
        else:
            logger.info(f"Output file: {output_file}")
        
        # Create finder
        finder = DeactivatedUserEnvFinder(registry_url, repository)
        
        # Handle different operation modes
        if use_input_file:
            # Mode 1: Delete from pre-generated file
            logger.info(f"Loading report from {args.input}...")
            environment_ids, revision_ids, deactivated_user_tags = finder.load_report_from_file(args.input)
            
            if not deactivated_user_tags:
                logger.warning(f"No tags found in {args.input}")
                sys.exit(0)
                
        else:
            # Mode 2: Find deactivated user environments (and optionally delete them)
            logger.info("Fetching private environments owned by deactivated Keycloak users...")
            environment_ids, revision_ids, user_mapping = finder.fetch_deactivated_user_env_ids()
            
            if not environment_ids and not revision_ids:
                logger.info("No private environments found for deactivated users")
                # Still create an empty report
                empty_report = {
                    'summary': {
                        'total_deactivated_users': 0,
                        'total_environment_ids': 0,
                        'total_revision_ids': 0,
                        'total_matching_tags': 0,
                        'freed_space_bytes': 0,
                        'freed_space_mb': 0,
                        'freed_space_gb': 0,
                        'tags_by_image_type': {'environment': 0, 'model': 0}
                    },
                    'environment_ids': [],
                    'revision_ids': [],
                    'all_object_ids': [],
                    'tags': [],
                    'grouped_by_user': {},
                    'metadata': {
                        'registry_url': registry_url,
                        'repository': repository,
                        'image_types_scanned': ['environment', 'model'],
                        'analysis_timestamp': datetime.now().isoformat()
                    }
                }
                save_json(output_file, empty_report)
                logger.info(f"Empty report written to {output_file}")
                sys.exit(0)
            
            logger.info("Finding matching Docker tags...")
            all_ids = list(set(environment_ids + revision_ids))
            deactivated_user_tags = finder.find_matching_tags(all_ids, user_mapping)
            
            if not deactivated_user_tags:
                logger.info("No matching Docker tags found for deactivated user environments")
                # Still create a report with the IDs but no tags
                report = finder.generate_report(environment_ids, revision_ids, [], freed_space_bytes=0)
                save_json(output_file, report)
                logger.info(f"Report written to {output_file}")
                sys.exit(0)
        
        # Handle deletion mode
        if is_delete_mode:
            if not deactivated_user_tags:
                logger.info("No tags to delete")
                sys.exit(0)
            
            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to delete {len(deactivated_user_tags)} tags for private environments owned by deactivated users!")
                logger.warning("This will delete Docker images and clean up MongoDB records.")
                logger.warning("This action cannot be undone.")
                
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)
            
            logger.info(f"\n🗑️  Deleting {len(deactivated_user_tags)} tags...")
            deletion_results = finder.delete_deactivated_user_envs(deactivated_user_tags, environment_ids, revision_ids)
            
            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_deleted = deletion_results.get('docker_images_deleted', 0)
            total_cleaned = deletion_results.get('mongo_records_cleaned', 0)
            logger.info(f"Total Docker images deleted: {total_deleted}")
            logger.info(f"Total MongoDB records cleaned: {total_cleaned}")
            
            logger.info("\n✅ Deactivated user environment deletion completed successfully!")
            
        else:
            # Find mode - calculate freed space and generate report
            logger.info("Calculating freed space...")
            freed_space_bytes = finder.calculate_freed_space(deactivated_user_tags)
            
            logger.info("Generating report...")
            report = finder.generate_report(environment_ids, revision_ids, deactivated_user_tags, freed_space_bytes)
            
            # Save report
            save_json(output_file, report)
            
            # Print summary
            summary = report['summary']
            logger.info("\n" + "=" * 60)
            logger.info("   DISABLED USER ENVIRONMENTS ANALYSIS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total deactivated users with private environments: {summary['total_deactivated_users']}")
            logger.info(f"Total environment IDs: {summary['total_environment_ids']}")
            logger.info(f"Total revision IDs: {summary['total_revision_ids']}")
            logger.info(f"Total matching tags: {summary['total_matching_tags']}")
            logger.info(f"Space that would be freed: {summary['freed_space_gb']:.2f} GB ({summary['freed_space_mb']:.2f} MB)")
            logger.info(f"Tags by image type:")
            for img_type, count in summary['tags_by_image_type'].items():
                logger.info(f"  {img_type}: {count} tags")
            
            logger.info(f"\nDetailed report saved to: {output_file}")
            
            if deactivated_user_tags:
                logger.warning(f"\n⚠️  Found {len(deactivated_user_tags)} tags for private environments owned by deactivated users!")
                logger.info("Review the detailed report to identify affected environments.")
                logger.info("Use --apply flag to delete these images and clean up MongoDB records.")
                logger.info("Or use --apply --input <file> to delete from a saved report.")
            else:
                logger.info("\n✅ No private environments found for deactivated users!")
            
            logger.info("\n✅ Analysis completed successfully!")
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Operation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
