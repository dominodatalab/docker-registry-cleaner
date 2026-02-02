#!/usr/bin/env python3
"""
Reset default environments in userPreferences and organizations.

This script reads a list of environment ObjectIDs from an input file (one per line),
finds any:

- `userPreferences` documents whose ``defaultEnvironmentId`` matches one of those
  environments, and
- `organizations` documents whose ``defaultV2EnvironmentId`` matches one of those
  environments,

reports how many users/organizations are affected per environment, and optionally
unsets those default fields (when not running in dry-run mode).

Input file format (same conventions as other environment ID files):

- Plain ObjectID per line:
    5f9d88f5b1e3c40012d3ab01
    5f9d88f5b1e3c40012d3ab02

- Or typed format (only the ObjectID part is used):
    environment: 5f9d88f5b1e3c40012d3ab01
    env: 5f9d88f5b1e3c40012d3ab02

Usage examples:

  # Dry-run: show which userPreferences/organizations would be changed
  python python/main.py reset_default_environments --input environments
  
  # Actually unset defaults for matching userPreferences and organizations
  python python/main.py reset_default_environments --input environments --apply
"""

import argparse
from pathlib import Path
from typing import Dict, List

from bson import ObjectId

# Add parent directory to path for imports
import os
import sys

_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager  # noqa: E402
from utils.logging_utils import get_logger, setup_logging  # noqa: E402
from utils.mongo_utils import get_mongo_client  # noqa: E402
from utils.object_id_utils import validate_object_id  # noqa: E402
from utils.image_metadata import lookup_user_names_and_logins  # noqa: E402


logger = get_logger(__name__)


def load_environment_ids_from_file(file_path: str) -> List[str]:
    """
    Load environment ObjectIDs from a file.

    Supports lines like:
      - "5f9d88f5b1e3c40012d3ab01"
      - "environment: 5f9d88f5b1e3c40012d3ab01"
      - "env:5f9d88f5b1e3c40012d3ab01"
    """
    env_ids: List[str] = []
    path = Path(file_path)
    if not path.exists():
        logger.error(f"Input file not found: {file_path}")
        return env_ids

    with path.open("r") as f:
        for line_num, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            value = line
            if ":" in line:
                prefix, _, rest = line.partition(":")
                # only accept environment / env prefixes; ignore others
                pref = prefix.strip().lower()
                if pref not in ("environment", "env"):
                    logger.warning(
                        f"Line {line_num}: unexpected prefix '{prefix}'. Expected 'environment:' or 'env:'. Skipping."
                    )
                    continue
                value = rest.strip()

            try:
                oid = validate_object_id(value, field_name=f"Environment ObjectID on line {line_num}")
                env_ids.append(str(oid))
            except ValueError as e:
                logger.warning(str(e))

    if not env_ids:
        logger.warning(f"No valid environment ObjectIDs found in input file '{file_path}'")
    else:
        logger.info(f"Loaded {len(env_ids)} environment IDs from {file_path}")
    return env_ids


