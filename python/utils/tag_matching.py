#!/usr/bin/env python3
"""
Tag matching utilities for Docker image tags.

Provides functions for matching model tags that may have extended formats
like <modelId>-<version>-<timestamp>_<uniqueId>.
"""


def extract_model_tag_prefix(tag: str) -> str:
    """Extract the prefix from a model tag for matching purposes.
    
    Model tags can have formats like:
    - Simple: `<modelId>-<version>`
    - Extended: `<modelId>-<version>-<timestamp>_<uniqueId>`
    
    This function extracts the prefix (first two parts separated by '-')
    which is used for matching against stored tags in MongoDB.
    
    Args:
        tag: Model tag (e.g., "507f1f77bcf86cd799439011-v2-1234567890_abc123")
    
    Returns:
        Prefix string (e.g., "507f1f77bcf86cd799439011-v2")
    """
    parts = tag.split('-')
    if len(parts) >= 2:
        # Use first two parts as prefix (modelId-version)
        return '-'.join(parts[:2])
    # Single part tag, return as-is
    return tag


def model_tags_match(registry_tag: str, stored_tag: str) -> bool:
    """Check if a registry tag matches a stored tag from MongoDB.
    
    Handles both exact matches and prefix matches for extended tag formats.
    
    Args:
        registry_tag: Tag from Docker registry (may be extended format)
        stored_tag: Tag stored in MongoDB (may be simple format)
    
    Returns:
        True if tags match, False otherwise
    """
    # Exact match
    if registry_tag == stored_tag:
        return True
    
    # Case 1: Registry tag starts with stored tag + '-'
    # e.g., stored_tag = "507f1f77bcf86cd799439011-v2", 
    #       registry_tag = "507f1f77bcf86cd799439011-v2-1234567890_abc123"
    if registry_tag.startswith(stored_tag + '-'):
        return True
    
    # Case 2: Stored tag equals registry tag prefix
    # e.g., stored_tag = "507f1f77bcf86cd799439011-v2",
    #       registry_tag = "507f1f77bcf86cd799439011-v2-1234567890_abc123"
    registry_prefix = extract_model_tag_prefix(registry_tag)
    if stored_tag == registry_prefix:
        return True
    
    # Case 3: Stored tag starts with registry tag prefix + '-'
    # (less common, but handle for completeness)
    if stored_tag.startswith(registry_prefix + '-'):
        return True
    
    return False


def build_model_tag_query(tags: list) -> dict:
    """Build a MongoDB query for model tags that handles extended formats.
    
    Creates a query that matches tags either exactly or by prefix.
    
    Args:
        tags: List of model tags to match
    
    Returns:
        MongoDB query dict using $or with exact matches and regex patterns
    """
    if not tags:
        return {}
    
    # Build exact match conditions
    exact_conditions = [{'metadata.builds.slug.image.tag': tag} for tag in tags]
    
    # Build prefix match conditions (regex)
    regex_conditions = []
    for tag in tags:
        prefix = extract_model_tag_prefix(tag)
        # Match tags that start with the prefix followed by '-' or end with the prefix
        regex_conditions.append({
            'metadata.builds.slug.image.tag': {
                '$regex': f'^{prefix}(-|$)',
                '$options': 'i'
            }
        })
    
    # Combine all conditions with $or
    all_conditions = exact_conditions + regex_conditions
    
    if len(all_conditions) == 1:
        return all_conditions[0]
    
    return {'$or': all_conditions}
