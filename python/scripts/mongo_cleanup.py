#!/usr/bin/env python3
"""
MongoDB cleanup tool for environment revision records tied to Docker image tags.

Reads targets from a file and either finds or deletes matching documents in the
configured Mongo collection based on `metadata.dockerImageName.tag`.

Accepted file formats (first non-comment token per line):
- Environments-style: <24-char ObjectID> [other columns ignored]
  → matches any tag that starts with "<ObjectID>-"
- Full tag: repo/image:tag or tag
  → exact match on the full tag value

Authentication/Config:
- Uses centralized settings from config_manager (host, port, rs, db, collection)
- Credentials from env: MONGODB_USERNAME (default: admin), MONGODB_PASSWORD (required)

Usage examples:
- Dry run (find): python mongo_cleanup.py --file ./environments
- Delete:         python mongo_cleanup.py --apply --file ./environments
- Custom file:    python mongo_cleanup.py --apply --file ./my-environments
"""

import argparse
import sys

from pathlib import Path
from typing import Iterator, Tuple

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.mongo_utils import get_mongo_client


def iter_targets_from_file(path: str) -> Iterator[Tuple[str, str]]:
	"""Yield (mode, value) pairs where mode is 'objectId' or 'tag'."""
	with open(path, "r") as fh:
		for line in fh:
			line = line.strip()
			if not line or line.startswith("#"):
				continue
			first_token = line.split()[0]
			candidate = first_token
			if len(candidate) == 24:
				try:
					int(candidate, 16)
					yield ("objectId", candidate)
					continue
				except ValueError:
					pass
			yield ("tag", first_token)



def connect_and_execute(apply: bool, target_mode: str, value: str, collection_name: str = "environment_revisions") -> None:
	"""Clean up MongoDB records for deleted Docker images.
	
	Args:
	    apply: If True, delete records; if False, dry-run mode (find only)
	    target_mode: Target mode ('objectId' or 'tag')
	    value: The tag or ObjectID value to search for
	    collection_name: MongoDB collection to clean up (default: environment_revisions)
	"""
	db_name = config_manager.get_mongo_db()
	client = get_mongo_client()
	db = client[db_name]
	collection = db[collection_name]
	if target_mode == "objectId":
		query = {"metadata.dockerImageName.tag": {"$regex": f"^{value}-"}}
	else:
		query = {"metadata.dockerImageName.tag": value}
	
	if apply:
		print(f"Deleting documents for {target_mode}='{value}' in {db_name}.{collection_name}")
		res = collection.delete_many(query)
		print(f"  deleted: {res.deleted_count}")
	else:
		print(f"DRY RUN: Finding documents for {target_mode}='{value}' in {db_name}.{collection_name}")
		count = 0
		for doc in collection.find(query):
			print(doc)
			count += 1
		print(f"  matched: {count}")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Cleanup Mongo records by Docker tag or ObjectID prefix")
	parser.add_argument("--file", required=True, help="Path to file: first column is ObjectID (environments-style) or full tag")
	parser.add_argument("--collection", default="environment_revisions", help="MongoDB collection to clean up (default: environment_revisions)")
	parser.add_argument("--apply", action="store_true", help="Apply deletion (default: dry-run mode shows what would be deleted)")
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	
	# Show mode
	if args.apply:
		print("⚠️  DELETION MODE - MongoDB records will be deleted")
	else:
		print("🔍 DRY RUN MODE - No changes will be made")
	print()
	
	for target_mode, value in iter_targets_from_file(args.file):
		connect_and_execute(args.apply, target_mode, value, args.collection)


if __name__ == "__main__":
	main()