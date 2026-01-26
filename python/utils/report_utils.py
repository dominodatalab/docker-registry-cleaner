"""
Utility functions for report generation, saving, and freshness checking.

This module provides functions to:
- Save reports in various formats (JSON, table+JSON)
- Check if reports are fresh
- Automatically generate reports when needed
- Generate timestamped report filenames
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from utils.config_manager import config_manager
from utils.logging_utils import get_logger

logger = get_logger(__name__)


# ============================================================================
# Formatting Utilities
# ============================================================================

def sizeof_fmt(num: float, suffix: str = "B") -> str:
	"""Format bytes into human-readable size.
	
	Args:
	    num: Number of bytes
	    suffix: Suffix to append (default: "B")
	
	Returns:
	    Formatted string like "1.5GiB", "500MiB", etc.
	"""
	for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
		if abs(num) < 1024.0:
			return f"{num:3.1f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f}Yi{suffix}"


# ============================================================================
# Timestamp Utilities
# ============================================================================

def get_timestamp_suffix() -> str:
    """
    Generate a timestamp suffix for report filenames.
    
    Returns:
        String in format: YYYY-MM-DD-HH-MM-SS
    """
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def add_timestamp_to_path(path: str, timestamp: Optional[str] = None) -> str:
    """
    Add a timestamp to a file path before the extension.
    
    Args:
        path: Original file path (e.g., 'reports/analysis.json')
        timestamp: Optional timestamp string (defaults to current time)
    
    Returns:
        Path with timestamp inserted (e.g., 'reports/analysis-2026-01-15-14-30-00.json')
    """
    if timestamp is None:
        timestamp = get_timestamp_suffix()
    
    p = Path(path)
    # Insert timestamp before extension
    stem = p.stem
    suffix = p.suffix
    return str(p.parent / f"{stem}-{timestamp}{suffix}")


def get_latest_report(report_pattern: str, reports_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Find the most recent report file matching a pattern.
    
    Args:
        report_pattern: Pattern to match (e.g., 'analysis-*.json')
        reports_dir: Directory to search (defaults to configured reports directory)
    
    Returns:
        Path to latest report file, or None if not found
    """
    if reports_dir is None:
        reports_dir = get_reports_dir()
    
    reports = list(reports_dir.glob(report_pattern))
    if not reports:
        return None
    
    # Sort by modification time, return most recent
    return max(reports, key=lambda p: p.stat().st_mtime)


# ============================================================================
# Report Saving Functions
# ============================================================================

def save_table_and_json(base_path: str, table_str: str, json_obj: Dict[str, Any], timestamp: bool = True) -> str:
    """
    Write a table string to <base>.txt and JSON object to <base>.json.
    
    Args:
        base_path: Base path for the reports (without extension)
        table_str: Table content to write
        json_obj: JSON object to write
        timestamp: If True, add timestamp to filenames (default: True)
    
    Returns:
        Path to the saved JSON file
    """
    # Use save_json to handle ObjectId serialization
    base = Path(base_path)
    
    # Add timestamp if requested
    if timestamp:
        timestamp_suffix = get_timestamp_suffix()
        base = base.parent / f"{base.name}-{timestamp_suffix}"
    
    # Ensure parent directory exists
    base.parent.mkdir(parents=True, exist_ok=True)
    
    # Write table
    with open(f"{base}.txt", "w") as f:
        f.write(table_str)
    
    # Write JSON using save_json to handle ObjectId serialization
    json_path = save_json(f"{base}.json", json_obj, timestamp=False)
    
    logger.info(f"Saved reports to {base}.txt and {base}.json")
    return json_path


