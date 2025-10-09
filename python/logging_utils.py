import logging
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
