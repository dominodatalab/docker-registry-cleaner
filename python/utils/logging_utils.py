import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

# Standard LogRecord fields — excluded from the JSON "extra" payload so that
# callers can safely pass arbitrary kwargs via extra={} without duplication.
_STDLIB_LOG_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


class StructuredFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Extra fields passed via logger.info("msg", extra={"key": "val"}) are merged
    into the top-level JSON object, making log lines queryable without regex
    parsing in Datadog, ELK, or similar aggregators.
    """

    def format(self, record: logging.LogRecord) -> str:
        super().format(record)  # populates record.message, exc_text, etc.
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _STDLIB_LOG_FIELDS}
        if extra:
            log_data.update(extra)
        if record.exc_text:
            log_data["exception"] = record.exc_text
        return json.dumps(log_data)


def setup_logging(level: int = logging.INFO, fmt: Optional[str] = None) -> None:
    """Configure root logging once. Subsequent calls are no-ops.

    When the LOG_FORMAT environment variable is set to "json", structured JSON
    output is used — suitable for log aggregators (Datadog, ELK, etc.).
    Otherwise a human-readable format is used, which is more convenient for
    interactive kubectl exec / kubectl logs sessions.
    """
    if logging.getLogger().handlers:
        return
    if os.environ.get("LOG_FORMAT") == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter())
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(level)
    else:
        format_str = fmt or "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
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