def save_json(path: str, data: Any, timestamp: bool = False) -> str:
    """
    Write JSON data to a file with indentation.
    
    Handles MongoDB BSON types and Python types that aren't JSON serializable:
    - ObjectId: normalized to strings
    - datetime/date: converted to ISO format strings
    - set/frozenset: converted to lists
    - bytes: decoded to UTF-8 strings (or base64 if decode fails)
    - Decimal128: converted to string representation
    - Binary: converted to base64 string
    - UUID: converted to string
    
    Args:
        path: Path to save the JSON file
        data: Data to save
        timestamp: If True, add timestamp to filename (default: True)
    
    Returns:
        Path to the saved file
    """
    from bson import ObjectId
    from datetime import datetime, date
    from uuid import UUID
    import base64
    from utils.object_id_utils import normalize_object_id
    
    # Import MongoDB BSON types (may not be available in all pymongo versions)
    try:
        from bson import Decimal128, Binary
    except ImportError:
        # Fallback if these aren't available
        Decimal128 = None
        Binary = None
    
    def normalize_object_ids_in_data(data):
        """Recursively normalize non-JSON-serializable types in data structures.
        
        Converts:
        - ObjectId objects to normalized strings
        - datetime/date objects to ISO format strings
        - set/frozenset to lists
        - bytes to UTF-8 strings (or base64 if decode fails)
        - Decimal128 to string
        - Binary to base64 string
        - UUID to string
        """
        if isinstance(data, ObjectId):
            return normalize_object_id(data)
        elif isinstance(data, (datetime, date)):
            # Convert datetime/date objects to ISO format strings
            return data.isoformat()
        elif isinstance(data, (set, frozenset)):
            # Convert sets to lists (sorted for deterministic output)
            try:
                return [normalize_object_ids_in_data(item) for item in sorted(data)]
            except TypeError:
                # If items aren't sortable (e.g., mixed types), just convert to list
                return [normalize_object_ids_in_data(item) for item in data]
        elif isinstance(data, bytes):
            # Try to decode as UTF-8, fall back to base64 if it fails
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                return base64.b64encode(data).decode('ascii')
        elif Decimal128 is not None and isinstance(data, Decimal128):
            # Convert Decimal128 to string representation
            return str(data)
        elif Binary is not None and isinstance(data, Binary):
            # Convert Binary to base64 string
            return base64.b64encode(data).decode('ascii')
        elif isinstance(data, UUID):
            # Convert UUID to string
            return str(data)
        elif isinstance(data, dict):
            return {k: normalize_object_ids_in_data(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [normalize_object_ids_in_data(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(normalize_object_ids_in_data(item) for item in data)
        else:
            return data
    
    p = Path(path)
    
    # Add timestamp if requested
    if timestamp:
        p = Path(add_timestamp_to_path(str(p)))
    
    p.parent.mkdir(parents=True, exist_ok=True)
    
    # Normalize ObjectIds in the data before serialization
    normalized_data = normalize_object_ids_in_data(data)
    
    with open(p, 'w') as f:
        json.dump(normalized_data, f, indent=2)
    logger.info(f"Saved JSON to {p}")
    return str(p)


# ============================================================================
# Report Freshness and Generation Functions
# ============================================================================

def get_reports_dir() -> Path:
    """Get the reports directory path."""
    reports_dir = config_manager.get_output_dir()
    return Path(reports_dir)


def is_report_fresh(report_name: str, max_age_hours: int = 24) -> bool:
    """
    Check if a report file exists and is fresh (modified within max_age_hours).
    
    Supports both timestamped and non-timestamped report filenames.
    For timestamped reports, finds the most recent matching file.
    
    Args:
        report_name: Name of the report file (e.g., 'mongodb_usage_report.json')
        max_age_hours: Maximum age in hours before report is considered stale (default: 24)
    
    Returns:
        True if report exists and is fresh, False otherwise
    """
    reports_dir = get_reports_dir()
    
    # Try exact match first
    report_path = reports_dir / report_name
    if report_path.exists():
        mtime = datetime.fromtimestamp(report_path.stat().st_mtime)
        age = datetime.now() - mtime
        return age < timedelta(hours=max_age_hours)
    
    # Try to find timestamped version (e.g., mongodb_usage_report-2026-01-15-14-30-00.json)
    stem = Path(report_name).stem
    suffix = Path(report_name).suffix
    pattern = f"{stem}-*-*-*-*-*-*{suffix}"
    
    latest = get_latest_report(pattern, reports_dir)
    if latest:
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        age = datetime.now() - mtime
        return age < timedelta(hours=max_age_hours)
    
    return False


def ensure_mongodb_reports(max_age_hours: int = 24) -> None:
    """
    Ensure MongoDB usage reports are fresh, generating them if needed.
    
    Args:
        max_age_hours: Maximum age in hours before report is considered stale (default: 24)
    """
    report_name = 'mongodb_usage_report.json'
    
    if is_report_fresh(report_name, max_age_hours):
        logger.debug(f"MongoDB usage report is fresh (less than {max_age_hours} hours old)")
        return
    
    logger.info(f"MongoDB usage report is missing or stale (older than {max_age_hours} hours). Generating now...")
    
    try:
        from utils.extract_metadata import run
        run("all")
        logger.info("✓ MongoDB usage reports generated successfully")
    except Exception as e:
        logger.error(f"Failed to generate MongoDB usage reports: {e}")
        raise


def ensure_image_analysis_reports(max_age_hours: int = 24) -> None:
    """
    Ensure image analysis reports are fresh, generating them if needed.
    
    This generates reports for both 'environment' and 'model' image types.
    
    Args:
        max_age_hours: Maximum age in hours before report is considered stale (default: 24)
    """
    # Check if any of the key reports are fresh
    # Note: images-report is a directory, not a file, so we check tag-sums and layers-and-sizes
    key_reports = ['tag-sums.json', 'layers-and-sizes.json']
    any_fresh = any(is_report_fresh(report, max_age_hours) for report in key_reports)
    
    if any_fresh:
        logger.debug(f"Image analysis reports are fresh (less than {max_age_hours} hours old)")
        return
    
    logger.info(f"Image analysis reports are missing or stale (older than {max_age_hours} hours). Generating now...")
    
    try:
        from utils.image_data_analysis import ImageAnalyzer
        
        registry_url = config_manager.get_registry_url()
        repository = config_manager.get_repository()
        analyzer = ImageAnalyzer(registry_url, repository)
        
        # Analyze both environment and model images
        for image_type in ['environment', 'model']:
            logger.info(f"Analyzing {image_type} images...")
            success = analyzer.analyze_image(image_type)
            if not success:
                logger.warning(f"Failed to analyze {image_type} images")
        
        # Save reports
        analyzer.save_reports()
        logger.info("✓ Image analysis reports generated successfully")
    except Exception as e:
        logger.error(f"Failed to generate image analysis reports: {e}")
        raise


def ensure_all_reports(max_age_hours: int = 24) -> None:
    """
    Ensure all reports (MongoDB and image analysis) are fresh.
    
    Args:
        max_age_hours: Maximum age in hours before reports are considered stale (default: 24)
    """
    ensure_mongodb_reports(max_age_hours)
    ensure_image_analysis_reports(max_age_hours)
