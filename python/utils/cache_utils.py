"""Caching utilities for expensive operations"""

import functools
import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, Optional, TypeVar, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

T = TypeVar('T')


class TTLCache:
    """Time-To-Live cache with automatic expiration"""
    
    def __init__(self, ttl_seconds: int = 3600, max_size: Optional[int] = None):
        """Initialize TTL cache
        
        Args:
            ttl_seconds: Time to live in seconds (default: 1 hour)
            max_size: Maximum number of items (None = unlimited)
        """
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._access_times: Dict[str, float] = {}
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired"""
        if key not in self._cache:
            return None
        
        value, expiry_time = self._cache[key]
        
        if time.time() > expiry_time:
            # Expired, remove it
            del self._cache[key]
            if key in self._access_times:
                del self._access_times[key]
            return None
        
        # Update access time for LRU eviction
        self._access_times[key] = time.time()
        return value
    
    def set(self, key: str, value: Any) -> None:
        """Set value in cache with TTL"""
        expiry_time = time.time() + self.ttl_seconds
        
        # If at max size, evict least recently used
        if self.max_size and len(self._cache) >= self.max_size and key not in self._cache:
            # Find least recently used key
            lru_key = min(self._access_times.items(), key=lambda x: x[1])[0]
            del self._cache[lru_key]
            del self._access_times[lru_key]
        
        self._cache[key] = (value, expiry_time)
        self._access_times[key] = time.time()
    
    def clear(self) -> None:
        """Clear all cached items"""
        self._cache.clear()
        self._access_times.clear()
    
    def remove(self, key: str) -> None:
        """Remove specific key from cache"""
        if key in self._cache:
            del self._cache[key]
        if key in self._access_times:
            del self._access_times[key]
    
    def size(self) -> int:
        """Get current cache size"""
        return len(self._cache)
    
    def cleanup_expired(self) -> int:
        """Remove expired entries and return count of removed items"""
        now = time.time()
        expired_keys = [
            key for key, (_, expiry_time) in self._cache.items()
            if now > expiry_time
        ]
        
        for key in expired_keys:
            del self._cache[key]
            if key in self._access_times:
                del self._access_times[key]
        
        return len(expired_keys)


# Global caches for different operation types
_tag_list_cache = TTLCache(ttl_seconds=1800, max_size=100)  # 30 minutes, 100 entries
_image_inspect_cache = TTLCache(ttl_seconds=3600, max_size=1000)  # 1 hour, 1000 entries
_mongo_query_cache = TTLCache(ttl_seconds=600, max_size=500)  # 10 minutes, 500 entries
_layer_calc_cache = TTLCache(ttl_seconds=7200, max_size=2000)  # 2 hours, 2000 entries


def cache_key(*args, **kwargs) -> str:
    """Generate a cache key from function arguments"""
    # Create a deterministic string representation
    key_parts = []
    
    # Add positional args
    for arg in args:
        if isinstance(arg, (str, int, float, bool, type(None))):
            key_parts.append(str(arg))
        elif isinstance(arg, (list, tuple)):
            key_parts.append(json.dumps(arg, sort_keys=True))
        elif isinstance(arg, dict):
            key_parts.append(json.dumps(arg, sort_keys=True))
        else:
            key_parts.append(str(hash(str(arg))))
    
    # Add keyword args
    if kwargs:
        sorted_kwargs = sorted(kwargs.items())
        key_parts.append(json.dumps(sorted_kwargs, sort_keys=True))
    
    # Create hash for long keys
    key_str = "|".join(key_parts)
    if len(key_str) > 200:
        return hashlib.md5(key_str.encode()).hexdigest()
    return key_str


def cached(cache: TTLCache, key_func: Optional[Callable] = None):
    """Decorator for caching function results
    
    Args:
        cache: TTLCache instance to use
        key_func: Optional function to generate cache key from args/kwargs
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Generate cache key - skip 'self' for methods
            if key_func:
                cache_key_str = key_func(*args, **kwargs)
            else:
                # For methods, exclude 'self' from cache key
                key_args = args[1:] if args and hasattr(args[0], func.__name__) else args
                cache_key_str = f"{func.__module__}.{func.__name__}:{cache_key(*key_args, **kwargs)}"
            
            # Try to get from cache
            cached_result = cache.get(cache_key_str)
            if cached_result is not None:
                logger.debug(f"Cache hit for {func.__name__}: {cache_key_str[:50]}...")
                return cached_result
            
            # Cache miss, execute function
            logger.debug(f"Cache miss for {func.__name__}: {cache_key_str[:50]}...")
            result = func(*args, **kwargs)
            
            # Store in cache (only if result is not None)
            if result is not None:
                cache.set(cache_key_str, result)
            
            return result
        
        # Add cache management methods to wrapper
        wrapper.cache = cache
        wrapper.clear_cache = lambda: cache.clear()
        wrapper.cache_size = lambda: cache.size()
        wrapper.cleanup_expired = lambda: cache.cleanup_expired()
        
        return wrapper
    return decorator


