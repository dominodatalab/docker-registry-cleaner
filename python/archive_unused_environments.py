#!/usr/bin/env python3
"""
Archive unused environments in MongoDB (set isArchived = true on environments_v2).

This script reuses the unused-environment detection logic from delete_unused_environments.py
to identify environments that are not used by:
- Project defaults (projects.overrideV2EnvironmentId)
as well as other known usage locations (workspaces, models, runs, scheduled jobs, app versions).

By default this script runs in DRY-RUN mode and does NOT modify MongoDB.
Use the --apply flag to actually mark the environments as archived.

Usage examples:
  # Dry-run: list environments that would be archived
  python archive_unused_environments.py

  # Force regeneration of metadata reports before analysis
  python archive_unused_environments.py --generate-reports

  # Actually archive the environments (with confirmation)
  python archive_unused_environments.py --apply

  # Archive with recent usage window (e.g., treat runs in last 30 days as in-use)
  python archive_unused_environments.py --unused-since-days 30 --apply
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Dict

from bson import ObjectId

from config_manager import config_manager
from delete_unused_environments import UnusedEnvironmentsFinder, UnusedEnvInfo
from logging_utils import setup_logging, get_logger
from mongo_utils import get_mongo_client
from report_utils import save_json


logger = get_logger(__name__)


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Archive unused environments in MongoDB (set isArchived = true in environments_v2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: list environments that would be archived
  python archive_unused_environments.py

  # Force regeneration of metadata reports before analysis
  python archive_unused_environments.py --generate-reports

  # Consider only recent usage in runs (e.g., last 30 days) when determining unused
  python archive_unused_environments.py --unused-since-days 30

  # Actually archive the environments (requires confirmation)
  python archive_unused_environments.py --apply

  # Archive without confirmation
  python archive_unused_environments.py --apply --force

  # Custom output file
  python archive_unused_environments.py --output archive-unused-environments.json
        """
    )

    parser.add_argument(
        '--output',
        help='Output file path (default: reports/archive-unused-environments.json)'
    )

    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually mark environments as archived in MongoDB (default: dry-run)'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt when using --apply'
    )

    parser.add_argument(
        '--generate-reports',
        action='store_true',
        help='Generate required metadata reports (extract_metadata) before analysis'
    )

    parser.add_argument(
        '--unused-since-days',
        dest='days',
        type=int,
        metavar='N',
        help='Only consider environments as "in-use" if they were used in a run within the last N days. '
             'If the last execution that used an environment was more than N days ago, it will be considered '
             'unused and eligible for archiving. If omitted, any historical run marks the environment as in-use. '
             'This filters based on the last_used, completed, or started timestamp from runs.'
    )

    return parser.parse_args()


def find_unused_environment_docs(recent_days: int | None, generate_reports: bool) -> List[UnusedEnvInfo]:
    """
    Use UnusedEnvironmentsFinder to determine which environment/revision IDs are unused,
    then filter to only environments that exist in environments_v2 and are not already archived.
    """
    registry_url = config_manager.get_registry_url()
    repository = config_manager.get_repository()

    finder = UnusedEnvironmentsFinder(
        registry_url=registry_url,
        repository=repository,
        recent_days=recent_days,
        enable_docker_deletion=False,
        registry_statefulset=None,
    )

    # Ensure metadata reports exist if requested
    mongodb_usage_path = Path(config_manager.get_mongodb_usage_path())
    reports_exist = mongodb_usage_path.exists()

    if generate_reports or not reports_exist:
        if not reports_exist:
            logger.info("Required metadata reports not found. Generating them now...")
        finder.generate_required_reports()

    logger.info("Finding unused environments from metadata...")
    unused_envs = finder.find_unused_environments()
    if not unused_envs:
        logger.info("No unused environments (or revisions) found by UnusedEnvironmentsFinder")
        return []

    # Filter to only environments that actually exist in environments_v2 and are not already archived
    mongo_client = get_mongo_client()
    try:
        db = mongo_client[config_manager.get_mongo_db()]
        envs_collection = db["environments_v2"]

        # Map object_id -> UnusedEnvInfo
        by_id: Dict[str, UnusedEnvInfo] = {env.object_id: env for env in unused_envs}

        # Look up which of these IDs correspond to environment documents
        candidate_ids = list(by_id.keys())
        valid_object_ids = []
        for oid_str in candidate_ids:
            if len(oid_str) == 24:
                try:
                    valid_object_ids.append(ObjectId(oid_str))
                except Exception:
                    continue

        if not valid_object_ids:
            logger.info("No valid ObjectIDs among unused environment candidates")
            return []

        cursor = envs_collection.find(
            {"_id": {"$in": valid_object_ids}},
            {"_id": 1, "name": 1, "isArchived": 1},
        )

        result: List[UnusedEnvInfo] = []
        for doc in cursor:
            oid_str = str(doc.get("_id"))
            is_archived = bool(doc.get("isArchived", False))
            if is_archived:
                # Skip environments already archived
                continue

            base_info = by_id.get(oid_str)
            if base_info is None:
                # Shouldn't happen, but skip if we can't map back
                continue

            # Ensure env_name is populated from Mongo if missing
            name = doc.get("name", "") or base_info.env_name
            result.append(
                UnusedEnvInfo(
                    object_id=oid_str,
                    env_name=name,
                    image_type="",  # Not relevant for archiving
                    tag="",
                    full_image="",
                    size_bytes=0,
                )
            )

        logger.info(f"Found {len(result)} unused environments eligible for archiving (not already archived)")
        return result
    finally:
        mongo_client.close()


