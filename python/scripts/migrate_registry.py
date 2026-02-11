#!/usr/bin/env python3
"""
Migrate Docker images from one registry to another using skopeo copy.

This script automates the documented process for bulk migration of Docker images
between registries, with optional MongoDB metadata updates. Scenarios include:
- Moving from Domino's internal registry to a managed registry (e.g. ECR, GCR/GAR, ACR)
- Moving a Domino instance to a different cloud provider
- Moving system images into an internal registry for air-gapped deployments

Workflow:
1. Verify connectivity to source registry (and MongoDB if --update-mongodb)
2. Discover all repositories and tags in the source registry
3. Copy each image from source to destination using skopeo copy
4. Optionally update MongoDB metadata to reference the new registry
5. Generate a migration summary report

Usage examples:
  # Discover images to migrate (dry-run)
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo

  # Migrate with basic auth to destination
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass --apply

  # Migrate with token auth to destination (e.g. GCR/GAR)
  python migrate_registry.py --dest-registry-url europe-west1-docker.pkg.dev/project/repo --dest-registry-token TOKEN --apply

  # Migrate specific repositories only
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --repos domino-abc123,domino-def456 --apply

  # Migrate and update MongoDB metadata
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass --update-mongodb --apply

  # Force (skip confirmation)
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass --apply --force
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.deletion_base import BaseDeletionScript
from utils.logging_utils import get_logger, setup_logging
from utils.mongo_utils import get_mongo_client
from utils.report_utils import save_json

logger = get_logger(__name__)


class RegistryMigrator(BaseDeletionScript):
    """Handles migration of Docker images between registries."""

    def __init__(
        self,
        registry_url: Optional[str] = None,
        repository: Optional[str] = None,
        dest_registry_url: str = "",
        dest_creds: Optional[str] = None,
        dest_registry_token: Optional[str] = None,
        dest_tls_verify: bool = False,
    ):
        super().__init__(registry_url=registry_url, repository=repository)
        self.dest_registry_url = dest_registry_url
        self.dest_creds = dest_creds
        self.dest_registry_token = dest_registry_token
        self.dest_tls_verify = dest_tls_verify

    def discover_repositories(self, filter_repos: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """Discover all repositories and their tags from the source registry.

        Args:
            filter_repos: If provided, only discover these specific repositories.

        Returns:
            Dict mapping repository name to list of tags.
        """
        repo_tags = {}

        if filter_repos:
            repos = filter_repos
            self.logger.info(f"Using specified repositories: {repos}")
        else:
            # List tags from the base repository to discover sub-repos.
            # The source registry's repository config (e.g. "dominodatalab") is the base.
            # We try common sub-repos used by Domino.
            repos = self._discover_repos_from_registry()

        for repo in repos:
            self.logger.info(f"Listing tags for {repo}...")
            tags = self.skopeo_client.list_tags(repo)
            if tags:
                repo_tags[repo] = tags
                self.logger.info(f"  Found {len(tags)} tags in {repo}")
            else:
                self.logger.info(f"  No tags found in {repo}, skipping")

        return repo_tags

    def _discover_repos_from_registry(self) -> List[str]:
        """Discover available repositories by probing known Domino repo patterns.

        Returns a list of repository paths that have tags in the source registry.
        """
        base_repo = self.repository
        candidate_repos = []

        # Try the base repo directly first
        tags = self.skopeo_client.list_tags(base_repo)
        if tags:
            candidate_repos.append(base_repo)

        # Try common Domino sub-repo patterns
        sub_repos = [
            f"{base_repo}/environment",
            f"{base_repo}/model",
        ]

        for sub_repo in sub_repos:
            tags = self.skopeo_client.list_tags(sub_repo)
            if tags:
                candidate_repos.append(sub_repo)

        if not candidate_repos:
            self.logger.warning(
                f"No repositories found under '{base_repo}'. " "Use --repos to specify repositories explicitly."
            )

        return candidate_repos

    def _get_tags_by_archive_status(self, archived: bool) -> Set[str]:
        """Query MongoDB for Docker tags filtered by archive status.

        Args:
            archived: If True, return tags for archived envs/models.
                      If False, return tags for non-archived envs/models.

        Returns:
            Set of Docker tag strings matching the requested archive status.
        """
        label = "archived" if archived else "unarchived"
        env_query = {"isArchived": True} if archived else {"isArchived": {"$ne": True}}
        model_query = {"isArchived": True} if archived else {"isArchived": {"$ne": True}}

        mongo_client = get_mongo_client()
        tags = set()

        try:
            db = mongo_client[config_manager.get_mongo_db()]

            # 1. Environments -> their revisions -> Docker tags
            env_collection = db["environments_v2"]
            env_ids = [doc["_id"] for doc in env_collection.find(env_query, {"_id": 1})]
            if env_ids:
                rev_collection = db["environment_revisions"]
                for doc in rev_collection.find(
                    {
                        "environmentId": {"$in": env_ids},
                        "metadata.dockerImageName.tag": {"$exists": True, "$ne": None},
                    },
                    {"metadata.dockerImageName.tag": 1},
                ):
                    tag = doc.get("metadata", {}).get("dockerImageName", {}).get("tag")
                    if tag:
                        tags.add(tag)

            self.logger.info(f"  Found {len(tags)} environment tags from {len(env_ids)} {label} environments")

            # 2. Models -> their versions -> Docker tags
            model_collection = db["models"]
            model_ids = [doc["_id"] for doc in model_collection.find(model_query, {"_id": 1})]
            model_tag_count = 0
            if model_ids:
                version_collection = db["model_versions"]
                for doc in version_collection.find(
                    {
                        "modelId.value": {"$in": model_ids},
                        "metadata.builds.slug.image.tag": {"$exists": True, "$ne": None},
                    },
                    {"metadata.builds.slug.image.tag": 1},
                ):
                    builds = doc.get("metadata", {}).get("builds", [])
                    for build in builds:
                        tag = build.get("slug", {}).get("image", {}).get("tag")
                        if tag:
                            tags.add(tag)
                            model_tag_count += 1

            self.logger.info(f"  Found {model_tag_count} model tags from {len(model_ids)} {label} models")

        finally:
            mongo_client.close()

        return tags

    def get_unarchived_tags(self) -> Set[str]:
        """Query MongoDB for Docker tags belonging to non-archived environments and models."""
        return self._get_tags_by_archive_status(archived=False)

    def get_archived_tags(self) -> Set[str]:
        """Query MongoDB for Docker tags belonging to archived environments and models."""
        return self._get_tags_by_archive_status(archived=True)

    def filter_by_archive_status(self, repo_tags: Dict[str, List[str]], archived: bool) -> Dict[str, List[str]]:
        """Filter repo_tags by archive status of their environments/models.

        Args:
            repo_tags: Dict mapping repo name to list of tags (from discover_repositories)
            archived: If True, keep only archived tags. If False, keep only unarchived tags.

        Returns:
            Filtered dict. Repos with no remaining tags are omitted.
        """
        label = "archived" if archived else "unarchived"
        self.logger.info(f"Querying MongoDB for {label} environment/model tags...")
        allowed_tags = self._get_tags_by_archive_status(archived=archived)
        self.logger.info(f"  Total {label} tags: {len(allowed_tags)}")

        filtered = {}
        total_before = 0
        total_after = 0

        for repo, tags in repo_tags.items():
            total_before += len(tags)
            kept = [t for t in tags if t in allowed_tags]
            total_after += len(kept)
            if kept:
                filtered[repo] = kept

        excluded_label = "unarchived" if archived else "archived"
        skipped = total_before - total_after
        self.logger.info(f"  Filtered: {total_after} tags kept, {skipped} {excluded_label} tags excluded")

        return filtered

    def filter_to_unarchived(self, repo_tags: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Filter repo_tags to only include tags belonging to unarchived environments/models."""
        return self.filter_by_archive_status(repo_tags, archived=False)

    def filter_to_archived(self, repo_tags: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Filter repo_tags to only include tags belonging to archived environments/models."""
        return self.filter_by_archive_status(repo_tags, archived=True)

    def copy_repo_tags(
        self,
        repo: str,
        tags: List[str],
        dry_run: bool = True,
    ) -> Dict[str, int]:
        """Copy all tags for a repository from source to destination.

        Args:
            repo: Repository name (e.g. "dominodatalab/environment")
            tags: List of tags to copy
            dry_run: If True, only log what would be copied

        Returns:
            Dict with counts: {"copied": N, "failed": N, "skipped": N}
        """
        results = {"copied": 0, "failed": 0, "skipped": 0}

        for i, tag in enumerate(tags, 1):
            src_ref = f"docker://{self.registry_url}/{repo}:{tag}"
            dest_ref = f"docker://{self.dest_registry_url}/{repo}:{tag}"

            if dry_run:
                self.logger.info(f"  [{i}/{len(tags)}] Would copy {repo}:{tag}")
                results["copied"] += 1
                continue

            self.logger.info(f"  [{i}/{len(tags)}] Copying {repo}:{tag}...")
            success = self.skopeo_client.copy_image(
                src_ref=src_ref,
                dest_ref=dest_ref,
                dest_creds=self.dest_creds,
                dest_registry_token=self.dest_registry_token,
                dest_tls_verify=self.dest_tls_verify,
            )

            if success:
                results["copied"] += 1
            else:
                self.logger.error(f"  Failed to copy {repo}:{tag}")
                results["failed"] += 1

        return results

    def update_mongodb_metadata(
        self,
        old_prefix: str,
        new_prefix: str,
        dry_run: bool = True,
    ) -> Dict[str, Dict[str, int]]:
        """Update MongoDB metadata to reference the new registry.

        Performs string replacement on repository fields across the collections
        used by Domino: builds, environment_revisions, and model_versions.

        Args:
            old_prefix: Current repository prefix to match (e.g. "dominodatalab")
            new_prefix: New repository prefix to replace with (e.g. "my-ecr-repo/dominodatalab")
            dry_run: If True, only count affected documents

        Returns:
            Dict mapping collection name to {"matched": N, "modified": N}
        """
        mongo_client = get_mongo_client()
        results = {}

        try:
            db = mongo_client[config_manager.get_mongo_db()]

            # Define the collections and their repository field paths
            collection_updates = [
                {
                    "collection": "builds",
                    "field": "image.repository",
                },
                {
                    "collection": "environment_revisions",
                    "field": "metadata.dockerImageName.repository",
                },
            ]

            for update_spec in collection_updates:
                coll_name = update_spec["collection"]
                field = update_spec["field"]

                collection = db[coll_name]

                # Find documents where the repository field exists and doesn't already
                # start with the new prefix (idempotent — safe to re-run)
                query = {
                    field: {"$exists": True, "$not": {"$regex": f"^{new_prefix}"}},
                }
                matched = collection.count_documents(query)

                if dry_run:
                    self.logger.info(f"  {coll_name}.{field}: {matched} documents would be updated")
                    results[coll_name] = {"matched": matched, "modified": 0}
                else:
                    # Perform prefix replacement: old_prefix -> new_prefix
                    # For each matching document, we prepend the new prefix path
                    modified = 0
                    for doc in collection.find(query):
                        # Navigate to the field value
                        current_value = self._get_nested_field(doc, field)
                        if current_value and isinstance(current_value, str):
                            new_value = self._replace_prefix(current_value, old_prefix, new_prefix)
                            if new_value != current_value:
                                collection.update_one(
                                    {"_id": doc["_id"]},
                                    {"$set": {field: new_value}},
                                )
                                modified += 1

                    self.logger.info(f"  {coll_name}.{field}: updated {modified}/{matched} documents")
                    results[coll_name] = {"matched": matched, "modified": modified}

            # model_versions requires special handling: builds is an array
            self._update_model_versions(db, old_prefix, new_prefix, dry_run, results)

        finally:
            mongo_client.close()

        return results

    def _update_model_versions(
        self,
        db,
        old_prefix: str,
        new_prefix: str,
        dry_run: bool,
        results: Dict,
    ):
        """Update model_versions collection where builds is an array of objects."""
        collection = db["model_versions"]
        coll_name = "model_versions"

        # Find documents with builds that have repository fields not yet updated
        query = {
            "metadata.builds": {"$exists": True},
            "metadata.builds.slug.image.repository": {
                "$exists": True,
                "$not": {"$regex": f"^{new_prefix}"},
            },
        }
        matched = collection.count_documents(query)

        if dry_run:
            self.logger.info(f"  {coll_name}: {matched} documents would be updated")
            results[coll_name] = {"matched": matched, "modified": 0}
            return

        modified = 0
        for doc in collection.find(query):
            changed = False
            builds = doc.get("metadata", {}).get("builds", [])

            for build in builds:
                repo_val = build.get("slug", {}).get("image", {}).get("repository", "")
                if repo_val and not repo_val.startswith(new_prefix):
                    new_val = self._replace_prefix(repo_val, old_prefix, new_prefix)
                    if new_val != repo_val:
                        build["slug"]["image"]["repository"] = new_val
                        changed = True

            if changed:
                collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"metadata.builds": builds}},
                )
                modified += 1

        self.logger.info(f"  {coll_name}: updated {modified}/{matched} documents")
        results[coll_name] = {"matched": matched, "modified": modified}

    @staticmethod
    def _get_nested_field(doc: dict, field_path: str):
        """Get a value from a nested dict using dot-notation path."""
        parts = field_path.split(".")
        current = doc
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    @staticmethod
    def _replace_prefix(value: str, old_prefix: str, new_prefix: str) -> str:
        """Replace old_prefix with new_prefix in a repository value.

        Handles the common Domino repository naming patterns:
        - "domino-<hash>" -> "<new_prefix>/domino-<hash>"
        - "dom-mdl-<hash>" -> "<new_prefix>/dom-mdl-<hash>"
        - "dominodatalab/environment" -> "<new_prefix>/dominodatalab/environment"
        - "dominodatalab/model" -> "<new_prefix>/dominodatalab/model"
        """
        if value.startswith(f"{new_prefix}/"):
            # Already updated
            return value
        if value.startswith(f"{old_prefix}/") or value.startswith(f"{old_prefix}"):
            return f"{new_prefix}/{value}"
        # For patterns like "domino-" or "dom-mdl-" that don't start with the old_prefix
        return f"{new_prefix}/{value}"


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Migrate Docker images from one registry to another",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Discover images to migrate (dry-run)
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo

  # Migrate with basic auth to destination
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass --apply

  # Migrate with token auth (e.g. GCR/GAR)
  python migrate_registry.py --dest-registry-url europe-west1-docker.pkg.dev/project/repo \\
    --dest-registry-token "$(gcloud auth print-access-token)" --apply

  # Migrate specific repositories only
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --repos domino-abc,domino-def --apply

  # Migrate and update MongoDB metadata
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass \\
    --update-mongodb --apply

  # Resume an interrupted migration
  python migrate_registry.py --dest-registry-url ecr.example.com/my-repo --dest-creds user:pass \\
    --apply --resume
        """,
    )

    parser.add_argument(
        "--dest-registry-url",
        required=True,
        help="Destination registry URL (e.g. ecr.example.com/my-repo, europe-west1-docker.pkg.dev/project/repo)",
    )

    parser.add_argument(
        "--dest-creds",
        help="Destination registry credentials in user:password format",
    )

    parser.add_argument(
        "--dest-registry-token",
        help="Destination registry token (e.g. for GCR/GAR: $(gcloud auth print-access-token))",
    )

    parser.add_argument(
        "--dest-tls-verify",
        action="store_true",
        default=False,
        help="Verify TLS certificates for the destination registry (default: false)",
    )

    parser.add_argument(
        "--repos",
        help="Comma-separated list of specific repositories to migrate (default: auto-discover all)",
    )

    parser.add_argument(
        "--registry-url",
        help="Source registry URL (default: from config)",
    )

    parser.add_argument(
        "--repository",
        help="Source repository name (default: from config)",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually copy images (default: dry-run showing what would be copied)",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt when using --apply",
    )

    parser.add_argument(
        "--update-mongodb",
        action="store_true",
        help="Update MongoDB metadata to reference the new registry after copying",
    )

    parser.add_argument(
        "--old-prefix",
        help="Old repository prefix for MongoDB updates (default: auto-detected from source repository config)",
    )

    parser.add_argument(
        "--new-prefix",
        help="New repository prefix for MongoDB updates (default: derived from --dest-registry-url)",
    )

    parser.add_argument(
        "--output",
        help="Output file for migration report (default: reports/migration-report.json)",
    )

    archive_group = parser.add_mutually_exclusive_group()
    archive_group.add_argument(
        "--unarchived",
        action="store_true",
        help="Only migrate images belonging to non-archived environments and models (requires MongoDB access)",
    )
    archive_group.add_argument(
        "--archived",
        action="store_true",
        help="Only migrate images belonging to archived environments and models (requires MongoDB access). "
        "Useful as a reversible alternative to deleting archived images.",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous checkpoint if migration was interrupted",
    )

    parser.add_argument(
        "--operation-id",
        help="Unique identifier for this migration (used for checkpoint management)",
    )

    return parser.parse_args()


