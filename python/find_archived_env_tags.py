#!/usr/bin/env python3
"""
Find archived environment ObjectIDs in Mongo and matching tags in the Docker registry.

Workflow:
- Query Mongo collection (default: environments_v2) for documents where isArchived == true
- Collect their _id values (ObjectIDs) as strings
- For each target image (default: environment, model) under the configured repository,
  list tags and find any that contain one of the archived ObjectIDs
- Write a report mapping each ObjectID to matching tags; also include summary counts

Usage examples:
  python find_archived_env_tags.py --registry-url docker-registry:5000 --repository dominodatalab \
    --images environment model
"""

import argparse
import json
from typing import Dict, List, Set

from pymongo import MongoClient

from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger


logger = get_logger(__name__)


def fetch_archived_object_ids(
    mongo_uri: str,
    database: str,
) -> List[str]:
    """Return list of ObjectID strings from documents where isArchived is True."""
    client = MongoClient(mongo_uri)
    try:
        col = client[database]["environments_v2"]
        # Only select _id field for efficiency
        cursor = col.find({"isArchived": True}, {"_id": 1})
        ids: List[str] = []
        for doc in cursor:
            _id = doc.get("_id")
            if _id is not None:
                ids.append(str(_id))
        return ids
    finally:
        client.close()


def list_tags_for_image(
    skopeo_client: SkopeoClient,
    registry_url: str,
    repository: str,
    image: str,
) -> List[str]:
    """List tags for a specific image using skopeo via the project's SkopeoClient.

    We directly call run_skopeo_command to avoid assumptions about SkopeoClient's
    higher-level convenience signatures.
    """
    ref = f"docker://{registry_url}/{repository}/{image}"
    output = skopeo_client.run_skopeo_command("list-tags", [ref])
    if not output:
        return []
    try:
        payload = json.loads(output)
        return payload.get("Tags", []) or []
    except json.JSONDecodeError:
        logger.error(f"Failed to parse list-tags output for {ref}")
        return []


def find_matching_tags(
    tags_by_image: Dict[str, List[str]],
    archived_ids: List[str],
) -> Dict[str, List[str]]:
    """Return mapping of archived ObjectID -> list of tags that contain it (substring)."""
    archived_set: Set[str] = set(archived_ids)
    matches: Dict[str, List[str]] = {oid: [] for oid in archived_set}

    for image, tags in tags_by_image.items():
        for tag in tags:
            for oid in archived_set:
                if oid in tag:
                    matches[oid].append(f"{image}:{tag}")
    # Remove OIDs that had no matches for a cleaner report
    return {oid: lst for oid, lst in matches.items() if lst}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find archived environment ObjectIDs and matching Docker tags",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python find_archived_env_tags.py --registry-url docker-registry:5000 --repository dominodatalab \
    --images environment model --output archived-tags.json
        """,
    )
    parser.add_argument("--registry-url", default=config_manager.get_registry_url(), help="Container registry URL")
    parser.add_argument("--repository", default=config_manager.get_repository(), help="Container repository (org/project)")
    parser.add_argument("--images", nargs="*", default=["environment", "model"], help="Images to scan under the repository")
    return parser.parse_args()


def main() -> None:
    setup_logging()

    args = parse_args()

    # Prepare Mongo parameters
    mongo_uri = config_manager.get_mongo_connection_string()
    mongo_db = config_manager.get_mongo_db()

    logger.info("Fetching archived ObjectIDs from Mongo...")
    archived_ids = fetch_archived_object_ids(mongo_uri, mongo_db)
    if not archived_ids:
        logger.info("No archived ObjectIDs found.")
        with open(args.output, "w") as f:
            json.dump({"archived_ids": [], "matches": {}, "summary": {"num_archived_ids": 0, "num_matches": 0}}, f, indent=2)
        logger.info(f"Report written to {args.output}")
        return

    logger.info(f"Found {len(archived_ids)} archived ObjectIDs")

    # Initialize Skopeo client (uses env REGISTRY_PASSWORD)
    skopeo_client = SkopeoClient(config_manager, use_pod=False)

    # Gather tags for each requested image
    tags_by_image: Dict[str, List[str]] = {}
    for image in args.images:
        logger.info(f"Listing tags for {args.repository}/{image}...")
        tags = list_tags_for_image(skopeo_client, args.registry_url, args.repository, image)
        logger.info(f"  Found {len(tags)} tags")
        tags_by_image[f"{args.repository}/{image}"] = tags

    # Find matches
    logger.info("Searching for tags that contain archived ObjectIDs...")
    matches = find_matching_tags(tags_by_image, archived_ids)

    # Prepare report
    num_matches = sum(len(v) for v in matches.values())
    report = {
        "archived_ids": archived_ids,
        "matches": matches,
        "summary": {
            "num_archived_ids": len(archived_ids),
            "num_images_scanned": len(args.images),
            "num_matches": num_matches,
        },
    }

    # Write report using configured path
    from pathlib import Path
    out_path = Path(config_manager.get_archived_tags_report_path())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Report written to {out_path}")
    logger.info(f"Archived IDs: {len(archived_ids)}, Matches: {num_matches}")


if __name__ == "__main__":
    main()