def reset_user_preferences(env_ids: List[str], apply: bool = False) -> Dict[str, int]:
    """
    Reset default environment references in userPreferences and organizations.
    
    For the provided environment IDs, this function:
    
    - Finds `userPreferences` documents whose ``defaultEnvironmentId`` matches any of
      the given environments and (optionally) unsets ``defaultEnvironmentId``.
    - Finds `organizations` documents whose ``defaultV2EnvironmentId`` matches any of
      the given environments and (optionally) unsets ``defaultV2EnvironmentId``.
    
    Returns a summary dict with:
      - 'matched_user_prefs': number of userPreferences docs with matching defaultEnvironmentId
      - 'updated_user_prefs': number of userPreferences docs actually modified (0 in dry-run)
      - 'matched_organizations': number of organizations docs with matching defaultV2EnvironmentId
      - 'updated_organizations': number of organizations docs actually modified (0 in dry-run)
      - 'affected_environments': number of distinct environment IDs referenced in userPreferences
      - 'affected_org_environments': number of distinct environment IDs referenced in organizations
    """
    summary = {
        "matched_user_prefs": 0,
        "updated_user_prefs": 0,
        "matched_organizations": 0,
        "updated_organizations": 0,
        "affected_environments": 0,
        "affected_org_environments": 0,
    }

    if not env_ids:
        logger.info("No environment IDs provided; nothing to do.")
        return summary

    mongo_client = get_mongo_client()
    try:
        db = mongo_client[config_manager.get_mongo_db()]

        env_oids = [ObjectId(eid) for eid in env_ids]
        
        collections = db.list_collection_names()
        
        # --- userPreferences: defaultEnvironmentId ---
        if "userPreferences" in collections:
            user_prefs = db["userPreferences"]
            
            # Aggregate user IDs per environment defaultEnvironmentId
            pipeline = [
                {"$match": {"defaultEnvironmentId": {"$in": env_oids}}},
                {
                    "$group": {
                        "_id": "$defaultEnvironmentId",
                        "user_ids": {"$addToSet": "$userId"},
                        "user_count": {"$sum": 1},
                    }
                },
            ]
            results = list(user_prefs.aggregate(pipeline))
            
            if results:
                summary["affected_environments"] = len(results)
                
                # Collect all userIds to look up names/loginIds in users collection
                all_user_ids = set()
                for doc in results:
                    for uid in doc.get("user_ids", []):
                        if isinstance(uid, ObjectId):
                            all_user_ids.add(uid)
                
                user_id_to_name, user_id_to_login = lookup_user_names_and_logins(all_user_ids)
                
                total_matched = 0
                
                logger.info("Found userPreferences with defaultEnvironmentId matching provided environments:")
                for doc in results:
                    env_oid = doc.get("_id")
                    user_count = doc.get("user_count", 0)
                    user_ids = doc.get("user_ids") or []
                    env_id_str = str(env_oid) if env_oid is not None else "<unknown>"
                    total_matched += user_count
                    
                    # Build example labels "<fullName> (<loginId.id>)" for up to 5 users
                    example_labels: List[str] = []
                    for uid in user_ids[:5]:
                        uid_str = str(uid)
                        name = user_id_to_name.get(uid_str, "Unknown")
                        login = user_id_to_login.get(uid_str, "")
                        if login:
                            label = f"{name} ({login})"
                        else:
                            label = name or uid_str
                        example_labels.append(label)
                    
                    example_str = ", ".join(example_labels) if example_labels else "no user details available"
                    
                    logger.info(
                        f"  Environment {env_id_str}: {user_count} user(s) with this as defaultEnvironmentId "
                        f"(example users: {example_str})"
                    )
                
                summary["matched_user_prefs"] = total_matched
                
                if apply:
                    # Apply the update: unset defaultEnvironmentId for all matching docs
                    logger.warning(
                        f"Applying changes: unsetting defaultEnvironmentId for {total_matched} userPreferences document(s)..."
                    )
                    result = user_prefs.update_many(
                        {"defaultEnvironmentId": {"$in": env_oids}},
                        {"$unset": {"defaultEnvironmentId": ""}},
                    )
                    modified = result.modified_count if result is not None else 0
                    summary["updated_user_prefs"] = modified
                    logger.info(f"✅ Unset defaultEnvironmentId for {modified} userPreferences document(s).")
                    if modified < total_matched:
                        logger.warning(
                            f"Expected to update {total_matched} docs but only {modified} were modified. "
                            "Some documents may have changed between scan and update, or lacked defaultEnvironmentId at update time."
                        )
                else:
                    logger.info(
                        "Dry run: not modifying userPreferences. "
                        f"{total_matched} userPreferences document(s) would have defaultEnvironmentId unset."
                    )
            else:
                logger.info(
                    "No userPreferences documents found with defaultEnvironmentId matching the provided environment IDs."
                )
        else:
            logger.info("Collection 'userPreferences' not found. Nothing to reset for user preferences.")
        
        # --- organizations: defaultV2EnvironmentId ---
        if "organizations" in collections:
            orgs = db["organizations"]
            
            pipeline = [
                {"$match": {"defaultV2EnvironmentId": {"$in": env_oids}}},
                {
                    "$group": {
                        "_id": "$defaultV2EnvironmentId",
                        "org_ids": {"$addToSet": "$_id"},
                        "org_count": {"$sum": 1},
                    }
                },
            ]
            org_results = list(orgs.aggregate(pipeline))
            
            if org_results:
                summary["affected_org_environments"] = len(org_results)
                
                total_org_matched = 0
                logger.info("Found organizations with defaultV2EnvironmentId matching provided environments:")
                for doc in org_results:
                    env_oid = doc.get("_id")
                    org_count = doc.get("org_count", 0)
                    env_id_str = str(env_oid) if env_oid is not None else "<unknown>"
                    total_org_matched += org_count
                    
                    logger.info(
                        f"  Environment {env_id_str}: {org_count} organization(s) with this as defaultV2EnvironmentId"
                    )
                
                summary["matched_organizations"] = total_org_matched
                
                if apply:
                    logger.warning(
                        f"Applying changes: unsetting defaultV2EnvironmentId for {total_org_matched} organization document(s)..."
                    )
                    org_result = orgs.update_many(
                        {"defaultV2EnvironmentId": {"$in": env_oids}},
                        {"$unset": {"defaultV2EnvironmentId": ""}},
                    )
                    org_modified = org_result.modified_count if org_result is not None else 0
                    summary["updated_organizations"] = org_modified
                    logger.info(f"✅ Unset defaultV2EnvironmentId for {org_modified} organization document(s).")
                    if org_modified < total_org_matched:
                        logger.warning(
                            f"Expected to update {total_org_matched} docs but only {org_modified} were modified. "
                            "Some documents may have changed between scan and update, or lacked defaultV2EnvironmentId at update time."
                        )
                else:
                    logger.info(
                        "Dry run: not modifying organizations. "
                        f"{total_org_matched} organization document(s) would have defaultV2EnvironmentId unset."
                    )
            else:
                logger.info(
                    "No organizations found with defaultV2EnvironmentId matching the provided environment IDs."
                )
        else:
            logger.info("Collection 'organizations' not found. Nothing to reset for organizations.")
        
        return summary
    finally:
        mongo_client.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset defaultEnvironmentId in userPreferences for a set of environment IDs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: show which userPreferences/organizations would be affected
  python python/main.py reset_default_environments --input environments
  
  # Actually unset defaults for matching userPreferences and organizations
  python python/main.py reset_default_environments --input environments --apply
        """,
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Path to file containing environment IDs (one per line). "
             "Lines may be plain ObjectIDs or prefixed with 'environment:' / 'env:'.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually unset defaultEnvironmentId. If omitted, runs in dry-run mode.",
    )

    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_arguments()

    logger.info("=" * 60)
    logger.info("   Reset default environments in userPreferences and organizations")
    if args.apply:
        logger.warning("⚠️  APPLY mode: userPreferences.defaultEnvironmentId will be unset where it matches input IDs.")
    else:
        logger.info("Dry-run mode: no changes will be made. Use --apply to perform updates.")
    logger.info("=" * 60)

    env_ids = load_environment_ids_from_file(args.input)
    summary = reset_user_preferences(env_ids, apply=args.apply)

    logger.info("\nSummary:")
    logger.info(f"  Environment IDs provided: {len(env_ids)}")
    logger.info(f"  Environments with matching userPreferences.defaultEnvironmentId: {summary['affected_environments']}")
    logger.info(f"  Matched userPreferences: {summary['matched_user_prefs']}")
    logger.info(f"  Updated userPreferences: {summary['updated_user_prefs']}")
    logger.info(
        f"  Environments with matching organizations.defaultV2EnvironmentId: "
        f"{summary['affected_org_environments']}"
    )
    logger.info(f"  Matched organizations: {summary['matched_organizations']}")
    logger.info(f"  Updated organizations: {summary['updated_organizations']}")


if __name__ == "__main__":
    main()

