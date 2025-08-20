from pathlib import Path
from typing import Any, Dict
import json
from logging_utils import get_logger

logger = get_logger(__name__)


def save_table_and_json(base_path: str, table_str: str, json_obj: Dict[str, Any]) -> None:
    """Write a table string to <base>.txt and JSON object to <base>.json."""
    base = Path(base_path)
    # Ensure parent directory exists
    base.parent.mkdir(parents=True, exist_ok=True)
    # Write table
    with open(f"{base}.txt", "w") as f:
        f.write(table_str)
    # Write JSON
    with open(f"{base}.json", "w") as f:
        json.dump(json_obj, f, indent=2)
    logger.info(f"Saved reports to {base}.txt and {base}.json")


def save_json(path: str, data: Any) -> None:
    """Write JSON data to a file with indentation."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved JSON to {p}")
