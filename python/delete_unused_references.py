#!/usr/bin/env python3
"""
Find and optionally delete unused Docker image references in MongoDB collections.

This script queries MongoDB collections for Docker image references and checks
which ones no longer exist in the Docker registry, helping identify orphaned
database records. Can optionally delete the MongoDB records containing unused references.

Workflow:
- Query MongoDB collections for documents containing Docker image references
- Extract image tags from various fields (metadata.dockerImageName.tag, etc.)
- Check Docker registry to see which images actually exist
- Generate a report of unused references (Mongo has them but Docker does not)
- Optionally delete MongoDB records containing unused references (with --apply)

Usage examples:
  # Find unused references (dry-run)
  python delete_unused_references.py --registry-url docker-registry:5000 --repository dominodatalab
  
  # Delete unused references directly
  python delete_unused_references.py --apply
  
  # Delete unused references from pre-generated file
  python delete_unused_references.py --apply --input unused-refs.json
"""

import argparse
import json
import sys

from bson import ObjectId
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Set, Tuple

from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json

logger = get_logger(__name__)


@dataclass
class ImageReference:
    """Data class for image reference information"""
    tag: str
    repository: str
    full_image: str
    collection: str
    document_id: str
    field_path: str
    context: Dict


class UnusedReferencesFinder:
    """Main class for finding unused Docker image references"""
    
    def __init__(self, registry_url: str, repository: str):
        self.registry_url = registry_url
        self.repository = repository
        self.skopeo_client = SkopeoClient(config_manager, use_pod=config_manager.get_skopeo_use_pod())
        self.logger = get_logger(__name__)
        
        # Image type mappings for registry queries
        self.image_types = ['environment', 'model']
        
        # Collections and their image reference field patterns
        self.collection_patterns = {
            'environment_revisions': [
                'metadata.dockerImageName.tag',
                'metadata.dockerImageName.repository'
            ],
            'model_versions': [
                'metadata.builds.slug.image.tag',
                'metadata.builds.slug.image.repository'
            ]
        }
    
    def extract_image_references_from_collection(self, db, collection_name: str) -> List[ImageReference]:
        """Extract all image references from a MongoDB collection"""
        references = []
        collection = db[collection_name]
        
        if collection_name not in self.collection_patterns:
            self.logger.warning(f"No patterns defined for collection '{collection_name}'")
            return references
        
        # Get field patterns for this collection
        field_patterns = self.collection_patterns[collection_name]
        
        try:
            # Query all documents that might contain image references
            # Use $or to match documents with any of the field patterns
            or_conditions = []
            for pattern in field_patterns:
                # Use dot notation for nested fields in MongoDB queries
                or_conditions.append({pattern: {"$exists": True}})
            
            query = {"$or": or_conditions} if len(or_conditions) > 1 else or_conditions[0]
            
            self.logger.info(f"Querying {collection_name} for image references...")
            self.logger.debug(f"Query: {query}")
            cursor = collection.find(query)
            
            for doc in cursor:
                doc_id = str(doc.get('_id', ''))
                
                # Extract image references from each pattern
                # Only process .tag patterns to avoid duplicates (repository field is derived from tag field)
                for pattern in field_patterns:
                    if not pattern.endswith('.tag'):
                        continue  # Skip non-tag patterns
                    
                    tag_field = pattern
                    repo_field = pattern[:-4] + '.repository'  # Replace '.tag' with '.repository'
                    
                    # Get tag value
                    tag_value = self._get_nested_value(doc, tag_field)
                    if not tag_value:
                        continue
                    
                    # Get repository value
                    repo_value = self._get_nested_value(doc, repo_field)
                    if not repo_value:
                        repo_value = self.repository  # Use default repository
                    
                    # Create full image reference
                    if repo_value.startswith(self.registry_url):
                        full_image = f"{repo_value}:{tag_value}"
                        repository = repo_value.replace(f"{self.registry_url}/", "")
                    else:
                        full_image = f"{self.registry_url}/{repo_value}:{tag_value}"
                        repository = repo_value
                    
                    # Create context information
                    context = {
                        'collection': collection_name,
                        'document_id': doc_id,
                        'field_path': pattern,
                        'tag_field_value': tag_value,
                        'repo_field_value': repo_value
                    }
                    
                    references.append(ImageReference(
                        tag=tag_value,
                        repository=repository,
                        full_image=full_image,
                        collection=collection_name,
                        document_id=doc_id,
                        field_path=pattern,
                        context=context
                    ))
            
            self.logger.info(f"Found {len(references)} image references in {collection_name}")
            
        except Exception as e:
            self.logger.error(f"Error querying collection {collection_name}: {e}")
        
        return references
    
    def _get_nested_value(self, doc: Dict, field_path: str):
        """Get a nested value from a document using dot notation"""
        try:
            value = doc
            for key in field_path.split('.'):
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return None
            return value
        except (TypeError, KeyError):
            return None
    
    def get_existing_images_from_registry(self) -> Set[str]:
        """Get all existing images from the Docker registry"""
        existing_images = set()
        
        for image_type in self.image_types:
            try:
                self.logger.info(f"Listing existing tags for {image_type} images...")
                tags = self.skopeo_client.list_tags(f"{self.repository}/{image_type}")
                
                for tag in tags:
                    full_image = f"{self.registry_url}/{self.repository}/{image_type}:{tag}"
                    existing_images.add(full_image)
                    existing_images.add(tag)  # Also add just the tag for flexible matching
                
                self.logger.info(f"Found {len(tags)} existing {image_type} tags")
                
            except Exception as e:
                self.logger.error(f"Error listing {image_type} tags: {e}")
        
        return existing_images
    
    def find_unused_references(self) -> Tuple[List[ImageReference], List[ImageReference]]:
        """Find unused image references by comparing Mongo with Docker registry"""
        mongo_client = get_mongo_client()
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            
            # Extract all image references from MongoDB
            all_references = []
            for collection_name in self.collection_patterns.keys():
                references = self.extract_image_references_from_collection(db, collection_name)
                all_references.extend(references)
            
            if not all_references:
                self.logger.warning("No image references found in MongoDB collections")
                return [], []
            
            self.logger.info(f"Total image references found in MongoDB: {len(all_references)}")
            
            # Get existing images from Docker registry
            existing_images = self.get_existing_images_from_registry()
            self.logger.info(f"Total existing images in Docker registry: {len(existing_images)}")
            
            # Find unused references
            unused_references = []
            used_references = []
            
            for ref in all_references:
                # Check if the image exists in registry (flexible matching)
                image_exists = (
                    ref.full_image in existing_images or
                    ref.tag in existing_images or
                    f"{ref.repository}:{ref.tag}" in existing_images
                )
                
                if image_exists:
                    used_references.append(ref)
                else:
                    unused_references.append(ref)
            
            self.logger.info(f"Found {len(unused_references)} unused references and {len(used_references)} used references")
            
            return unused_references, used_references
            
        finally:
            mongo_client.close()
    
    def generate_report(self, unused_references: List[ImageReference], used_references: List[ImageReference]) -> Dict:
        """Generate a comprehensive report of unused references"""
        
        # Group by collection
        unused_by_collection = defaultdict(list)
        used_by_collection = defaultdict(list)
        
        for ref in unused_references:
            unused_by_collection[ref.collection].append(ref)
        
        for ref in used_references:
            used_by_collection[ref.collection].append(ref)
        
        # Create summary statistics
        summary = {
            'total_references_found': len(unused_references) + len(used_references),
            'unused_references': len(unused_references),
            'used_references': len(used_references),
            'unused_percentage': round((len(unused_references) / (len(unused_references) + len(used_references)) * 100), 2) if (unused_references or used_references) else 0,
            'collections_analyzed': list(self.collection_patterns.keys()),
            'image_types_checked': self.image_types
        }
        
        # Create detailed breakdown
        collection_summary = {}
        for collection in self.collection_patterns.keys():
            unused_count = len(unused_by_collection.get(collection, []))
            used_count = len(used_by_collection.get(collection, []))
            total_count = unused_count + used_count
            
            collection_summary[collection] = {
                'total_references': total_count,
                'unused_references': unused_count,
                'used_references': used_count,
                'unused_percentage': round((unused_count / total_count * 100), 2) if total_count > 0 else 0
            }
        
        # Prepare detailed data
        unused_details = []
        for ref in unused_references:
            unused_details.append({
                'tag': ref.tag,
                'repository': ref.repository,
                'full_image': ref.full_image,
                'collection': ref.collection,
                'document_id': ref.document_id,
                'field_path': ref.field_path
            })
        
        used_details = []
        for ref in used_references:
            used_details.append({
                'tag': ref.tag,
                'repository': ref.repository,
                'full_image': ref.full_image,
                'collection': ref.collection,
                'document_id': ref.document_id,
                'field_path': ref.field_path
            })
        
        report = {
            'summary': summary,
            'collection_summary': collection_summary,
            'unused_references': unused_details,
            'used_references': used_details,
            'metadata': {
                'registry_url': self.registry_url,
                'repository': self.repository,
                'analysis_timestamp': datetime.now().isoformat()
            }
        }
        
        return report
    
    def delete_unused_references(self, unused_references: List[ImageReference]) -> Dict[str, int]:
        """Delete MongoDB records containing unused references"""
        if not unused_references:
            self.logger.info("No unused references to delete")
            return {}
        
        mongo_client = get_mongo_client()
        deletion_results = {}
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            
            # Group references by collection for batch deletion
            references_by_collection = {}
            for ref in unused_references:
                if ref.collection not in references_by_collection:
                    references_by_collection[ref.collection] = []
                references_by_collection[ref.collection].append(ref)
            
            # Delete from each collection
            for collection_name, refs in references_by_collection.items():
                collection = db[collection_name]
                deleted_count = 0
                
                self.logger.info(f"Deleting {len(refs)} unused references from collection '{collection_name}'...")
                
                for ref in refs:
                    try:
                        # Build query based on the field path and document ID
                        try:
                            doc_id = ObjectId(ref.document_id)
                        except (TypeError, ValueError) as e:
                            self.logger.error(f"Invalid document ID {ref.document_id}: {e}")
                            continue
                        
                        query = {"_id": doc_id}
                        
                        # For safety, also match on the specific field to ensure we're deleting the right record
                        if ref.tag:
                            # Add the tag field to the query for extra safety
                            field_parts = ref.field_path.split('.')
                            if len(field_parts) >= 2:
                                nested_query = {}
                                current_level = nested_query
                                for part in field_parts[:-1]:
                                    current_level[part] = {}
                                    current_level = current_level[part]
                                current_level[field_parts[-1]] = ref.tag
                                query.update(nested_query)
                        
                        # Delete the document
                        result = collection.delete_one(query)
                        if result.deleted_count > 0:
                            deleted_count += 1
                            self.logger.debug(f"Deleted document {ref.document_id} from {collection_name}")
                        else:
                            self.logger.warning(f"Document {ref.document_id} not found in {collection_name}")
                            
                    except Exception as e:
                        self.logger.error(f"Error deleting document {ref.document_id} from {collection_name}: {e}")
                
                deletion_results[collection_name] = deleted_count
                self.logger.info(f"Deleted {deleted_count}/{len(refs)} documents from {collection_name}")
            
            total_deleted = sum(deletion_results.values())
            self.logger.info(f"Total documents deleted: {total_deleted}")
            
            return deletion_results
            
        finally:
            mongo_client.close()
    
    def load_unused_references_from_file(self, file_path: str) -> List[ImageReference]:
        """Load unused references from a pre-generated report file"""
        try:
            with open(file_path, 'r') as f:
                report = json.load(f)
            
            unused_refs = []
            for ref_data in report.get('unused_references', []):
                # Reconstruct ImageReference object from the report data
                # Context is optional (for backward compatibility with old reports)
                context = ref_data.get('context', {})
                
                ref = ImageReference(
                    tag=ref_data['tag'],
                    repository=ref_data['repository'],
                    full_image=ref_data['full_image'],
                    collection=ref_data['collection'],
                    document_id=ref_data['document_id'],
                    field_path=ref_data['field_path'],
                    context=context
                )
                unused_refs.append(ref)
            
            self.logger.info(f"Loaded {len(unused_refs)} unused references from {file_path}")
            return unused_refs
            
        except Exception as e:
            self.logger.error(f"Error loading unused references from {file_path}: {e}")
            raise
    
    def delete_by_tags_or_objectids(self, tags_or_ids: List[str], collection_name: str, dry_run: bool = True) -> int:
        """Delete MongoDB records by exact tag match or ObjectID prefix.
        
        This method provides simple tag/ObjectID-based deletion, similar to mongo_cleanup.py.
        
        Args:
            tags_or_ids: List of full tags or 24-char ObjectIDs
            collection_name: MongoDB collection to clean up
            dry_run: If True, only count matches without deleting
        
        Returns:
            Number of documents deleted (or would be deleted in dry-run mode)
        """
        if not tags_or_ids:
            self.logger.info("No tags or ObjectIDs provided")
            return 0
        
        mongo_client = get_mongo_client()
        total_affected = 0
        
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            collection = db[collection_name]
            
            for value in tags_or_ids:
                # Determine if this is an ObjectID or a tag
                is_objectid = len(value) == 24
                if is_objectid:
                    try:
                        int(value, 16)  # Verify it's a valid hex string
                        # Match tags that start with ObjectID-
                        query = {"metadata.dockerImageName.tag": {"$regex": f"^{value}-"}}
                        mode = "objectId"
                    except ValueError:
                        # Not a valid hex, treat as tag
                        query = {"metadata.dockerImageName.tag": value}
                        mode = "tag"
                else:
                    # Exact tag match
                    query = {"metadata.dockerImageName.tag": value}
                    mode = "tag"
                
                if dry_run:
                    count = collection.count_documents(query)
                    if count > 0:
                        self.logger.info(f"Would delete {count} documents for {mode}='{value}' in {collection_name}")
                    total_affected += count
                else:
                    result = collection.delete_many(query)
                    if result.deleted_count > 0:
                        self.logger.info(f"Deleted {result.deleted_count} documents for {mode}='{value}' in {collection_name}")
                    total_affected += result.deleted_count
            
            return total_affected
            
        finally:
            mongo_client.close()
    
    def load_tags_from_simple_file(self, file_path: str) -> List[str]:
        """Load tags or ObjectIDs from a simple text file.
        
        Accepts file formats:
        - <24-char ObjectID> [other columns ignored]
        - Full tag: repo/image:tag or tag
        - Lines starting with # are ignored
        
        Returns:
            List of tags or ObjectIDs (first token from each line)
        """
        tags_or_ids = []
        
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    first_token = line.split()[0]
                    tags_or_ids.append(first_token)
            
            self.logger.info(f"Loaded {len(tags_or_ids)} tags/ObjectIDs from {file_path}")
            return tags_or_ids
            
        except Exception as e:
            self.logger.error(f"Error loading tags from {file_path}: {e}")
            raise


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Find unused Docker image references in MongoDB collections",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find unused references (dry-run)
  python delete_unused_references.py

  # Override registry settings
  python delete_unused_references.py --registry-url registry.example.com --repository my-repo

  # Custom output file
  python delete_unused_references.py --output unused-refs.json

  # Delete unused references directly (requires confirmation)
  python delete_unused_references.py --apply

  # Delete unused references from pre-generated file
  python delete_unused_references.py --apply --input unused-refs.json

  # Force deletion without confirmation
  python delete_unused_references.py --apply --force
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
        help='Output file path (default: reports/unused-references.json)'
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete MongoDB records containing unused references (default: dry-run)'
    )
    
    parser.add_argument(
        '--input',
        help='Input file containing pre-generated unused references to delete'
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
    output_file = args.output or config_manager.get_unused_references_report_path()
    
    try:
        # Determine operation mode
        is_delete_mode = args.apply
        use_input_file = args.input is not None
        
        logger.info("=" * 60)
        if is_delete_mode:
            logger.info("   Deleting unused Docker image references")
        else:
            logger.info("   Finding unused Docker image references")
        logger.info("=" * 60)
        logger.info(f"Registry URL: {registry_url}")
        logger.info(f"Repository: {repository}")
        
        if use_input_file:
            logger.info(f"Input file: {args.input}")
        else:
            logger.info(f"Output file: {output_file}")
        
        # Create finder
        finder = UnusedReferencesFinder(registry_url, repository)
        
        # Handle different operation modes
        if use_input_file:
            # Mode 1: Delete from pre-generated file
            logger.info(f"Loading unused references from {args.input}...")
            unused_refs = finder.load_unused_references_from_file(args.input)
            used_refs = []  # Not relevant for deletion mode
            
            if not unused_refs:
                logger.warning(f"No unused references found in {args.input}")
                sys.exit(0)
                
        else:
            # Mode 2: Find unused references (and optionally delete them)
            logger.info("Extracting image references from MongoDB collections...")
            unused_refs, used_refs = finder.find_unused_references()
            
            if not unused_refs and not used_refs:
                logger.warning("No image references found in MongoDB collections")
                sys.exit(0)
        
        # Handle deletion mode
        if is_delete_mode:
            if not unused_refs:
                logger.info("No unused references to delete")
                sys.exit(0)
            
            # Confirmation prompt (unless --force)
            if not args.force:
                logger.warning(f"\n⚠️  WARNING: About to delete {len(unused_refs)} MongoDB records containing unused Docker image references!")
                logger.warning("This action cannot be undone.")
                
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ['yes', 'y']:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)
            
            logger.info(f"\n🗑️  Deleting {len(unused_refs)} MongoDB records...")
            deletion_results = finder.delete_unused_references(unused_refs)
            
            # Print deletion summary
            logger.info("\n" + "=" * 60)
            logger.info("   DELETION SUMMARY")
            logger.info("=" * 60)
            total_deleted = sum(deletion_results.values())
            logger.info(f"Total records deleted: {total_deleted}")
            
            for collection, count in deletion_results.items():
                logger.info(f"  {collection}: {count} records deleted")
            
            logger.info("\n✅ Unused references deletion completed successfully!")
            
        else:
            # Find mode - generate and save report
            logger.info("Generating report...")
            report = finder.generate_report(unused_refs, used_refs)
            
            # Save report
            save_json(output_file, report)
            
            # Print summary
            summary = report['summary']
            logger.info("\n" + "=" * 60)
            logger.info("   UNUSED REFERENCES ANALYSIS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total references found: {summary['total_references_found']}")
            logger.info(f"Unused references: {summary['unused_references']}")
            logger.info(f"Used references: {summary['used_references']}")
            logger.info(f"Unused percentage: {summary['unused_percentage']}%")
            
            logger.info("\nCollection breakdown:")
            for collection, stats in report['collection_summary'].items():
                logger.info(f"  {collection}: {stats['unused_references']}/{stats['total_references']} unused ({stats['unused_percentage']}%)")
            
            logger.info(f"\nDetailed report saved to: {output_file}")
            
            if unused_refs:
                logger.warning(f"\n⚠️  Found {len(unused_refs)} unused references that may need cleanup!")
                logger.info("Review the detailed report to identify which MongoDB records reference non-existent Docker images.")
                logger.info("Use --apply flag to delete these records, or --apply --input <file> to delete from a saved report.")
            else:
                logger.info("\n✅ No unused references found - all MongoDB image references exist in Docker registry!")
            
            logger.info("\n✅ Unused references analysis completed successfully!")
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Operation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
