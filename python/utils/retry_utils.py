"""Retry utilities for network operations with exponential backof"""

import logging
import time
from enum import Enum
from functools import wraps
from typing import Callable, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableErrorType(Enum):
    """Types of errors that should trigger retries"""

    NETWORK = "network"  # Connection errors, timeouts
    TEMPORARY = "temporary"  # 5xx errors, rate limiting
    PERMANENT = "permanent"  # 4xx errors (except rate limiting), auth failures


def is_retryable_error(error: Exception, error_message: str = "") -> Tuple[bool, RetryableErrorType]:
    """Determine if an error is retryable and what type it is

    Args:
        error: The exception that occurred
        error_message: Optional error message string

    Returns:
        Tuple of (is_retryable, error_type)
    """
    error_str = str(error).lower()
    msg_str = error_message.lower()
    combined = f"{error_str} {msg_str}".lower()

    # Network/connection errors - always retryable
    network_indicators = [
        "connection",
        "timeout",
        "network",
        "dns",
        "resolve",
        "refused",
        "unreachable",
        "reset",
        "broken pipe",
        "no route to host",
        "temporary failure",
    ]
    if any(indicator in combined for indicator in network_indicators):
        return True, RetryableErrorType.NETWORK

    # HTTP 5xx errors - retryable (server errors)
    if "500" in combined or "502" in combined or "503" in combined or "504" in combined:
        return True, RetryableErrorType.TEMPORARY

    # Rate limiting - retryable
    if "429" in combined or "rate limit" in combined or "too many requests" in combined:
        return True, RetryableErrorType.TEMPORARY

    # Auth errors - not retryable (won't fix itself)
    if "401" in combined or "403" in combined or "unauthorized" in combined or "forbidden" in combined:
        return False, RetryableErrorType.PERMANENT

    # 404 errors - usually not retryable (resource doesn't exist)
    if "404" in combined or "not found" in combined:
        return False, RetryableErrorType.PERMANENT

    # Subprocess errors - check the return code and stderr
    if hasattr(error, "returncode"):
        # Return code 0 = success, non-zero = failure
        # Network issues often result in non-zero codes
        if error.returncode != 0:
            # Check stderr for network indicators
            if hasattr(error, "stderr") and error.stderr:
                stderr_lower = error.stderr.lower()
                if any(indicator in stderr_lower for indicator in network_indicators):
                    return True, RetryableErrorType.NETWORK
            # Default: treat as potentially retryable (could be transient)
            return True, RetryableErrorType.TEMPORARY

    # Kubernetes API errors
    if "ApiException" in str(type(error)):
        # 5xx errors are retryable
        if hasattr(error, "status") and error.status >= 500:
            return True, RetryableErrorType.TEMPORARY
        # 429 (rate limit) is retryable
        if hasattr(error, "status") and error.status == 429:
            return True, RetryableErrorType.TEMPORARY
        # 404 might be retryable if it's a pod not ready yet
        if hasattr(error, "status") and error.status == 404:
            # Could be pod not ready, so retryable
            return True, RetryableErrorType.TEMPORARY

    # Unknown errors - default to retryable (conservative approach)
    # Better to retry and fail than to give up too early
    return True, RetryableErrorType.TEMPORARY


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_errors: Optional[List[RetryableErrorType]] = None,
) -> Callable:
    """Decorator for retrying functions with exponential backoff

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 60.0)
        exponential_base: Base for exponential backoff (default: 2.0)
        jitter: Add random jitter to prevent thundering herd (default: True)
        retryable_errors: List of error types to retry (None = retry all retryable types)

    Returns:
        Decorator function
    """
    if retryable_errors is None:
        retryable_errors = [RetryableErrorType.NETWORK, RetryableErrorType.TEMPORARY]

    def decorator(func: Callable[..., T]) -> Callable[..., Optional[T]]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[T]:
            last_error = None

            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    # If result is None and we're not on the last attempt, retry
                    # (Some functions return None on failure)
                    if result is None and attempt < max_retries:
                        # Check if we should retry None results
                        # For now, we'll retry None results as they might indicate transient failures
                        logger.warning(
                            f"{func.__name__} returned None on attempt {attempt + 1}/{max_retries + 1}. " "Retrying..."
                        )
                    else:
                        if attempt > 0:
                            logger.info(f"{func.__name__} succeeded on attempt {attempt + 1}")
                        return result

                except Exception as e:
                    last_error = e
                    error_message = str(e)
                    if hasattr(e, "stderr") and e.stderr:
                        error_message = e.stderr

                    is_retryable, error_type = is_retryable_error(e, error_message)

                    # Check if this error type should be retried
                    if not is_retryable or error_type not in retryable_errors:
                        logger.error(f"{func.__name__} failed with non-retryable error ({error_type.value}): {e}")
                        raise

                    # If this was the last attempt, raise the error
                    if attempt >= max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts. "
                            f"Last error ({error_type.value}): {e}"
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(initial_delay * (exponential_base**attempt), max_delay)

                    # Add jitter to prevent thundering herd
                    if jitter:
                        import random

                        jitter_amount = delay * 0.1  # 10% jitter
                        delay = delay + random.uniform(-jitter_amount, jitter_amount)
                        delay = max(0.1, delay)  # Ensure delay is positive

                    logger.warning(
                        f"{func.__name__} failed on attempt {attempt + 1}/{max_retries + 1} "
                        f"({error_type.value} error: {e}). "
                        f"Retrying in {delay:.2f}s..."
                    )

                    time.sleep(delay)

            # Should never reach here, but just in case
            if last_error:
                logger.error(f"{func.__name__} exhausted all retries. Last error: {last_error}")
                raise last_error

            return None

        return wrapper

    return decorator


def retry_operation(
    operation: Callable[..., T],
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    operation_name: str = "operation",
) -> Optional[T]:
    """Retry an operation with exponential backoff (non-decorator version)

    Useful for retrying operations that can't be easily decorated.

    Args:
        operation: Callable to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        exponential_base: Base for exponential backoff
        jitter: Add random jitter
        operation_name: Name for logging purposes

    Returns:
        Result of operation or None if all retries failed
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = operation()
            if result is not None or attempt >= max_retries:
                if attempt > 0:
                    logger.info(f"{operation_name} succeeded on attempt {attempt + 1}")
                return result
        except Exception as e:
            last_error = e
            error_message = str(e)
            if hasattr(e, "stderr") and e.stderr:
                error_message = e.stderr

            is_retryable, error_type = is_retryable_error(e, error_message)

            if not is_retryable:
                logger.error(f"{operation_name} failed with non-retryable error: {e}")
                raise

            if attempt >= max_retries:
                logger.error(f"{operation_name} failed after {max_retries + 1} attempts: {e}")
                raise

            delay = min(initial_delay * (exponential_base**attempt), max_delay)
            if jitter:
                import random

                jitter_amount = delay * 0.1
                delay = delay + random.uniform(-jitter_amount, jitter_amount)
                delay = max(0.1, delay)

            logger.warning(
                f"{operation_name} failed on attempt {attempt + 1}/{max_retries + 1} "
                f"({error_type.value} error). Retrying in {delay:.2f}s..."
            )
            time.sleep(delay)

    if last_error:
        raise last_error
    return None
