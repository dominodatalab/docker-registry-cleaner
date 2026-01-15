import logging
import traceback
from typing import Optional


def setup_logging(level: int = logging.INFO, fmt: Optional[str] = None) -> None:
	"""Configure root logging once. Subsequent calls are no-ops.
	If fmt is not provided, a sensible default is used.
	"""
	if logging.getLogger().handlers:
		# Already configured; do nothing
		return
	format_str = fmt or '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
	logging.basicConfig(level=level, format=format_str)


def get_logger(name: Optional[str] = None) -> logging.Logger:
	"""Return a module/logger by name, after ensuring logging is configured."""
	setup_logging()
	return logging.getLogger(name) if name else logging.getLogger(__name__)


def log_exception(logger: logging.Logger, message: str = "An error occurred", exc_info: Exception = None) -> None:
	"""Centralized exception logging with full traceback.
	
	Args:
		logger: Logger instance to use
		message: Custom error message to log before the traceback
		exc_info: Exception instance (if None, uses current exception context)
	"""
	logger.error(message)
	if exc_info is not None:
		logger.error(f"Exception type: {type(exc_info).__name__}")
		logger.error(f"Exception message: {str(exc_info)}")
	logger.error("Full traceback:")
	logger.error(traceback.format_exc())
