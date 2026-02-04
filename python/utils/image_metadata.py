#!/usr/bin/env python3
"""
Image metadata utilities for extracting names and owners from MongoDB.

Provides functions for querying MongoDB to get image names and owner information
for Docker images (models and environments).
"""

from typing import Dict, Optional, Set, Tuple

from utils.object_id_utils import normalize_object_id
from utils.tag_matching import model_tags_match


def extract_model_tag_from_version_doc(version_doc: dict) -> Optional[str]:
    """Extract Docker tag from a model_version document.

    Args:
        version_doc: MongoDB model_version document

    Returns:
        Tag string if found, None otherwise
    """
    metadata = version_doc.get("metadata", {})
    builds = metadata.get("builds", [])

    # metadata.builds is a list of dictionaries
    if isinstance(builds, list) and len(builds) > 0:
        first_build = builds[0]
        if isinstance(first_build, dict):
            slug = first_build.get("slug", {})
            if isinstance(slug, dict):
                image = slug.get("image", {})
                if isinstance(image, dict):
                    return image.get("tag")
    return None


def lookup_user_names_and_logins(user_ids: Set) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Look up user names (fullName) and login IDs (loginId.id) from MongoDB.

    Args:
        user_ids: Set of user ObjectIds (can be ObjectId objects or strings)

    Returns:
        Tuple of (user_id_to_name dict, user_id_to_login_id dict)
    """
    from bson import ObjectId

    from utils.config_manager import config_manager
    from utils.mongo_utils import get_mongo_client

    user_id_to_name = {}
    user_id_to_login = {}

    if not user_ids:
        return user_id_to_name, user_id_to_login

    try:
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]

        # Convert to ObjectIds for query
        object_ids = []
        id_to_str = {}
        for user_id in user_ids:
            try:
                if isinstance(user_id, ObjectId):
                    obj_id = user_id
                elif isinstance(user_id, str):
                    obj_id = ObjectId(user_id)
                else:
                    continue
                object_ids.append(obj_id)
                id_to_str[str(obj_id)] = normalize_object_id(user_id)
            except (ValueError, TypeError):
                continue

        if object_ids:
            users = db.users.find({"_id": {"$in": object_ids}}, {"_id": 1, "fullName": 1, "loginId.id": 1})
            for user_doc in users:
                user_id_str = id_to_str.get(str(user_doc["_id"]), normalize_object_id(user_doc["_id"]))
                user_id_to_name[user_id_str] = user_doc.get("fullName", "Unknown")
                login_id = (
                    user_doc.get("loginId", {}).get("id", "") if isinstance(user_doc.get("loginId"), dict) else ""
                )
                if login_id:
                    user_id_to_login[user_id_str] = str(login_id)

        mongo_client.close()
    except Exception as e:
        from utils.logging_utils import get_logger

        logger = get_logger(__name__)
        logger.warning(f"Could not query MongoDB for user names and login IDs: {e}")

    return user_id_to_name, user_id_to_login


def build_model_tag_to_metadata_mapping(
    model_tags: Set[str], include_model_name: bool = True, include_owner_info: bool = True
) -> Dict[str, Dict]:
    """Build a mapping from model tags to their metadata (name and owner info).

    Args:
        model_tags: Set of model Docker tags
        include_model_name: If True, include model name in result
        include_owner_info: If True, include owner name and login ID in result

    Returns:
        Dict mapping tag -> {'image_name': str, 'user_name': str, 'owner_login_id': str}
        Fields may be missing if not requested or not found
    """
    from bson import ObjectId

    from utils.config_manager import config_manager
    from utils.mongo_utils import get_mongo_client

    tag_to_metadata = {}

    if not model_tags:
        return tag_to_metadata

    try:
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]

        # Query model_versions for tags
        model_versions = db.model_versions.find(
            {"metadata.builds.slug.image.tag": {"$exists": True, "$ne": None}},
            {"_id": 1, "metadata.builds.slug.image.tag": 1, "metadata.createdBy": 1, "modelId.value": 1},
        )

        # Build tag -> (model_version_id, createdBy_id, model_id) mapping
        tag_to_model_info = {}
        model_ids = set()
        created_by_ids = set()

        for version_doc in model_versions:
            stored_tag = extract_model_tag_from_version_doc(version_doc)
            version_id = version_doc.get("_id")
            metadata = version_doc.get("metadata", {})
            created_by = metadata.get("createdBy")

            # Extract modelId - can be stored as modelId.value or directly as modelId
            model_id_obj = version_doc.get("modelId")
            model_id = None
            if isinstance(model_id_obj, dict):
                model_id = model_id_obj.get("value")
            elif model_id_obj:
                model_id = model_id_obj

            if stored_tag and version_id and created_by:
                # Match registry tags to stored tags (handles extended formats)
                for registry_tag in model_tags:
                    if registry_tag not in tag_to_model_info and model_tags_match(registry_tag, stored_tag):
                        tag_to_model_info[registry_tag] = (version_id, created_by, model_id)
                        if model_id and include_model_name:
                            normalized_model_id = normalize_object_id(model_id)
                            if normalized_model_id:
                                model_ids.add(normalized_model_id)
                        if include_owner_info:
                            created_by_ids.add(created_by)
                        break

        # Look up model names if requested
        model_id_to_name = {}
        if include_model_name and model_ids:
            object_ids = []
            id_to_str = {}
            for model_id_str in model_ids:
                try:
                    obj_id = ObjectId(model_id_str)
                    object_ids.append(obj_id)
                    id_to_str[str(obj_id)] = model_id_str
                except (ValueError, TypeError):
                    continue

            if object_ids:
                models = db.models.find({"_id": {"$in": object_ids}}, {"_id": 1, "name": 1})
                for model_doc in models:
                    model_id_str = id_to_str.get(str(model_doc["_id"]), normalize_object_id(model_doc["_id"]))
                    model_id_to_name[model_id_str] = model_doc.get("name", "Unknown")

        # Look up owner names and loginIds if requested
        owner_id_to_name = {}
        owner_id_to_login = {}
        if include_owner_info and created_by_ids:
            owner_id_to_name, owner_id_to_login = lookup_user_names_and_logins(created_by_ids)

        # Build final mapping
        for tag, (version_id, created_by_id, model_id) in tag_to_model_info.items():
            metadata_entry = {}

            if include_model_name:
                model_id_str = normalize_object_id(model_id) if model_id else ""
                metadata_entry["image_name"] = model_id_to_name.get(model_id_str, "Unknown")

            if include_owner_info:
                created_by_str = normalize_object_id(created_by_id)
                metadata_entry["user_name"] = owner_id_to_name.get(created_by_str, "Unknown")
                metadata_entry["owner_login_id"] = owner_id_to_login.get(created_by_str, "")

            tag_to_metadata[tag] = metadata_entry

        mongo_client.close()
    except Exception as e:
        from utils.logging_utils import get_logger

        logger = get_logger(__name__)
        logger.warning(f"Could not query MongoDB for model metadata: {e}")
        import traceback

        logger.debug(traceback.format_exc())

    return tag_to_metadata


def build_environment_tag_to_metadata_mapping(
    environment_tags: Set[str], include_env_name: bool = True, include_owner_info: bool = True
) -> Dict[str, Dict]:
    """Build a mapping from environment tags to their metadata (name and owner info).

    Args:
        environment_tags: Set of environment Docker tags
        include_env_name: If True, include environment name in result
        include_owner_info: If True, include owner name and login ID in result

    Returns:
        Dict mapping tag -> {'image_name': str, 'user_name': str, 'owner_login_id': str}
        Fields may be missing if not requested or not found
    """
    from utils.config_manager import config_manager
    from utils.mongo_utils import get_mongo_client

    tag_to_metadata = {}

    if not environment_tags:
        return tag_to_metadata

    try:
        mongo_client = get_mongo_client()
        db = mongo_client[config_manager.get_mongo_db()]

        # Query environment_revisions for tags
        revisions = db.environment_revisions.find(
            {"metadata.dockerImageName.tag": {"$in": list(environment_tags)}},
            {"_id": 1, "metadata.dockerImageName.tag": 1, "metadata.authorId": 1, "environmentId": 1},
        )

        # Build tag -> (environment_id, author_id) mapping
        tag_to_env_info = {}
        environment_ids = set()
        author_ids = set()

        for rev_doc in revisions:
            tag = rev_doc.get("metadata", {}).get("dockerImageName", {}).get("tag")
            environment_id = rev_doc.get("environmentId")
            author_id = rev_doc.get("metadata", {}).get("authorId")

            if tag and environment_id and author_id:
                tag_to_env_info[tag] = (environment_id, author_id)
                if include_env_name:
                    environment_ids.add(environment_id)
                if include_owner_info:
                    author_ids.add(author_id)

        # Look up environment names if requested
        env_id_to_name = {}
        if include_env_name and environment_ids:
            environments = db.environments_v2.find({"_id": {"$in": list(environment_ids)}}, {"_id": 1, "name": 1})
            for env_doc in environments:
                env_id_str = normalize_object_id(env_doc["_id"])
                env_id_to_name[env_id_str] = env_doc.get("name", "Unknown")

        # Look up owner names and loginIds if requested
        owner_id_to_name = {}
        owner_id_to_login = {}
        if include_owner_info and author_ids:
            owner_id_to_name, owner_id_to_login = lookup_user_names_and_logins(author_ids)

        # Build final mapping
        for tag, (env_id, author_id) in tag_to_env_info.items():
            metadata_entry = {}

            if include_env_name:
                env_id_str = normalize_object_id(env_id)
                metadata_entry["image_name"] = env_id_to_name.get(env_id_str, "Unknown")

            if include_owner_info:
                author_id_str = normalize_object_id(author_id)
                metadata_entry["user_name"] = owner_id_to_name.get(author_id_str, "Unknown")
                metadata_entry["owner_login_id"] = owner_id_to_login.get(author_id_str, "")

            tag_to_metadata[tag] = metadata_entry

        mongo_client.close()
    except Exception as e:
        from utils.logging_utils import get_logger

        logger = get_logger(__name__)
        logger.warning(f"Could not query MongoDB for environment metadata: {e}")
        import traceback

        logger.debug(traceback.format_exc())

    return tag_to_metadata