def archive_environments(envs_to_archive: List[UnusedEnvInfo], apply: bool) -> int:
    """Set isArchived = true for the given environment IDs. Returns number of modified documents."""
    if not envs_to_archive:
        return 0

    mongo_client = get_mongo_client()
    try:
        db = mongo_client[config_manager.get_mongo_db()]
        envs_collection = db["environments_v2"]

        object_ids = []
        for env in envs_to_archive:
            try:
                object_ids.append(ObjectId(env.object_id))
            except Exception:
                logger.warning(f"Skipping invalid ObjectID: {env.object_id}")

        if not object_ids:
            return 0

        if not apply:
            # In dry-run mode we don't actually modify MongoDB
            logger.info("Dry-run mode: not updating MongoDB")
            return 0

        result = envs_collection.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"isArchived": True}},
        )

        modified = result.modified_count if result is not None else 0
        logger.info(f"Archived {modified} environments in environments_v2")
        return modified
    finally:
        mongo_client.close()


def main():
    """Main entrypoint."""
    setup_logging()
    args = parse_arguments()

    output_file = args.output or str(Path(config_manager.get_output_dir()) / "archive-unused-environments.json")
    is_apply_mode = args.apply

    logger.info("=" * 60)
    if is_apply_mode:
        logger.info("   📝 ARCHIVING MODE: Archiving unused environments")
        logger.warning("⚠️  MongoDB records WILL be updated (isArchived = True)!")
    else:
        logger.info("   🔍 DRY RUN MODE (default): Listing environments that would be archived")
        logger.info("   No MongoDB records will be modified. Use --apply to actually archive environments.")
    logger.info("=" * 60)

    try:
        unused_env_docs = find_unused_environment_docs(
            recent_days=args.days,
            generate_reports=args.generate_reports,
        )

        if not unused_env_docs:
            logger.info("No environments eligible for archiving were found.")
            # Still write an empty report
            empty_report = {
                "summary": {
                    "total_candidates": 0,
                    "would_archive": 0,
                    "actually_archived": 0,
                },
                "environments": [],
            }
            save_json(output_file, empty_report)
            logger.info(f"Empty archive report written to {output_file}")
            sys.exit(0)

        # Prepare report data
        env_summaries = []
        for env in unused_env_docs:
            env_summaries.append(
                {
                    "object_id": env.object_id,
                    "name": env.env_name,
                }
            )

        logger.info(f"Total environments that would be archived: {len(env_summaries)}")
        for env in unused_env_docs[:20]:
            logger.info(f"  - {env.object_id}  {env.env_name}")
        if len(unused_env_docs) > 20:
            logger.info(f"  ... and {len(unused_env_docs) - 20} more")

        # Confirmation prompt for apply mode (unless --force)
        actually_archived = 0
        if is_apply_mode:
            if not args.force:
                logger.warning("\n⚠️  WARNING: You are about to mark the above environments as archived in MongoDB!")
                logger.warning("This will set isArchived = true on the corresponding documents in environments_v2.")
                logger.warning("This action cannot be undone.")
                response = input("\nDo you want to continue? (yes/no): ").strip().lower()
                if response not in ("yes", "y"):
                    logger.info("Operation cancelled by user")
                    is_apply_mode = False  # Treat as dry-run for reporting
                else:
                    logger.info("Proceeding with archiving unused environments...")

            if is_apply_mode:
                actually_archived = archive_environments(unused_env_docs, apply=True)
        else:
            logger.info("Dry-run mode: no changes will be made. Use --apply to actually archive these environments.")

        report = {
            "summary": {
                "total_candidates": len(env_summaries),
                "would_archive": len(env_summaries),
                "actually_archived": actually_archived,
            },
            "environments": env_summaries,
        }
        save_json(output_file, report)
        logger.info(f"Archive report written to {output_file}")

        if is_apply_mode:
            logger.info("\n✅ Archiving of unused environments completed.")
        else:
            logger.info("\n" + "=" * 60)
            logger.info("🔍 DRY RUN MODE COMPLETED")
            logger.info("=" * 60)
            logger.info("No MongoDB records were modified. Use --apply to actually archive environments:")
            logger.info("  python archive_unused_environments.py --apply")

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


