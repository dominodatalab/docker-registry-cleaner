#!/usr/bin/env python3
"""
Find and optionally delete old environment revisions, keeping only the most recent N per environment.

This script queries MongoDB to find all environment revisions grouped by parent environment,
identifies revisions that are not among the N most recent, and optionally deletes their
Docker images from the registry.

Before any deletion, a real-time usage check is performed to ensure no revision being
deleted is currently in use by any workspace, run, model, scheduled job, project, or app version.

Workflow:
- Query MongoDB environment_revisions, grouped by environmentId
- Sort revisions within each group by creation time (ObjectId order)
- Mark all but the latest N revisions per environment as candidates for deletion
- Filter out revisions with no Docker image (e.g., failed builds)
- Filter out revisions that are cloned from by a kept revision (build chain protection)
- Perform a real-time usage check and skip any revision currently in use
- Generate a report of old revisions
- Optionally delete Docker images and clean up MongoDB records (with --apply)

Usage examples:
  # Dry-run: find old revisions (default: keep 5 most recent per environment)
  python delete_old_revisions.py

  # Keep only 3 revisions per environment
  python delete_old_revisions.py --keep-revisions 3

  # Delete old revisions (requires confirmation)
  python delete_old_revisions.py --apply

  # Delete without confirmation prompt
  python delete_old_revisions.py --apply --force

  # Also remove MongoDB records after Docker image deletion
  python delete_old_revisions.py --apply --mongo-cleanup

  # Generate fresh usage reports before analysis
  python delete_old_revisions.py --generate-reports

  # Restrict to specific environments from a file
  python delete_old_revisions.py --input my-envs.txt

  # Override registry settings
  python delete_old_revisions.py --registry-url registry.example.com --repository my-repo
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from bson import ObjectId

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.deletion_base import BaseDeletionScript
from utils.image_data_analysis import ImageAnalyzer
from utils.image_usage import ImageUsageService
from utils.logging_utils import get_logger, setup_logging
from utils.mongo_utils import get_mongo_client
from utils.object_id_utils import read_object_ids_from_file
from utils.report_utils import ensure_mongodb_reports, save_json

logger = get_logger(__name__)

DEFAULT_KEEP_REVISIONS = 5


@dataclass
class OldRevisionInfo:
    """Data class for an environment revision scheduled for deletion."""

    revision_id: str
    environment_id: str
    environment_name: str
    docker_tag: str
    full_image: str
    image_type: str = "environment"
    size_bytes: int = 0


class OldRevisionCleaner(BaseDeletionScript):
    """Find and delete environment revisions beyond the N most recent per environment."""

    def __init__(
        self,
        registry_url: str,
        repository: str,
        keep_revisions: int = DEFAULT_KEEP_REVISIONS,
        enable_docker_deletion: bool = False,
        registry_statefulset: Optional[str] = None,
    ):
        super().__init__(
            registry_url=registry_url,
            repository=repository,
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset=registry_statefulset,
        )
        self.keep_revisions = keep_revisions

    def generate_required_reports(self) -> None:
        """Generate required metadata reports."""
        self.logger.info("Generating required metadata reports...")
        ensure_mongodb_reports()

    def find_old_revisions(self, environment_ids: Optional[Set[str]] = None) -> List[OldRevisionInfo]:
        """Find environment revisions that are not among the N most recent per environment.

        Queries MongoDB for all revisions, groups by environment, then identifies
        the older ones to delete (all but the latest `keep_revisions` per environment).
        Only revisions with a built Docker image are included.

        Args:
            environment_ids: Optional set of environment ObjectId strings to restrict
                processing to. When provided, only those environments are examined.

        Returns:
            List of OldRevisionInfo objects for revisions to be deleted.
        """
        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            revisions_coll = db["environment_revisions"]
            environments_coll = db["environments_v2"]

            # Build environment name lookup (include archived - we still want names)
            env_names: Dict[str, str] = {}
            for doc in environments_coll.find({}, {"_id": 1, "name": 1}):
                env_names[str(doc["_id"])] = doc.get("name", "")

            # Fetch all successfully built revisions, sorted oldest-first by ObjectId.
            # Exclude revisions where metadata.isBuilt=False (failed builds have no
            # corresponding Docker image in the registry).
            query: dict = {"metadata.isBuilt": {"$ne": False}}
            if environment_ids:
                query["environmentId"] = {"$in": [ObjectId(eid) for eid in environment_ids if len(eid) == 24]}
                self.logger.info(f"Filtering to {len(environment_ids)} environment(s) from --input")

            all_revisions = list(
                revisions_coll.find(
                    query,
                    {
                        "_id": 1,
                        "environmentId": 1,
                        "metadata.dockerImageName.tag": 1,
                        "clonedEnvironmentRevisionId": 1,
                    },
                    sort=[("_id", 1)],
                )
            )

            self.logger.info(f"Found {len(all_revisions)} built environment revisions in MongoDB")

            # Group by environment
            by_environment: Dict[str, List[dict]] = {}
            for rev in all_revisions:
                env_id = str(rev.get("environmentId", ""))
                if not env_id:
                    continue
                by_environment.setdefault(env_id, []).append(rev)

            self.logger.info(
                f"Grouped into {len(by_environment)} environments "
                f"(keeping {self.keep_revisions} most recent per environment)"
            )

            # Identify old revisions (all but the latest N per environment)
            old_revisions: List[OldRevisionInfo] = []

            for env_id, revisions in by_environment.items():
                total = len(revisions)
                if total <= self.keep_revisions:
                    continue

                # revisions is sorted oldest-first; the first (total - keep_revisions) are old
                to_delete = revisions[: total - self.keep_revisions]
                env_name = env_names.get(env_id, "")

                for rev in to_delete:
                    rev_id = str(rev["_id"])
                    docker_tag = rev.get("metadata", {}).get("dockerImageName", {}).get("tag", "")

                    if not docker_tag:
                        self.logger.debug(f"Revision {rev_id} has no Docker tag, skipping")
                        continue

                    full_image = f"{self.registry_url}/{self.repository}/environment:{docker_tag}"
                    old_revisions.append(
                        OldRevisionInfo(
                            revision_id=rev_id,
                            environment_id=env_id,
                            environment_name=env_name,
                            docker_tag=docker_tag,
                            full_image=full_image,
                        )
                    )

            self.logger.info(f"Found {len(old_revisions)} old revision(s) in MongoDB eligible for deletion")

            # Filter to only revisions whose Docker image actually exists in the registry.
            # Revisions may reference images that were already deleted or never pushed.
            self.logger.info("Checking registry for existing tags...")
            existing_tags = set(self.skopeo_client.list_tags(f"{self.repository}/environment"))
            before = len(old_revisions)
            old_revisions = [r for r in old_revisions if r.docker_tag in existing_tags]
            filtered = before - len(old_revisions)
            if filtered:
                self.logger.info(
                    f"Filtered out {filtered} revision(s) whose Docker image no longer exists in the registry"
                )
            self.logger.info(f"Found {len(old_revisions)} old revision(s) eligible for deletion")
            return old_revisions

        finally:
            mongo_client.close()

    def _filter_cloned_by_kept_revisions(self, old_revisions: List[OldRevisionInfo]) -> List[OldRevisionInfo]:
        """Remove revisions that a kept revision was cloned from.

        If a kept (recent) revision has a clonedEnvironmentRevisionId pointing at one of our
        deletion candidates, that candidate must be preserved to maintain the build chain.

        Args:
            old_revisions: Candidate revisions for deletion.

        Returns:
            Filtered list with build-chain-protected revisions removed.
        """
        if not old_revisions:
            return old_revisions

        old_rev_ids: Set[str] = {r.revision_id for r in old_revisions}
        old_oids = [ObjectId(rid) for rid in old_rev_ids if len(rid) == 24]

        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            revisions_coll = db["environment_revisions"]

            # Find kept revisions (not in old_rev_ids) that clone an old revision
            protected_ids: Set[str] = set()
            cursor = revisions_coll.find(
                {
                    "_id": {"$nin": old_oids},
                    "clonedEnvironmentRevisionId": {"$in": old_oids},
                },
                {"clonedEnvironmentRevisionId": 1},
            )
            for doc in cursor:
                cloned_from = doc.get("clonedEnvironmentRevisionId")
                if cloned_from is not None:
                    protected_ids.add(str(cloned_from))

            if not protected_ids:
                return old_revisions

            filtered = [r for r in old_revisions if r.revision_id not in protected_ids]
            skipped = len(old_revisions) - len(filtered)
            self.logger.info(
                f"Skipping {skipped} old revision(s) that are cloned from by a kept revision "
                "(build chain protection)"
            )
            return filtered

        finally:
            mongo_client.close()

    def delete_old_revisions(
        self,
        old_revisions: List[OldRevisionInfo],
        mongo_cleanup: bool = False,
    ) -> Dict[str, int]:
        """Delete Docker images for old revisions, skipping any currently in use.

        Performs a real-time usage check right before deletion to catch any revisions
        that became in-use after the initial analysis (new runs, workspaces, etc.).

        Args:
            old_revisions: Old revisions to delete.
            mongo_cleanup: Also delete revision records from MongoDB after image deletion.

        Returns:
            Dict with deletion statistics.
        """
        if not old_revisions:
            self.logger.info("No old revisions to delete")
            return {}

        deletion_results = {
            "docker_images_deleted": 0,
            "mongo_revisions_cleaned": 0,
            "skipped_in_use": 0,
            "failed": 0,
        }

        try:
            registry_in_cluster = self.skopeo_client.is_registry_in_cluster()
            registry_enabled = False
            if registry_in_cluster:
                registry_enabled = self.enable_registry_deletion()

            try:
                # Real-time usage check before deletion
                self.logger.info("Performing real-time usage check before deletion...")
                service = ImageUsageService()
                tags_to_check = [rev.docker_tag for rev in old_revisions]
                in_use_tags, usage_info = service.check_tags_in_use(tags_to_check)

                if in_use_tags:
                    self.logger.warning(
                        f"  Found {len(in_use_tags)} revision tag(s) currently in use - these will be skipped"
                    )
                    for tag in sorted(in_use_tags):
                        usage = usage_info.get(tag, {})
                        summary = service.generate_usage_summary(usage)
                        self.logger.warning(f"  - {tag}: {summary}")
                else:
                    self.logger.info("  All revision tags confirmed as unused")

                deleted_revision_ids: Set[str] = set()

                for rev in old_revisions:
                    if rev.docker_tag in in_use_tags:
                        usage = usage_info.get(rev.docker_tag, {})
                        summary = service.generate_usage_summary(usage)
                        self.logger.warning(f"  Skipping {rev.full_image} (in use: {summary})")
                        deletion_results["skipped_in_use"] += 1
                        continue

                    try:
                        self.logger.info(f"  Deleting: {rev.full_image}")
                        success = self.skopeo_client.delete_image(f"{self.repository}/environment", rev.docker_tag)
                        if success:
                            self.logger.info("    Deleted successfully")
                            deletion_results["docker_images_deleted"] += 1
                            deleted_revision_ids.add(rev.revision_id)
                        else:
                            self.logger.warning("    Failed to delete")
                            deletion_results["failed"] += 1
                    except Exception as e:
                        self.logger.error(f"    Error deleting: {e}")
                        deletion_results["failed"] += 1

                # MongoDB cleanup - only for successfully deleted images
                if mongo_cleanup and deleted_revision_ids:
                    self.logger.info(
                        f"Cleaning up MongoDB records for {len(deleted_revision_ids)} deleted revision(s)..."
                    )
                    mongo_client = get_mongo_client()
                    try:
                        db = mongo_client[config_manager.get_mongo_db()]
                        revisions_coll = db["environment_revisions"]
                        model_versions_coll = db["model_versions"]
                        models_coll = db["models"]

                        for rev_id_str in deleted_revision_ids:
                            try:
                                rev_oid = ObjectId(rev_id_str)

                                # Skip if an unarchived model version references this revision
                                model_ids = model_versions_coll.distinct(
                                    "modelId.value", {"environmentRevisionId": rev_oid}
                                )
                                if model_ids:
                                    unarchived = models_coll.count_documents(
                                        {"_id": {"$in": model_ids}, "isArchived": False},
                                        limit=1,
                                    )
                                    if unarchived > 0:
                                        self.logger.info(
                                            f"  Skipping MongoDB record {rev_id_str} "
                                            "(referenced by versions of unarchived models)"
                                        )
                                        continue

                                result = revisions_coll.delete_one({"_id": rev_oid})
                                if result.deleted_count > 0:
                                    self.logger.info(f"  Deleted environment_revision: {rev_id_str}")
                                    deletion_results["mongo_revisions_cleaned"] += 1
                                else:
                                    self.logger.warning(f"  Revision not found in MongoDB: {rev_id_str}")
                            except Exception as e:
                                self.logger.error(f"  Error deleting MongoDB record {rev_id_str}: {e}")
                    finally:
                        mongo_client.close()

            finally:
                if registry_enabled:
                    self.disable_registry_deletion()

        except Exception as e:
            self.logger.error(f"Error deleting old revisions: {e}")
            raise

        return deletion_results

    def calculate_freed_space(self, old_revisions: List[OldRevisionInfo]) -> int:
        """Calculate space that would be freed by deleting old revisions.

        Uses ImageAnalyzer to account for shared layers correctly.
        Layers shared with kept revisions or other image types are not counted.
        Per-revision size_bytes reflects what would be freed by deleting only that
        one revision (often 0 when layers are shared), while the returned total
        reflects what would be freed by deleting all candidates together.
        """
        if not old_revisions:
            return 0

        try:
            self.logger.info("Analyzing Docker images to calculate freed space (accounting for shared layers)...")
            self.logger.info("Analyzing both environment and model images so shared layers are counted correctly.")
            analyzer = ImageAnalyzer(self.registry_url, self.repository)

            # Must analyze ALL image types so that ref_counts reflect the full registry.
            # Analyzing only 'environment' would make layers shared with 'model' images
            # appear to have lower ref_counts and overestimate freed space.
            analyzer.analyze_image("environment")
            analyzer.analyze_image("model")

            # Set per-revision size (what would be freed by deleting just that one image)
            for rev in old_revisions:
                image_id = f"environment:{rev.docker_tag}"
                rev.size_bytes = analyzer.freed_space_if_deleted([image_id])

            # Total freed if all candidates deleted together (deduplicated)
            unique_image_ids = list(dict.fromkeys(f"environment:{r.docker_tag}" for r in old_revisions))
            total_freed = analyzer.freed_space_if_deleted(unique_image_ids)
            self.logger.info(f"Total space that would be freed: {total_freed / (1024 ** 3):.2f} GB")
            return total_freed

        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            return 0

    def generate_report(self, old_revisions: List[OldRevisionInfo], total_freed_bytes: int = 0) -> Dict:
        """Generate a report of old revisions found.

        Args:
            old_revisions: Old revisions identified for deletion.
            total_freed_bytes: Combined freed space if all revisions deleted together
                (calculated by calculate_freed_space; accounts for shared layers across
                the full candidate set, so may differ from summing per-revision sizes).

        Returns:
            Report dict with summary and per-environment details.
        """
        # Group by environment
        by_env: Dict[str, List[OldRevisionInfo]] = {}
        for rev in old_revisions:
            by_env.setdefault(rev.environment_id, []).append(rev)

        summary = {
            "total_old_revisions": len(old_revisions),
            "environments_affected": len(by_env),
            "keep_revisions": self.keep_revisions,
            "total_size_bytes": total_freed_bytes,
            "total_size_gb": round(total_freed_bytes / (1024**3), 2),
        }

        grouped: Dict[str, list] = {}
        for env_id, revisions in by_env.items():
            grouped[env_id] = [
                {
                    "revision_id": r.revision_id,
                    "environment_name": r.environment_name,
                    "docker_tag": r.docker_tag,
                    "full_image": r.full_image,
                    "size_bytes": r.size_bytes,
                }
                for r in revisions
            ]

        return {
            "summary": summary,
            "grouped_by_environment": grouped,
            "metadata": {
                "registry_url": self.registry_url,
                "repository": self.repository,
                "analysis_timestamp": datetime.now().isoformat(),
            },
        }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find and optionally delete old environment revisions, " "keeping only the most recent N per environment."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: find old revisions (keep 5 most recent per environment)
  python delete_old_revisions.py

  # Keep only 3 revisions per environment
  python delete_old_revisions.py --keep-revisions 3

  # Delete old revisions (requires confirmation)
  python delete_old_revisions.py --apply

  # Delete without confirmation
  python delete_old_revisions.py --apply --force

  # Also clean up MongoDB records after deletion
  python delete_old_revisions.py --apply --mongo-cleanup

  # Force-regenerate usage reports before analysis
  python delete_old_revisions.py --generate-reports

  # Restrict to specific environments from a file
  python delete_old_revisions.py --input my-envs.txt --apply

  # Full workflow: regenerate reports, keep 3, delete without confirmation
  python delete_old_revisions.py --generate-reports --keep-revisions 3 --apply --force
        """,
    )

    parser.add_argument("--registry-url", help="Docker registry URL (default: from config)")
    parser.add_argument("--repository", help="Repository name (default: from config)")
    parser.add_argument("--output", help="Output file path for the report (default: reports/old-revisions.json)")

    parser.add_argument(
        "--input",
        metavar="FILE",
        help="File of environment ObjectIDs to restrict processing to (one per line; supports environment: prefix)",
    )

    parser.add_argument(
        "--keep-revisions",
        type=int,
        default=DEFAULT_KEEP_REVISIONS,
        metavar="N",
        help=f"Number of most recent revisions to keep per environment (default: {DEFAULT_KEEP_REVISIONS})",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete old revision images (default: dry-run)",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt when using --apply",
    )

    parser.add_argument(
        "--generate-reports",
        action="store_true",
        help="Generate required metadata reports before analysis",
    )

    parser.add_argument(
        "--mongo-cleanup",
        action="store_true",
        help="Also delete MongoDB environment_revision records after Docker image deletion (advanced / high-risk; see README)",
    )

    parser.add_argument(
        "--enable-docker-deletion",
        action="store_true",
        help="Enable registry deletion by treating registry as in-cluster (overrides auto-detection)",
    )

    parser.add_argument(
        "--registry-statefulset",
        default="docker-registry",
        help="Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)",
    )

    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_arguments()

    if args.keep_revisions < 1:
        logger.error("--keep-revisions must be at least 1")
        sys.exit(1)

    registry_url = args.registry_url or config_manager.get_registry_url()
    repository = args.repository or config_manager.get_repository()
    output_file = args.output or str(Path(config_manager.get_output_dir()) / "old-revisions.json")

    try:
        cleaner = OldRevisionCleaner(
            registry_url=registry_url,
            repository=repository,
            keep_revisions=args.keep_revisions,
            enable_docker_deletion=args.enable_docker_deletion,
            registry_statefulset=args.registry_statefulset,
        )

        logger.info("=" * 60)
        if args.apply:
            logger.info("   Delete Old Environment Revisions (DELETE MODE)")
        else:
            logger.info("   Delete Old Environment Revisions (DRY RUN)")
        logger.info("=" * 60)
        logger.info(f"Registry:        {registry_url}")
        logger.info(f"Repository:      {repository}")
        logger.info(f"Keep revisions:  {args.keep_revisions} most recent per environment")
        logger.info(f"Mode:            {'DELETE' if args.apply else 'DRY RUN'}")
        if args.input:
            logger.info(f"Input file:      {args.input}")
        logger.info("=" * 60)

        # Load environment ID filter from --input if provided
        environment_ids: Optional[Set[str]] = None
        if args.input:
            ids = read_object_ids_from_file(args.input)
            if not ids:
                logger.error(f"No valid environment ObjectIDs found in {args.input}")
                sys.exit(1)
            environment_ids = set(ids)
            logger.info(f"Loaded {len(environment_ids)} environment ID(s) from {args.input}")

        # Optionally regenerate usage reports
        if args.generate_reports:
            cleaner.generate_required_reports()

        # Find old revisions
        old_revisions = cleaner.find_old_revisions(environment_ids=environment_ids)

        if not old_revisions:
            logger.info("No old revisions found - nothing to do.")
            sys.exit(0)

        # Filter out revisions that are cloned from by a kept revision
        old_revisions = cleaner._filter_cloned_by_kept_revisions(old_revisions)

        if not old_revisions:
            logger.info("No deletable old revisions after build-chain filtering - nothing to do.")
            sys.exit(0)

        # Calculate freed space (populates per-revision size_bytes and returns combined total)
        total_freed = cleaner.calculate_freed_space(old_revisions)

        # Generate and save report
        report = cleaner.generate_report(old_revisions, total_freed)
        saved_path = save_json(output_file, report, timestamp=True)
        logger.info(f"Report saved to: {saved_path}")

        # Print summary
        summary = report["summary"]
        logger.info("\nSummary:")
        logger.info(f"  Old revisions found:     {summary['total_old_revisions']}")
        logger.info(f"  Environments affected:   {summary['environments_affected']}")
        logger.info(f"  Keeping per environment: {summary['keep_revisions']} most recent")

        if not args.apply:
            logger.info("\nDRY RUN complete - no images were deleted.")
            logger.info("Use --apply to perform deletion.")
            sys.exit(0)

        # Confirm deletion
        if not cleaner.confirm_deletion(len(old_revisions), "old revision images", force=args.force):
            logger.info("Deletion cancelled.")
            sys.exit(0)

        # Delete
        deletion_results = cleaner.delete_old_revisions(
            old_revisions,
            mongo_cleanup=args.mongo_cleanup,
        )

        # Log summary
        logger.info("\nDeletion complete.")
        logger.info(f"  Docker images deleted:   {deletion_results.get('docker_images_deleted', 0)}")
        logger.info(f"  Skipped (in use):        {deletion_results.get('skipped_in_use', 0)}")
        logger.info(f"  Failed:                  {deletion_results.get('failed', 0)}")
        if args.mongo_cleanup:
            logger.info(f"  MongoDB records cleaned: {deletion_results.get('mongo_revisions_cleaned', 0)}")

    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
