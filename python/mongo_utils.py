"""
MongoDB utility helpers shared across scripts.
"""

from typing import Optional, Any
import json
from pymongo import MongoClient
from bson import json_util
from config_manager import config_manager


def get_mongo_client() -> MongoClient:
    """Return a MongoClient using the centralized connection string."""
    connection_string = config_manager.get_mongo_connection_string()
    return MongoClient(connection_string)


def get_db(client: Optional[MongoClient] = None):
    """Return the configured database handle.

    If client is not provided, a new client is created.
    Caller is responsible for closing the client they create/manage.
    """
    created_client = False
    if client is None:
        client = get_mongo_client()
        created_client = True
    try:
        return client[config_manager.get_mongo_db()]
    finally:
        # If we created the client implicitly, don't close here; caller cannot use DB after.
        # So only manage lifetime at caller.
        if created_client:
            pass


def get_collection(collection_name: Optional[str] = None, client: Optional[MongoClient] = None):
    """Return a collection from the configured DB. Defaults to configured collection name."""
    db = get_db(client)
    name = collection_name or config_manager.get_mongo_collection()
    return db[name]


def bson_to_jsonable(data: Any) -> Any:
    """Convert BSON-containing structures to JSON-serializable Python objects."""
    return json.loads(json_util.dumps(data))


