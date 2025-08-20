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
- Dry run (find): python mongo_cleanup.py find --file ./environments
- Delete:         python mongo_cleanup.py delete --file ./environments
- Tags file:      python mongo_cleanup.py delete --file ./python/to_delete.txt
"""

import argparse
from typing import Iterator, Tuple

import pymongo
from config_manager import config_manager


def get_mongo_primary() -> str:
	conn = config_manager.get_mongo_connection_string()
	client = pymongo.MongoClient(conn)
	topology = client.admin.command("ismaster")
	primary = topology.get("primary")
	if not primary:
		raise RuntimeError("Could not discover Mongo primary")
	return primary


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


def connect_and_execute(primary_host: str, op_mode: str, target_mode: str, value: str) -> None:
	auth = config_manager.get_mongo_auth()
	db_name = config_manager.get_mongo_db()
	collection_name = config_manager.get_mongo_collection()
	connection_string = f"mongodb://{auth}@{primary_host}"
	client = pymongo.MongoClient(connection_string)
	db = client[db_name]
	collection = db[collection_name]
	if target_mode == "objectId":
		query = {"metadata.dockerImageName.tag": {"$regex": f"^{value}-"}}
	else:
		query = {"metadata.dockerImageName.tag": value}
	if op_mode == "find":
		print(f"Finding documents for {target_mode}='{value}' in {db_name}.{collection_name}")
		count = 0
		for doc in collection.find(query):
			print(doc)
			count += 1
		print(f"  matched: {count}")
	elif op_mode == "delete":
		print(f"Deleting documents for {target_mode}='{value}' in {db_name}.{collection_name}")
		res = collection.delete_many(query)
		print(f"  deleted: {res.deleted_count}")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Cleanup environment revision Mongo records by Docker tag or ObjectID prefix")
	parser.add_argument("mode", choices=["find", "delete"], help="Operation to perform")
	parser.add_argument("--file", required=True, help="Path to file: first column is ObjectID (environments-style) or full tag")
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	primary = get_mongo_primary()
	for target_mode, value in iter_targets_from_file(args.file):
		connect_and_execute(primary, args.mode, target_mode, value)


if __name__ == "__main__":
	main()