def cached_tag_list(ttl_seconds: int = 1800):
    """Decorator for caching tag list operations"""
    cache = TTLCache(ttl_seconds=ttl_seconds, max_size=100)
    return cached(cache)


def cached_image_inspect(ttl_seconds: int = 3600):
    """Decorator for caching image inspection operations"""
    cache = TTLCache(ttl_seconds=ttl_seconds, max_size=1000)
    return cached(cache)


def cached_mongo_query(ttl_seconds: int = 600):
    """Decorator for caching MongoDB query results"""
    cache = TTLCache(ttl_seconds=ttl_seconds, max_size=500)
    return cached(cache)


def cached_layer_calc(ttl_seconds: int = 7200):
    """Decorator for caching layer calculation results"""
    cache = TTLCache(ttl_seconds=ttl_seconds, max_size=2000)
    return cached(cache)


# Convenience functions for accessing global caches
def get_tag_list_cache() -> TTLCache:
    """Get the global tag list cache"""
    return _tag_list_cache


def get_image_inspect_cache() -> TTLCache:
    """Get the global image inspection cache"""
    return _image_inspect_cache


def get_mongo_query_cache() -> TTLCache:
    """Get the global MongoDB query cache"""
    return _mongo_query_cache


def get_layer_calc_cache() -> TTLCache:
    """Get the global layer calculation cache"""
    return _layer_calc_cache


def clear_all_caches() -> None:
    """Clear all global caches"""
    _tag_list_cache.clear()
    _image_inspect_cache.clear()
    _mongo_query_cache.clear()
    _layer_calc_cache.clear()
    logger.info("All caches cleared")


def cleanup_all_expired() -> Dict[str, int]:
    """Clean up expired entries in all caches"""
    results = {
        'tag_list': _tag_list_cache.cleanup_expired(),
        'image_inspect': _image_inspect_cache.cleanup_expired(),
        'mongo_query': _mongo_query_cache.cleanup_expired(),
        'layer_calc': _layer_calc_cache.cleanup_expired()
    }
    total = sum(results.values())
    if total > 0:
        logger.debug(f"Cleaned up {total} expired cache entries: {results}")
    return results


def get_cache_stats() -> Dict[str, Dict[str, Any]]:
    """Get statistics for all caches"""
    return {
        'tag_list': {
            'size': _tag_list_cache.size(),
            'max_size': _tag_list_cache.max_size,
            'ttl_seconds': _tag_list_cache.ttl_seconds
        },
        'image_inspect': {
            'size': _image_inspect_cache.size(),
            'max_size': _image_inspect_cache.max_size,
            'ttl_seconds': _image_inspect_cache.ttl_seconds
        },
        'mongo_query': {
            'size': _mongo_query_cache.size(),
            'max_size': _mongo_query_cache.max_size,
            'ttl_seconds': _mongo_query_cache.ttl_seconds
        },
        'layer_calc': {
            'size': _layer_calc_cache.size(),
            'max_size': _layer_calc_cache.max_size,
            'ttl_seconds': _layer_calc_cache.ttl_seconds
        }
    }