def main():
    setup_logging()
    args = parse_arguments()

    # Validate arguments
    if not args.dest_creds and not args.dest_registry_token:
        logger.warning(
            "No destination credentials provided (--dest-creds or --dest-registry-token). "
            "Skopeo will attempt unauthenticated access to the destination registry."
        )

    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    output_file = args.output or str(Path(config_manager.get_output_dir()) / "migration-report.json")
    dry_run = not args.apply

    try:
        # Print mode banner
        logger.info("=" * 60)
        if dry_run:
            logger.info("   REGISTRY MIGRATION - DRY RUN MODE (default)")
            logger.info("   No images will be copied. Use --apply to execute.")
        else:
            logger.info("   REGISTRY MIGRATION - APPLY MODE")
            logger.warning("   Images WILL be copied to the destination registry!")
        logger.info("=" * 60)
        logger.info(f"Source registry:      {registry_url}")
        logger.info(f"Source repository:    {repository}")
        logger.info(f"Destination registry: {args.dest_registry_url}")
        if args.update_mongodb:
            logger.info("MongoDB update:       ENABLED")
        if args.unarchived:
            logger.info("Unarchived filter:    ENABLED (only non-archived envs/models)")
        if args.archived:
            logger.info("Archived filter:      ENABLED (only archived envs/models)")
        logger.info("")

        # Create migrator
        migrator = RegistryMigrator(
            registry_url=registry_url,
            repository=repository,
            dest_registry_url=args.dest_registry_url,
            dest_creds=args.dest_creds,
            dest_registry_token=args.dest_registry_token,
            dest_tls_verify=args.dest_tls_verify,
        )

        # Health checks (MongoDB needed for --update-mongodb, --unarchived, or --archived)
        needs_mongo = args.update_mongodb or args.unarchived or args.archived
        if not migrator.run_health_checks(skip_optional=not needs_mongo):
            logger.error("Health checks failed, aborting migration")
            sys.exit(1)

        # Discovery
        logger.info("Discovering repositories and tags...")
        filter_repos = args.repos.split(",") if args.repos else None
        repo_tags = migrator.discover_repositories(filter_repos=filter_repos)

        if not repo_tags:
            logger.info("No repositories with tags found. Nothing to migrate.")
            sys.exit(0)

        # Apply archive status filter
        if args.unarchived:
            repo_tags = migrator.filter_to_unarchived(repo_tags)
            if not repo_tags:
                logger.info("No unarchived tags found after filtering. Nothing to migrate.")
                sys.exit(0)
        elif args.archived:
            repo_tags = migrator.filter_to_archived(repo_tags)
            if not repo_tags:
                logger.info("No archived tags found after filtering. Nothing to migrate.")
                sys.exit(0)

        total_repos = len(repo_tags)
        total_tags = sum(len(tags) for tags in repo_tags.values())

        logger.info("")
        logger.info(f"Found {total_repos} repositories with {total_tags} total tags")
        for repo, tags in repo_tags.items():
            logger.info(f"  {repo}: {len(tags)} tags")
        logger.info("")

        # Load checkpoint for resume
        operation_id = args.operation_id or "migrate_registry"
        completed_repos = set()
        if args.resume:
            checkpoint = migrator.checkpoint_manager.load_checkpoint("migrate_registry", operation_id=operation_id)
            if checkpoint:
                completed_repos = set(checkpoint.completed_items)
                logger.info(f"Resuming: {len(completed_repos)} repositories already completed")

        # Filter out completed repos
        remaining_repos = {r: t for r, t in repo_tags.items() if r not in completed_repos}
        if not remaining_repos:
            logger.info("All repositories already migrated (from checkpoint). Nothing to do.")
        else:
            if completed_repos:
                logger.info(f"Skipping {len(completed_repos)} completed repos, " f"{len(remaining_repos)} remaining")

            # Confirmation
            remaining_tags = sum(len(tags) for tags in remaining_repos.values())
            if not dry_run and not args.force:
                if not migrator.confirm_deletion(
                    remaining_tags, f"images across {len(remaining_repos)} repositories to copy"
                ):
                    logger.info("Operation cancelled by user")
                    sys.exit(0)

            # Copy loop
            overall_results = {"copied": 0, "failed": 0, "skipped": 0}

            for repo_idx, (repo, tags) in enumerate(remaining_repos.items(), 1):
                logger.info(
                    f"[repo {repo_idx + len(completed_repos)}/{total_repos}] " f"Migrating {repo} ({len(tags)} tags)..."
                )

                results = migrator.copy_repo_tags(repo, tags, dry_run=dry_run)

                for key in overall_results:
                    overall_results[key] += results[key]

                # Save checkpoint after each repo (only in apply mode)
                if not dry_run:
                    completed_repos.add(repo)
                    migrator.checkpoint_manager.save_checkpoint(
                        operation_type="migrate_registry",
                        completed_items=list(completed_repos),
                        total_items=total_repos,
                        failed_items=[],
                        metadata={
                            "dest_registry_url": args.dest_registry_url,
                            "results": overall_results,
                        },
                        operation_id=operation_id,
                    )

        # MongoDB update
        mongo_results = {}
        if args.update_mongodb:
            old_prefix = args.old_prefix or repository
            new_prefix = (
                args.new_prefix or args.dest_registry_url.split("/", 1)[-1]
                if "/" in args.dest_registry_url
                else args.dest_registry_url
            )

            logger.info("")
            logger.info("=" * 60)
            if dry_run:
                logger.info("   MONGODB UPDATE - DRY RUN")
            else:
                logger.info("   MONGODB UPDATE - APPLYING CHANGES")
            logger.info("=" * 60)
            logger.info(f"Old prefix: {old_prefix}")
            logger.info(f"New prefix: {new_prefix}")
            logger.info("")

            mongo_results = migrator.update_mongodb_metadata(
                old_prefix=old_prefix,
                new_prefix=new_prefix,
                dry_run=dry_run,
            )

        # Generate report
        report = {
            "summary": {
                "total_repositories": total_repos,
                "total_tags": total_tags,
                "images_copied": overall_results.get("copied", 0),
                "images_failed": overall_results.get("failed", 0),
                "dry_run": dry_run,
            },
            "repositories": {repo: len(tags) for repo, tags in repo_tags.items()},
            "mongodb_updates": mongo_results,
            "metadata": {
                "source_registry": registry_url,
                "source_repository": repository,
                "dest_registry": args.dest_registry_url,
                "update_mongodb": args.update_mongodb,
                "timestamp": datetime.now().isoformat(),
            },
        }

        save_json(output_file, report)

        # Print summary
        logger.info("")
        logger.info("=" * 60)
        mode = "DRY RUN " if dry_run else ""
        logger.info(f"   {mode}MIGRATION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Repositories: {total_repos}")
        logger.info(f"Total tags:   {total_tags}")
        action = "Would copy" if dry_run else "Copied"
        logger.info(f"{action}:      {overall_results.get('copied', 0)}")
        if overall_results.get("failed", 0) > 0:
            logger.info(f"Failed:       {overall_results['failed']}")

        if mongo_results:
            logger.info("")
            logger.info("MongoDB updates:")
            for coll, counts in mongo_results.items():
                action = "would update" if dry_run else "updated"
                logger.info(f"  {coll}: {action} {counts.get('matched', 0)} documents")

        logger.info(f"\nReport saved to: {output_file}")

        if dry_run:
            logger.info("")
            logger.info("No changes were made. Use --apply to execute the migration.")

        # Clean up checkpoint on successful completion (apply mode only)
        if not dry_run and overall_results.get("failed", 0) == 0:
            migrator.checkpoint_manager.delete_checkpoint("migrate_registry", operation_id=operation_id)

    except KeyboardInterrupt:
        logger.warning("\nMigration interrupted by user")
        logger.info("Use --resume to continue from where you left off")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\nMigration failed: {e}")
        from utils.logging_utils import log_exception

        log_exception(logger, "Error in migration", exc_info=e)
        sys.exit(1)


if __name__ == "__main__":
    main()
