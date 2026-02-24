#!/usr/bin/env python3
"""
Intelligent Docker Image Deletion Tool

This script analyzes workload usage patterns and safely deletes unused Docker images
from the registry while preserving all actively used ones.

Configuration:
  Registry password is sourced from (in priority order):
  1. REGISTRY_PASSWORD environment variable
  2. config.yaml registry.password field

Usage examples:
  # Delete a specific image (dry-run)
  python delete_image.py dominodatalab/environment:abc-123

  # Delete a specific image (apply)
  python delete_image.py dominodatalab/environment:abc-123 --apply

  # Analyze unused images (dry-run)
  python delete_image.py

  # Delete unused images directly
  python delete_image.py --apply

  # Force deletion without confirmation
  python delete_image.py --apply --force

  # Back up images to S3 before deletion
  python delete_image.py --apply --backup

  # Optional: Back up images to S3 with custom bucket and region
  python delete_image.py --apply --backup --s3-bucket my-backup-bucket --region us-east-1

  # Optional: Filter by ObjectIDs from file (requires prefixes: environment:, environmentRevision:, model:, or modelVersion:)
  python delete_image.py --apply --input object_ids.txt

  # Optional: Custom report paths
  python delete_image.py --image-analysis reports/analysis.json --output reports/deletion-analysis.json

# Optional: Clean up MongoDB references after deletion (disabled by default)
python delete_image.py --apply --mongo-cleanup
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from scripts.backup_restore import process_backup
from utils.config_manager import ConfigManager, SkopeoClient, config_manager
from utils.deletion_base import BaseDeletionScript
from utils.image_data_analysis import ImageAnalyzer
from utils.image_usage import ImageUsageService
from utils.logging_utils import get_logger, setup_logging
from utils.object_id_utils import normalize_object_id, read_typed_object_ids_from_file
from utils.report_utils import ensure_image_analysis_reports, ensure_mongodb_reports, save_json, sizeof_fmt


@dataclass
class WorkloadAnalysis:
    """Data class for workload analysis results"""

    used_images: Set[str]
    unused_images: Set[str]
    total_size_saved: int
    image_usage_stats: Dict[str, Dict]


@dataclass
class LayerAnalysis:
    """Data class for layer analysis results"""

    layer_id: str
    size: int
    tags: List[str]
    environments: List[str]
    is_used: bool


def merge_workload_analyses(a: WorkloadAnalysis, b: WorkloadAnalysis) -> WorkloadAnalysis:
    """Merge two WorkloadAnalysis results by unioning their image sets.

    Used when model-version and other image-type analyses are run separately
    and need to be combined into a single result for reporting and deletion.
    The total_size_saved is summed (each analysis calculates its own freed space
    independently via ImageAnalyzer, accounting for shared layers within its scope).
    """
    return WorkloadAnalysis(
        used_images=a.used_images | b.used_images,
        unused_images=a.unused_images | b.unused_images,
        total_size_saved=a.total_size_saved + b.total_size_saved,
        image_usage_stats={**a.image_usage_stats, **b.image_usage_stats},
    )


def _is_object_id_hex(s: str) -> bool:
    """Return True if *s* is a MongoDB ObjectId (24-char hex string).

    ObjectIds prefix registry tag names, so prefix matching (startswith) is correct.
    Non-ObjectId values — e.g., model slug image tags stored verbatim in the registry —
    are full tag names and require exact equality matching.
    """
    if len(s) != 24:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


class IntelligentImageDeleter(BaseDeletionScript):
    """Main class for intelligent Docker image deletion"""

    def __init__(
        self,
        registry_url: str = None,
        repository: str = None,
        namespace: str = None,
        enable_docker_deletion: bool = False,
        registry_statefulset: str = None,
    ):
        super().__init__(
            registry_url=registry_url,
            repository=repository,
            namespace=namespace,
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset=registry_statefulset,
        )

    def load_image_analysis_report(self, report_path: Optional[str] = None) -> Dict:
        """Load image analysis report from JSON file.

        Supports both timestamped and non-timestamped report files.
        If exact file doesn't exist, finds the most recent timestamped version.
        """
        from utils.report_utils import get_latest_report, get_reports_dir

        if report_path is None:
            report_path = config_manager.get_image_analysis_path()

        report_file = Path(report_path)

        # If exact file doesn't exist, try to find latest timestamped version
        if not report_file.exists():
            reports_dir = get_reports_dir()
            stem = report_file.stem
            suffix = report_file.suffix
            pattern = f"{stem}-*-*-*-*-*-*{suffix}"
            latest = get_latest_report(pattern, reports_dir)
            if latest:
                report_file = latest
                self.logger.info(f"Using latest timestamped report: {report_file.name}")

        try:
            with open(report_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Image analysis report not found: {report_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in image analysis report: {e}")
            return {}

    def load_mongodb_usage_reports(self) -> Dict[str, List[Dict]]:
        """Load MongoDB usage reports (runs, workspaces, models, projects, scheduler_jobs, organizations, app_versions) that contain Docker image tag references

        Uses ImageUsageService to load from consolidated report file.

        Returns:
            Dict with keys: 'runs', 'workspaces', 'models', 'projects', 'scheduler_jobs', 'organizations', 'app_versions' containing lists of records
        """
        service = ImageUsageService()
        reports = service.load_usage_reports()

        # Log what was loaded
        total_records = sum(len(v) for v in reports.values())
        if total_records > 0:
            self.logger.info(f"Loaded {total_records} MongoDB usage records:")
            self.logger.info(f"  - {len(reports.get('runs', []))} runs")
            self.logger.info(f"  - {len(reports.get('workspaces', []))} workspaces")
            self.logger.info(f"  - {len(reports.get('models', []))} models")
            self.logger.info(f"  - {len(reports.get('projects', []))} projects")
            self.logger.info(f"  - {len(reports.get('scheduler_jobs', []))} scheduler jobs")
            self.logger.info(f"  - {len(reports.get('organizations', []))} organizations")
            self.logger.info(f"  - {len(reports.get('app_versions', []))} app versions")
        else:
            self.logger.warning("No MongoDB usage reports found")
            self.logger.info("  Tip: Reports will be auto-generated when needed")

        return reports

    def _parse_mongodb_json(self, content: str) -> List[dict]:
        """Parse MongoDB extended JSON format to extract data"""
        # First try to parse as a standard JSON array
        try:
            # Try to clean and parse
            cleaned = content.strip()
            # Remove MongoDB-specific syntax if present
            cleaned = cleaned.replace("ObjectId(", "").replace("ISODate(", "").replace(")", "")
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            elif isinstance(parsed, dict):
                return [parsed]
        except Exception:
            pass

        # If that fails, return empty list
        return []

    def extract_docker_tags_from_mongodb_reports(self, mongodb_reports: Dict[str, List[Dict]]) -> Set[str]:
        """Extract Docker image tags from MongoDB usage reports (legacy method, kept for compatibility)

        Args:
            mongodb_reports: Dict with 'runs', 'workspaces', 'models' keys containing lists of records

        Returns:
            Set of Docker image tags (without type prefix, just the tag name)
        """
        tags, _ = self.extract_docker_tags_with_usage_info_from_mongodb_reports(mongodb_reports)
        return tags

    def _parse_timestamp(self, timestamp_str) -> Optional[datetime]:
        """Parse a timestamp string to datetime object

        Args:
            timestamp_str: ISO format timestamp string (may end with 'Z') or
                MongoDB extended JSON dict like {"$date": "..."}.

        Returns:
            datetime object or None if parsing fails
        """
        if not timestamp_str:
            return None
        try:
            # Handle MongoDB extended JSON: {"$date": "..."}
            if isinstance(timestamp_str, dict) and "$date" in timestamp_str:
                timestamp_str = timestamp_str["$date"]

            # Handle numeric epoch milliseconds (defensive, not expected from current pipelines)
            if isinstance(timestamp_str, (int, float)):
                # Assume milliseconds since epoch
                return datetime.fromtimestamp(timestamp_str / 1000.0, tz=timezone.utc)

            # At this point we expect a string
            if not isinstance(timestamp_str, str):
                return None

            # Handle ISO strings possibly ending with 'Z'
            ts = timestamp_str.replace("Z", "+00:00")
            return datetime.fromisoformat(ts)
        except Exception:
            return None

    def _get_most_recent_usage_date(self, usage_info: Dict) -> Optional[datetime]:
        """Get the most recent usage date from usage information

        Checks runs (last_used, completed, started), workspaces (workspace_last_change),
        and other sources to find the most recent timestamp.

        Args:
            usage_info: Dict with 'runs', 'workspaces', 'models', etc. containing usage records

        Returns:
            Most recent datetime or None if no timestamps found
        """
        most_recent = None

        # Check runs - prefer last_used, then completed, then started
        for run in usage_info.get("runs", []):
            for field in ["last_used", "completed", "started"]:
                ts = self._parse_timestamp(run.get(field))
                if ts:
                    if most_recent is None or ts > most_recent:
                        most_recent = ts
                    break  # Use first available timestamp per run

        # Check workspaces - use workspace_last_change
        for workspace in usage_info.get("workspaces", []):
            ts = self._parse_timestamp(workspace.get("workspace_last_change"))
            if ts:
                if most_recent is None or ts > most_recent:
                    most_recent = ts

        return most_recent

    def _filter_usage_by_age(self, usage_sources: Dict[str, Dict], recent_days: Optional[int]) -> Dict[str, Dict]:
        """Filter usage sources to only include recent usage if recent_days is specified

        Args:
            usage_sources: Dict mapping tag -> usage info
            recent_days: Optional number of days - if provided, only keep usage within this window

        Returns:
            Filtered usage_sources dict with only recent usage
        """
        if recent_days is None or recent_days <= 0:
            return usage_sources

        threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)
        filtered_usage = {}

        for tag, usage_info in usage_sources.items():
            # Get most recent usage date for this tag
            most_recent = self._get_most_recent_usage_date(usage_info)

            # If no timestamp found, keep the usage (conservative - don't delete if we can't determine age)
            if most_recent is None:
                filtered_usage[tag] = usage_info
                continue

            # If most recent usage is within threshold, keep it
            if most_recent >= threshold:
                # Filter individual usage records to only include recent ones
                filtered_info = {
                    "runs": [],
                    "workspaces": [],
                    "models": [],
                    "scheduler_jobs": [],
                    "projects": [],
                    "organizations": [],
                    "app_versions": [],
                }

                # Filter runs
                for run in usage_info.get("runs", []):
                    for field in ["last_used", "completed", "started"]:
                        ts = self._parse_timestamp(run.get(field))
                        if ts and ts >= threshold:
                            filtered_info["runs"].append(run)
                            break

                # Filter workspaces
                for workspace in usage_info.get("workspaces", []):
                    ts = self._parse_timestamp(workspace.get("workspace_last_change"))
                    if ts and ts >= threshold:
                        filtered_info["workspaces"].append(workspace)
                    elif ts is None:
                        # No timestamp - keep it (conservative)
                        filtered_info["workspaces"].append(workspace)

                # Keep models, scheduler_jobs, projects, organizations, app_versions as-is
                # (these don't have timestamps in the usage info, so we keep them if the tag has recent usage)
                filtered_info["models"] = usage_info.get("models", [])
                filtered_info["scheduler_jobs"] = usage_info.get("scheduler_jobs", [])
                filtered_info["projects"] = usage_info.get("projects", [])
                filtered_info["organizations"] = usage_info.get("organizations", [])
                filtered_info["app_versions"] = usage_info.get("app_versions", [])

                filtered_usage[tag] = filtered_info

        return filtered_usage

    def extract_docker_tags_with_usage_info_from_mongodb_reports(
        self, mongodb_reports: Dict[str, List[Dict]]
    ) -> Tuple[Set[str], Dict[str, Dict]]:
        """Extract Docker image tags from MongoDB usage reports with detailed usage information

        This method uses ImageUsageService to check runs, workspaces, models, scheduler_jobs, and projects.

        Args:
            mongodb_reports: Dict with 'runs', 'workspaces', 'models' keys containing lists of records

        Returns:
            Tuple of (set of Docker image tags, dict mapping tag -> usage info)
            Usage info contains: {'runs': [...], 'workspaces': [...], 'models': [...], 'scheduler_jobs': [...], 'projects': [...]}
        """
        service = ImageUsageService()
        tags, usage_info = service.extract_docker_tags_with_usage_info(mongodb_reports)
        return tags, usage_info

    def _generate_usage_summary(self, usage: Dict) -> str:
        """Generate a human-readable summary of why an image is in use

        Args:
            usage: Usage dictionary with 'runs', 'workspaces', 'models', 'scheduler_jobs', 'projects' info
                  Can have either count fields (runs_count, workspaces_count, models_count) or
                  list fields (runs, workspaces, models), or both.

        Returns:
            Human-readable string describing usage
        """
        reasons = []

        # Check runs - prefer count field, fall back to list length
        runs_count = usage.get("runs_count", 0)
        if runs_count == 0:
            runs_list = usage.get("runs", [])
            runs_count = len(runs_list) if runs_list else 0
        if runs_count > 0:
            reasons.append(f"{runs_count} execution{'s' if runs_count > 1 else ''} in MongoDB")

        # Check workspaces - prefer count field, fall back to list length
        workspaces_count = usage.get("workspaces_count", 0)
        if workspaces_count == 0:
            workspaces_list = usage.get("workspaces", [])
            workspaces_count = len(workspaces_list) if workspaces_list else 0
        if workspaces_count > 0:
            reasons.append(f"{workspaces_count} workspace{'s' if workspaces_count > 1 else ''}")

        # Check models - prefer count field, fall back to list length
        models_count = usage.get("models_count", 0)
        if models_count == 0:
            models_list = usage.get("models", [])
            models_count = len(models_list) if models_list else 0
        if models_count > 0:
            reasons.append(f"{models_count} model{'s' if models_count > 1 else ''}")

        # Check scheduler_jobs (always a list)
        scheduler_jobs = usage.get("scheduler_jobs", [])
        if scheduler_jobs:
            scheduler_count = len(scheduler_jobs)
            reasons.append(f"{scheduler_count} scheduler job{'s' if scheduler_count > 1 else ''}")

        # Check projects (always a list)
        projects = usage.get("projects", [])
        if projects:
            project_count = len(projects)
            reasons.append(f"{project_count} project{'s' if project_count > 1 else ''} using as default")

        # Check organizations (always a list)
        organizations = usage.get("organizations", [])
        if organizations:
            org_count = len(organizations)
            reasons.append(f"{org_count} organization{'s' if org_count > 1 else ''} using as default")

        # Check app_versions (always a list)
        app_versions = usage.get("app_versions", [])
        if app_versions:
            app_version_count = len(app_versions)
            reasons.append(f"{app_version_count} app version{'s' if app_version_count > 1 else ''}")

        if not reasons:
            # Try to provide more context about what we checked
            checked_fields = []
            if usage.get("runs") or usage.get("runs_count"):
                checked_fields.append("runs")
            if usage.get("workspaces") or usage.get("workspaces_count"):
                checked_fields.append("workspaces")
            if usage.get("models") or usage.get("models_count"):
                checked_fields.append("models")
            if usage.get("scheduler_jobs"):
                checked_fields.append("scheduler_jobs")
            if usage.get("projects"):
                checked_fields.append("projects")
            if usage.get("organizations"):
                checked_fields.append("organizations")
            if usage.get("app_versions"):
                checked_fields.append("app_versions")

            if checked_fields:
                return f"No usage found (checked: {', '.join(checked_fields)}, all empty)"
            else:
                return "No usage found (no usage data available)"

        return ", ".join(reasons)

    def analyze_image_usage(
        self,
        image_analysis: Dict,
        object_ids: Optional[List[str]] = None,
        object_ids_map: Optional[Dict[str, List[str]]] = None,
        mongodb_reports: Optional[Dict[str, List[Dict]]] = None,
        recent_days: Optional[int] = None,
    ) -> WorkloadAnalysis:
        """Analyze which images are used vs unused based on MongoDB reports and image analysis

        Args:
            image_analysis: Image analysis report
            object_ids: List of ObjectIDs to filter by (merged from all types)
            object_ids_map: Dict mapping image types to ObjectID lists (e.g., {'environment': [...], 'model': [...]})
            mongodb_reports: Optional MongoDB usage reports
            recent_days: Optional number of days - if provided, only consider images as "in-use" if they were used within the last N days
        """
        used_images = set()
        unused_images = set()
        total_size_saved = 0
        image_usage_stats = {}

        # Build mapping from ObjectID to image type if object_ids_map is provided
        oid_to_type = {}
        if object_ids_map:
            for img_type, oids in object_ids_map.items():
                # Map both 'environment' and 'environment_revision' to 'environment'
                # Map both 'model' and 'model_version' to 'model'
                repo_type = "environment" if img_type in ("environment", "environment_revision") else "model"
                for oid in oids:
                    oid_to_type[oid] = repo_type

        # Track usage sources for each image
        # Keys: 'runs', 'workspaces', 'models', 'scheduler_jobs', 'projects'
        usage_sources = {}  # Maps tag -> dict with usage info

        # Get used images from MongoDB reports (runs, workspaces, models, scheduler_jobs, projects)
        # This ensures we don't delete images referenced in execution history
        if mongodb_reports:
            mongodb_tags, mongodb_usage_info = self.extract_docker_tags_with_usage_info_from_mongodb_reports(
                mongodb_reports
            )
            if mongodb_tags:
                original_count = len(used_images)
                used_images.update(mongodb_tags)
                added_count = len(used_images) - original_count
                if added_count > 0:
                    self.logger.info(
                        f"Found {added_count} additional used images from MongoDB (runs/workspaces/models/scheduler_jobs/projects)"
                    )

                # Merge MongoDB usage info into usage_sources
                for tag, usage_info in mongodb_usage_info.items():
                    if tag not in usage_sources:
                        usage_sources[tag] = {
                            "runs": [],
                            "workspaces": [],
                            "models": [],
                            "scheduler_jobs": [],
                            "projects": [],
                            "organizations": [],
                            "app_versions": [],
                        }
                    usage_sources[tag]["runs"].extend(usage_info.get("runs", []))
                    usage_sources[tag]["workspaces"].extend(usage_info.get("workspaces", []))
                    usage_sources[tag]["models"].extend(usage_info.get("models", []))
                    usage_sources[tag]["scheduler_jobs"].extend(usage_info.get("scheduler_jobs", []))
                    usage_sources[tag]["projects"].extend(usage_info.get("projects", []))
                    usage_sources[tag]["organizations"].extend(usage_info.get("organizations", []))
                    usage_sources[tag]["app_versions"].extend(usage_info.get("app_versions", []))
            else:
                self.logger.info("No Docker tags found in MongoDB reports")
        else:
            self.logger.warning(
                "MongoDB reports not provided - images referenced in runs/workspaces/models/scheduler_jobs/projects may be incorrectly marked as unused"
            )

        # Filter usage by age if recent_days is specified
        if recent_days is not None and recent_days > 0:
            self.logger.info(f"Filtering usage to only include activity within the last {recent_days} days")
            threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)
            original_used_count = len(used_images)

            # Rebuild used_images set based on filtered usage
            # Only include tags that have recent usage or usage from sources without timestamps
            filtered_used_images = set()
            for tag in used_images:
                usage_info = usage_sources.get(tag, {})

                # Check for usage from sources without timestamps (always keep these - they represent current config)
                has_config_usage = (
                    len(usage_info.get("models", [])) > 0
                    or len(usage_info.get("scheduler_jobs", [])) > 0
                    or len(usage_info.get("projects", [])) > 0
                    or len(usage_info.get("organizations", [])) > 0
                    or len(usage_info.get("app_versions", [])) > 0
                )

                if has_config_usage:
                    # Keep tags with current configuration usage (conservative)
                    filtered_used_images.add(tag)
                    continue

                # Check for recent usage from runs or workspaces (with timestamps)
                has_recent_usage = False
                most_recent = self._get_most_recent_usage_date(usage_info)
                if most_recent is not None and most_recent >= threshold:
                    has_recent_usage = True

                if has_recent_usage:
                    filtered_used_images.add(tag)

            removed_count = original_used_count - len(filtered_used_images)
            used_images = filtered_used_images
            self.logger.info(
                f"After age filtering: {len(used_images)} tags have recent usage or current configuration ({removed_count} tags removed due to old usage only)"
            )

        # Filter by ObjectIDs if provided
        if object_ids is not None:
            original_used_count = len(used_images)
            filtered_used_images = set()
            for image in used_images:
                for obj_id in object_ids:
                    if _is_object_id_hex(obj_id):
                        match = image.startswith(obj_id)
                    else:
                        match = image == obj_id
                    if match:
                        filtered_used_images.add(image)
                        break
            used_images = filtered_used_images
            self.logger.info(f"Filtered used images: {len(used_images)}/{original_used_count} match ObjectIDs")

        # Analyze image layers from image analysis
        # Support format produced by image_data_analysis (mapping of layer_id -> { size, tags, environments })
        all_tags = set()
        for layer_id, layer_info in image_analysis.items():
            layer_tags = layer_info.get("tags", [])

            # Filter tags by ObjectIDs if provided and determine image type
            if object_ids is not None:
                original_tag_count = len(layer_tags)
                filtered_tags = []
                for tag in layer_tags:
                    matched = False
                    matched_type = None
                    for obj_id in object_ids:
                        if _is_object_id_hex(obj_id):
                            tag_matches = tag.startswith(obj_id)
                        else:
                            tag_matches = tag == obj_id
                        if tag_matches:
                            filtered_tags.append(tag)
                            matched = True
                            # Determine image type from object_ids_map if available
                            if object_ids_map:
                                matched_type = oid_to_type.get(obj_id)
                                # Defensive fallback: non-ObjectId filter keys come exclusively from
                                # object_ids_map["model"] (resolved slug tags), so if the type lookup
                                # misses for some reason, we can safely default to "model".
                                if matched_type is None and not _is_object_id_hex(obj_id):
                                    matched_type = "model"
                            break
                    # Store tag with type prefix if we know the type
                    if matched and matched_type:
                        all_tags.add(f"{matched_type}:{tag}")
                    elif matched:
                        self.logger.warning(
                            f"Tag '{tag}' matched filter '{obj_id}' but type could not be determined; "
                            "it will be treated as ambiguous (tried as both environment and model)"
                        )
                        all_tags.add(tag)
                layer_tags = filtered_tags
                if len(filtered_tags) < original_tag_count:
                    self.logger.info(
                        f"Filtered layer {layer_id}: {len(filtered_tags)}/{original_tag_count} tags match ObjectIDs"
                    )
            else:
                # No filtering - just add all tags (we don't know the type)
                for tag in layer_tags:
                    all_tags.add(tag)

            # NOTE: We don't calculate freed_bytes here anymore - it's calculated separately
            # using ImageAnalyzer to properly account for shared layers

        # Deletion candidates: all tags not referenced by workloads
        # For tags with type prefix, compare just the tag part
        unused_images = set()
        for full_tag in all_tags:
            # Extract just the tag name for comparison with used_images
            if ":" in full_tag:
                tag_name = full_tag.split(":", 1)[1]
            else:
                tag_name = full_tag

            if tag_name not in used_images:
                unused_images.add(full_tag)

        # Calculate freed space correctly using ImageAnalyzer (accounts for shared layers)
        # This also returns individual tag sizes
        total_size_saved, individual_tag_sizes = self._calculate_freed_space_correctly(unused_images, object_ids_map)

        # Build per-tag stats with accurate sizes and usage information
        for full_tag in all_tags:
            # Extract tag name for stats lookup
            tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
            # Get individual size from calculation (0 if not found)
            tag_size = individual_tag_sizes.get(full_tag, 0)

            # Get usage information for this tag. Not all tags will have all usage keys
            # populated (e.g., tags referenced only by scheduler_jobs/projects), so we
            # must use .get() with sensible defaults for safety.
            tag_usage = usage_sources.get(
                tag_name,
                {
                    "runs": [],
                    "workspaces": [],
                    "models": [],
                    "scheduler_jobs": [],
                    "projects": [],
                    "organizations": [],
                    "app_versions": [],
                },
            )

            image_usage_stats[full_tag] = {
                "size": tag_size,
                "layer_id": "",
                "status": "used" if tag_name in used_images else "unused",
                "usage": {
                    "runs_count": len(tag_usage.get("runs", [])),
                    "runs": tag_usage.get("runs", [])[:5],  # Limit to first 5 for display
                    "workspaces_count": len(tag_usage.get("workspaces", [])),
                    "workspaces": tag_usage.get("workspaces", [])[:5],  # Limit to first 5 for display
                    "models_count": len(tag_usage.get("models", [])),
                    "models": tag_usage.get("models", [])[:5],  # Limit to first 5 for display
                    "scheduler_jobs": tag_usage.get("scheduler_jobs", []),
                    "projects": tag_usage.get("projects", []),
                    "organizations": tag_usage.get("organizations", []),
                    "app_versions": tag_usage.get("app_versions", []),
                },
            }

        # Also create stats entries for tags in used_images that aren't in all_tags
        # This ensures we have usage information for all used images, even if they're not in the image analysis
        for used_tag in used_images:
            # Check if we already have stats for this tag (either directly or via full_tag)
            has_stats = False
            if used_tag in image_usage_stats:
                has_stats = True
            else:
                # Check if any full_tag matches this tag name
                for full_tag in image_usage_stats.keys():
                    tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
                    if tag_name == used_tag:
                        has_stats = True
                        break

            if not has_stats:
                # Create stats entry for this used tag
                tag_usage = usage_sources.get(
                    used_tag,
                    {
                        "runs": [],
                        "workspaces": [],
                        "models": [],
                        "scheduler_jobs": [],
                        "projects": [],
                        "organizations": [],
                        "app_versions": [],
                    },
                )

                image_usage_stats[used_tag] = {
                    "size": 0,  # Size unknown if not in image analysis
                    "layer_id": "",
                    "status": "used",
                    "usage": {
                        "runs_count": len(tag_usage.get("runs", [])),
                        "runs": tag_usage.get("runs", [])[:5],
                        "workspaces_count": len(tag_usage.get("workspaces", [])),
                        "workspaces": tag_usage.get("workspaces", [])[:5],
                        "models_count": len(tag_usage.get("models", [])),
                        "models": tag_usage.get("models", [])[:5],
                        "scheduler_jobs": tag_usage.get("scheduler_jobs", []),
                        "projects": tag_usage.get("projects", []),
                        "organizations": tag_usage.get("organizations", []),
                        "app_versions": tag_usage.get("app_versions", []),
                    },
                }

        return WorkloadAnalysis(
            used_images=used_images,
            unused_images=unused_images,
            total_size_saved=total_size_saved,
            image_usage_stats=image_usage_stats,
        )

    def analyze_model_version_usage(
        self,
        resolved_slug_tags: List[str],
        object_ids_map: Dict[str, List[str]],
        mongodb_reports: Dict[str, List[Dict]],
    ) -> "WorkloadAnalysis":
        """Analyze model version images directly from resolved slug tags.

        This is used when the input contains only modelVersion: IDs.  Unlike
        analyze_image_usage, this method does NOT consult the combined
        image_analysis (final-report.json) at all — doing so would pull
        environment images into scope and produce false positives.

        Instead it:
        1. Treats the resolved slug tags as the complete set of candidates.
        2. Marks any slug that is currently deployed (found in
           model_active_versions in the MongoDB report) as "in use".
        3. Delegates size calculation to _calculate_freed_space_correctly.

        Args:
            resolved_slug_tags: Docker image tags resolved from model_version IDs.
            object_ids_map: Full typed ID map (used by size calculation).
            mongodb_reports: Pre-loaded MongoDB usage reports.

        Returns:
            WorkloadAnalysis with used_images, unused_images, sizes, and stats.
        """
        # Build the complete candidate set with type prefix
        all_tags: Set[str] = {f"model:{slug}" for slug in resolved_slug_tags}

        # Collect the slug tags of CURRENTLY DEPLOYED model versions from the report
        deployed_slugs: Set[str] = set()
        for record in mongodb_reports.get("models", []):
            for version in record.get("model_active_versions", []):
                tag = version.get("model_environment_tag")
                if tag:
                    deployed_slugs.add(tag)

        # Unused = candidates not currently deployed
        unused_images: Set[str] = {full_tag for full_tag in all_tags if full_tag.split(":", 1)[1] not in deployed_slugs}

        self.logger.info(
            f"Model version usage: {len(all_tags)} candidates, "
            f"{len(all_tags) - len(unused_images)} currently deployed (in use), "
            f"{len(unused_images)} unused (deletion candidates)"
        )

        # Calculate freed space using ImageAnalyzer
        total_size_saved, individual_tag_sizes = self._calculate_freed_space_correctly(unused_images, object_ids_map)

        # Build per-tag stats
        image_usage_stats: Dict[str, Dict] = {}
        for full_tag in all_tags:
            slug = full_tag.split(":", 1)[1]
            is_deployed = slug in deployed_slugs
            image_usage_stats[full_tag] = {
                "size": individual_tag_sizes.get(full_tag, 0),
                "layer_id": "",
                "status": "used" if is_deployed else "unused",
                "usage": {
                    "runs_count": 0,
                    "runs": [],
                    "workspaces_count": 0,
                    "workspaces": [],
                    "models_count": 1 if is_deployed else 0,
                    "models": [{"deployed": True}] if is_deployed else [],
                    "scheduler_jobs": [],
                    "projects": [],
                    "organizations": [],
                    "app_versions": [],
                },
            }

        return WorkloadAnalysis(
            used_images=deployed_slugs,
            unused_images=unused_images,
            total_size_saved=total_size_saved,
            image_usage_stats=image_usage_stats,
        )

    def _calculate_freed_space_correctly(
        self, unused_images: Set[str], object_ids_map: Optional[Dict[str, List[str]]] = None
    ) -> Tuple[int, Dict[str, int]]:
        """Calculate freed space correctly using ImageAnalyzer, accounting for shared layers.

        This method analyzes ALL images (not just unused ones) to get accurate reference counts,
        then calculates what would be freed by deleting the unused images. Only layers that would
        have zero references after deletion are counted.

        Args:
            unused_images: Set of unused image tags (format: "type:tag" or just "tag")
            object_ids_map: Optional map of image types to ObjectID lists (for logging)

        Returns:
            Tuple of (total_bytes_freed, dict mapping image_tag to individual_bytes_freed)
            Returns (0, {}) if calculation fails
        """
        if not unused_images:
            return 0, {}

        try:
            self.logger.info("Calculating accurate freed space using ImageAnalyzer (analyzing ALL images)...")
            self.logger.info("This ensures shared layers between unused and used images are properly accounted for.")

            # Create ImageAnalyzer
            analyzer = ImageAnalyzer(self.registry_url, self.repository)

            # CRITICAL: Analyze ALL images (not just unused ones) to get accurate reference counts
            # This ensures that shared layers between unused and used images are properly accounted for
            image_types = ["environment", "model"]
            for image_type in image_types:
                self.logger.info(f"Analyzing ALL {image_type} images...")
                success = analyzer.analyze_image(image_type, object_ids=None)
                if not success:
                    self.logger.warning(f"Failed to analyze {image_type} images")

            # Build list of image_ids from unused images
            # ImageAnalyzer uses format "image_type:tag" as image_id
            image_ids = []
            skipped_tags = []
            tag_mapping = {}  # Map image_id back to original full_tag for size reporting
            for full_tag in unused_images:
                if ":" in full_tag:
                    # Format: "type:tag"
                    image_ids.append(full_tag)
                    tag_mapping[full_tag] = full_tag
                else:
                    # Legacy format: just tag name, we don't know the type
                    # Skip these or try both - for now, skip with a warning
                    skipped_tags.append(full_tag)

            if skipped_tags:
                self.logger.warning(
                    f"Skipping {len(skipped_tags)} tags without type prefix for freed space calculation"
                )

            if not image_ids:
                self.logger.warning("No tags with type information found, cannot calculate freed space accurately")
                return 0, {}

            # Calculate individual tag sizes first (layers unique to each image)
            self.logger.info(f"Calculating individual image sizes for {len(image_ids)} images...")
            individual_sizes = {}
            for image_id in image_ids:
                # Calculate what would be freed if only this image was deleted
                size_bytes = analyzer.freed_space_if_deleted([image_id])
                individual_sizes[tag_mapping[image_id]] = size_bytes

            # Calculate total freed space using ImageAnalyzer's method
            # This properly accounts for shared layers - only counts layers that would have
            # zero references after deletion (i.e., not used by any remaining images)
            total_freed = analyzer.freed_space_if_deleted(image_ids)

            self.logger.info(f"Total space that would be freed: {sizeof_fmt(total_freed)}")

            return total_freed, individual_sizes

        except Exception as e:
            self.logger.error(f"Error calculating freed space: {e}")
            from utils.logging_utils import log_exception

            log_exception(self.logger, "Error calculating freed space", exc_info=e)
            return 0, {}

    def generate_deletion_report(self, analysis: WorkloadAnalysis, output_file: str = "deletion-analysis.json") -> None:
        """Generate a detailed deletion analysis report"""
        report = {
            "summary": {
                "total_images_analyzed": len(analysis.used_images) + len(analysis.unused_images),
                "used_images": len(analysis.used_images),
                "unused_images": len(analysis.unused_images),
                "total_size_saved": analysis.total_size_saved,
                "total_size_saved_gb": round(analysis.total_size_saved / (1024**3), 2),
            },
            "unused_images": [],
        }

        # Add details for each unused image
        for image_tag in analysis.unused_images:
            stats = analysis.image_usage_stats.get(image_tag, {})
            size_bytes = stats.get("size", 0)
            usage = stats.get("usage", {})
            unused_entry = {
                "tag": image_tag,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024**3), 2),
                "status": stats.get("status", "unused"),
                "usage": usage,
            }
            # Only include layer_id if it's non-empty
            layer_id = stats.get("layer_id", "")
            if layer_id:
                unused_entry["layer_id"] = layer_id
            report["unused_images"].append(unused_entry)

        # Add details for each used image (images that can't be deleted)
        # For used images, we only include summary info (counts and why_cannot_delete)
        # to keep file size manageable - full usage details are available in unused_images
        report["used_images"] = []
        for image_tag in analysis.used_images:
            # Find matching stats - need to search through all stats
            matching_stats = None
            for full_tag, stats in analysis.image_usage_stats.items():
                tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
                if tag_name == image_tag:
                    matching_stats = stats
                    break

            if matching_stats:
                size_bytes = matching_stats.get("size", 0)
                usage = matching_stats.get("usage", {})
                # Include only summary info - counts and why_cannot_delete
                # Full usage details removed to reduce file size (available in unused_images if needed)
                used_entry = {
                    "tag": image_tag,
                    "size_bytes": size_bytes,
                    "size_gb": round(size_bytes / (1024**3), 2),
                    "status": "used",
                    "runs_count": usage.get("runs_count", 0),
                    "workspaces_count": usage.get("workspaces_count", 0),
                    "models_count": usage.get("models_count", 0),
                    "scheduler_jobs_count": len(usage.get("scheduler_jobs", [])),
                    "projects_count": len(usage.get("projects", [])),
                    "organizations_count": len(usage.get("organizations", [])),
                    "app_versions_count": len(usage.get("app_versions", [])),
                    "why_cannot_delete": self._generate_usage_summary(usage),
                }
                report["used_images"].append(used_entry)

        try:
            save_json(output_file, report)
            self.logger.info(f"Deletion analysis report saved to: {output_file}")
        except Exception as e:
            self.logger.error(f"Failed to save deletion report: {e}")

        # Log summary
        self.logger.info("\n📊 Deletion Analysis Summary:")
        self.logger.info(f"   Total images analyzed: {report['summary']['total_images_analyzed']}")
        self.logger.info(f"   Images in use: {report['summary']['used_images']}")
        self.logger.info(f"   Images unused: {report['summary']['unused_images']}")
        total_saved_bytes = report["summary"].get("total_size_saved_bytes")
        if total_saved_bytes is None:
            total_saved_bytes = report["summary"].get("total_size_saved_gb", 0) * (1024**3)
        self.logger.info(f"   Potential space saved: {sizeof_fmt(total_saved_bytes)}")

    def save_deletion_results(
        self,
        analysis: WorkloadAnalysis,
        deleted_tags: List[str],
        successful_deletions: int,
        failed_deletions: int,
        total_size_deleted: int,
        dry_run: bool,
        output_file: str = None,
    ) -> str:
        """Save deletion results to a JSON file.

        Args:
            analysis: WorkloadAnalysis object with image usage information
            deleted_tags: List of successfully deleted image tags
            successful_deletions: Number of successful deletions
            failed_deletions: Number of failed deletions
            total_size_deleted: Total size of deleted images (bytes)
            dry_run: Whether this was a dry run
            output_file: Optional path for output file (defaults to config-managed path)

        Returns:
            Path to the saved JSON file
        """
        if output_file is None:
            if dry_run:
                output_file = config_manager.get_output_dir() + "/deletion-dry-run-results.json"
            else:
                output_file = config_manager.get_output_dir() + "/deletion-results.json"

        # Build results report
        results = {
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "summary": {
                "total_images_analyzed": len(analysis.used_images) + len(analysis.unused_images),
                "used_images": len(analysis.used_images),
                "unused_images": len(analysis.unused_images),
                "successful_deletions": successful_deletions,
                "failed_deletions": failed_deletions,
                "total_size_deleted_bytes": total_size_deleted,
                "total_size_deleted_gb": round(total_size_deleted / (1024**3), 2),
                "total_size_saved_bytes": analysis.total_size_saved,
                "total_size_saved_gb": round(analysis.total_size_saved / (1024**3), 2),
            },
            "deleted_images": [],
            "failed_deletions": [],
            "used_images": [],
        }

        # Add successfully deleted images
        # deleted_tags are in format "repository/type:tag", need to match with image_tag format "type:tag"
        deleted_tags_normalized = set()
        for dt in deleted_tags:
            # Extract the "type:tag" part from "repository/type:tag"
            if "/" in dt and ":" in dt:
                parts = dt.split("/", 1)
                if ":" in parts[1]:
                    deleted_tags_normalized.add(parts[1])  # Add "type:tag"
            elif ":" in dt:
                deleted_tags_normalized.add(dt)  # Already in "type:tag" format
            else:
                deleted_tags_normalized.add(dt)  # Legacy format

        for image_tag in sorted(analysis.unused_images):
            # Find matching stats - stats may be keyed by full_tag (type:tag) or just tag
            stats = analysis.image_usage_stats.get(image_tag, {})
            if not stats:
                # Try to find by matching tag name (strip type prefix from full_tag keys)
                for full_tag, tag_stats in analysis.image_usage_stats.items():
                    tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
                    if tag_name == image_tag or full_tag == image_tag:
                        stats = tag_stats
                        break

            size_bytes = stats.get("size", 0) if stats else 0
            usage = stats.get("usage", {}) if stats else {}

            # Check if this image was successfully deleted
            # For dry_run, all unused images are considered "would be deleted"
            is_deleted = (image_tag in deleted_tags_normalized) if not dry_run else True

            image_result = {
                "tag": image_tag,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024**3), 2),
                "status": "deleted" if is_deleted else "failed",
                "runs_count": usage.get("runs_count", 0),
                "workspaces_count": usage.get("workspaces_count", 0),
                "models_count": usage.get("models_count", 0),
                "scheduler_jobs_count": len(usage.get("scheduler_jobs", [])),
                "projects_count": len(usage.get("projects", [])),
                "organizations_count": len(usage.get("organizations", [])),
                "app_versions_count": len(usage.get("app_versions", [])),
                "usage": usage,
            }

            if image_result["status"] == "deleted":
                results["deleted_images"].append(image_result)
            else:
                results["failed_deletions"].append(image_result)

        # Also include images that were in use (not deleted) with full usage details
        for image_tag in sorted(analysis.used_images):
            # Find matching stats - need to search through all stats
            # used_images contains tag names without type prefix, but stats may be keyed by full_tag (type:tag)
            matching_stats = None
            if image_tag in analysis.image_usage_stats:
                matching_stats = analysis.image_usage_stats[image_tag]
            else:
                # Search through all stats to find match by tag name
                for full_tag, stats in analysis.image_usage_stats.items():
                    tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
                    if tag_name == image_tag:
                        matching_stats = stats
                        break

            size_bytes = matching_stats.get("size", 0) if matching_stats else 0
            usage = matching_stats.get("usage", {}) if matching_stats else {}

            results["used_images"].append(
                {
                    "tag": image_tag,
                    "size_bytes": size_bytes,
                    "size_gb": round(size_bytes / (1024**3), 2),
                    "status": "used",
                    "runs_count": usage.get("runs_count", 0),
                    "workspaces_count": usage.get("workspaces_count", 0),
                    "models_count": usage.get("models_count", 0),
                    "scheduler_jobs_count": len(usage.get("scheduler_jobs", [])),
                    "projects_count": len(usage.get("projects", [])),
                    "organizations_count": len(usage.get("organizations", [])),
                    "app_versions_count": len(usage.get("app_versions", [])),
                    "usage": usage,
                    "why_cannot_delete": (
                        self._generate_usage_summary(usage) if usage else "Referenced in system (source unknown)"
                    ),
                }
            )

        try:
            save_json(output_file, results)
            self.logger.info(f"Deletion results saved to: {output_file}")
            return output_file
        except Exception as e:
            self.logger.error(f"Failed to save deletion results: {e}")
            return output_file

    def enable_deletion_of_docker_images(self):
        """Enable deletion of Docker images in the registry"""
        return self.enable_registry_deletion()

    def disable_deletion_of_docker_images(self):
        """Disable deletion of Docker images in the registry"""
        return self.disable_registry_deletion()

    def delete_unused_images(
        self,
        analysis: WorkloadAnalysis,
        password: str,
        dry_run: bool = True,
        backup: bool = False,
        s3_bucket: str = None,
        region: str = "us-west-2",
    ) -> List[str]:
        """Delete unused images based on analysis. Returns list of successfully deleted image tags.

        Args:
            analysis: WorkloadAnalysis object containing used/unused image information
            password: Registry password (optional)
            dry_run: If True, only simulate deletion without actually deleting
            backup: Whether to backup images to S3 before deletion
            s3_bucket: S3 bucket name for backups
            region: AWS region for S3 and ECR operations
        """
        if not analysis.unused_images:
            self.logger.info("No unused images found to delete.")
            return []

        # Backup images to S3 if requested (only in non-dry-run mode)
        if not dry_run and backup and s3_bucket:
            self.logger.info(f"\n📦 Backing up {len(analysis.unused_images)} images to S3 bucket: {s3_bucket}")

            # Extract tags from unused images
            # Unused images are in format: type:tag (e.g., "environment:abc123...") or just "tag" (legacy)
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                # Handle format: "type:tag" or just "tag"
                if ":" in image_tag:
                    tag = image_tag.split(":", 1)[1]
                else:
                    tag = image_tag
                tags_to_backup.append(tag)

            full_repo = f"{self.registry_url}/{self.repository}"

            # Initialize ConfigManager and SkopeoClient for backup
            cfg_mgr = ConfigManager()
            backup_skopeo_client = SkopeoClient(cfg_mgr)

            # Call process_backup from backup_restore
            try:
                process_backup(
                    skopeo_client=backup_skopeo_client,
                    full_repo=full_repo,
                    tags=tags_to_backup,
                    s3_bucket=s3_bucket,
                    region=region,
                    dry_run=False,
                    delete=False,  # Don't delete yet, we'll do that below
                    min_age_days=None,
                    workers=1,
                    tmpdir=None,
                    failed_tags_file=None,
                )
                self.logger.info(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as backup_err:
                self.logger.error(f"❌ Backup failed: {backup_err}")
                self.logger.error("Aborting deletion to prevent data loss")
                raise

        self.logger.info(
            f"\n🗑️  {'DRY RUN: ' if dry_run else ''}Deleting {len(analysis.unused_images)} unused images..."
        )

        total_size_deleted = 0
        successful_deletions = 0
        failed_deletions = 0
        deleted_tags = []

        for image_tag in analysis.unused_images:
            # Extract repository type and tag from image tag
            # Format can be:
            # - "type:tag" (e.g., "environment:abc123def456...")
            # - "tag" (legacy format, try both repositories)
            parts = image_tag.split(":", 1)
            if len(parts) == 2:
                # Format: "type:tag"
                repo_type, tag = parts
                repository = f"{self.repository}/{repo_type}"
            elif len(parts) == 1:
                # Legacy format: just tag name, try both environment and model
                tag = parts[0]
                # Try environment first, then model if it fails
                # We'll try both in the deletion loop
                repository = None
            else:
                self.logger.warning(f"Invalid image tag format: {image_tag}")
                failed_deletions += 1
                continue

            stats = analysis.image_usage_stats.get(image_tag, {})
            size = stats.get("size", 0)

            if dry_run:
                if repository:
                    self.logger.info(f"  Would delete: {repository}:{tag} ({sizeof_fmt(size)})")
                else:
                    self.logger.info(f"  Would delete: environment:{tag} or model:{tag} ({sizeof_fmt(size)})")
                successful_deletions += 1
                total_size_deleted += size
            else:
                if repository:
                    self.logger.info(f"  Deleting: {repository}:{tag} ({sizeof_fmt(size)})")
                    # Use standardized Skopeo client for deletion
                    if self.skopeo_client.delete_image(repository, tag):
                        self.logger.info("    ✅ Deleted successfully")
                        successful_deletions += 1
                        total_size_deleted += size
                        deleted_tags.append(f"{repository}:{tag}")
                    else:
                        self.logger.warning("    ❌ Failed to delete")
                        failed_deletions += 1
                else:
                    # Try both environment and model repositories
                    deleted = False
                    for repo_type in ["environment", "model"]:
                        try_repo = f"{self.repository}/{repo_type}"
                        self.logger.info(f"  Trying to delete: {try_repo}:{tag}")
                        if self.skopeo_client.delete_image(try_repo, tag):
                            self.logger.info(f"    ✅ Deleted successfully from {try_repo}")
                            successful_deletions += 1
                            total_size_deleted += size
                            deleted_tags.append(f"{try_repo}:{tag}")
                            deleted = True
                            break
                    if not deleted:
                        self.logger.warning("    ❌ Failed to delete from both environment and model repositories")
                        failed_deletions += 1

        self.logger.info("\n📊 Deletion Summary:")
        self.logger.info(f"   {'Would delete' if dry_run else 'Successfully deleted'}: {successful_deletions} images")
        if not dry_run:
            self.logger.info(f"   Failed deletions: {failed_deletions} images")

        # Show images that couldn't be deleted and why
        used_images_count = len(analysis.used_images)
        if used_images_count > 0:
            self.logger.info(f"\n🔒 Images in use (not deleted): {used_images_count}")
            # Show a few examples
            shown_count = 0
            for image_tag in list(analysis.used_images)[:5]:
                # Try to find stats for this tag - check both the tag name and full_tag formats
                # image_usage_stats is keyed by full_tag (e.g., "environment:tag"), but used_images contains tag names
                stats = None
                # First try direct lookup
                if image_tag in analysis.image_usage_stats:
                    stats = analysis.image_usage_stats[image_tag]
                else:
                    # Try to find by matching tag name (strip type prefix from full_tag keys)
                    for full_tag, tag_stats in analysis.image_usage_stats.items():
                        tag_name = full_tag.split(":", 1)[1] if ":" in full_tag else full_tag
                        if tag_name == image_tag:
                            stats = tag_stats
                            break

                # If still not found, create empty stats
                if stats is None:
                    stats = {}

                usage = stats.get("usage", {})
                # Check if usage dict is actually empty (all lists/counts are empty)
                if usage and (
                    usage.get("runs_count", 0) > 0
                    or usage.get("workspaces_count", 0) > 0
                    or usage.get("models_count", 0) > 0
                    or usage.get("scheduler_jobs")
                    or usage.get("projects")
                ):
                    usage_summary = self._generate_usage_summary(usage)
                else:
                    usage_summary = "Referenced in system (source unknown)"

                # Extract tag name for display
                tag_name = image_tag.split(":", 1)[1] if ":" in image_tag else image_tag
                self.logger.info(f"   • {tag_name}: {usage_summary}")
                shown_count += 1
            if used_images_count > shown_count:
                self.logger.info(f"   ... and {used_images_count - shown_count} more (see report file for details)")

        # Use total_size_saved from analysis for accurate freed space (accounts for shared layers)
        # This is calculated correctly using ImageAnalyzer
        summary_size = analysis.total_size_saved if analysis.total_size_saved > 0 else total_size_deleted
        self.logger.info(f"   {'Would save' if dry_run else 'Saved'}: {sizeof_fmt(summary_size)}")

        # Save results to JSON file
        results_file = self.save_deletion_results(
            analysis=analysis,
            deleted_tags=deleted_tags,
            successful_deletions=successful_deletions,
            failed_deletions=failed_deletions,
            total_size_deleted=total_size_deleted,
            dry_run=dry_run,
        )
        self.logger.info(f"   Results saved to: {results_file}")

        return deleted_tags

    def cleanup_mongo_references(self, deleted_tags: List[str]) -> None:
        """Clean up Mongo references for deleted image tags by calling mongo_cleanup.py

        Automatically determines whether to clean environment_revisions or model_versions
        collections based on the image type in the tag.

        Args:
            deleted_tags: List of Docker image tags that were deleted (format: repository/type:tag)
        """
        if not deleted_tags:
            return

        self.logger.info(f"\n🗄️  Cleaning up Mongo references for {len(deleted_tags)} deleted tags...")

        # Separate tags by image type
        environment_tags = []
        model_tags = []

        for tag in deleted_tags:
            # Extract image type from tag format: repository/type:tag
            # e.g., "dominodatalab/environment:abc123" or "dominodatalab/model:xyz789"
            if "/environment:" in tag:
                environment_tags.append(tag)
            elif "/model:" in tag:
                model_tags.append(tag)
            else:
                self.logger.warning(f"Could not determine image type for tag: {tag}")

        script_path = os.path.join(os.path.dirname(__file__), "mongo_cleanup.py")

        # Clean up environment_revisions collection
        if environment_tags:
            self._cleanup_collection(environment_tags, "environment_revisions", script_path)

        # Clean up model_versions collection
        if model_tags:
            self._cleanup_collection(model_tags, "model_versions", script_path)

        if environment_tags or model_tags:
            self.logger.info("✅ Mongo references cleaned up successfully")

    def _cleanup_collection(self, tags: List[str], collection_name: str, script_path: str) -> None:
        """Helper method to clean up a specific MongoDB collection.

        Args:
            tags: List of tags to clean up
            collection_name: MongoDB collection name
            script_path: Path to mongo_cleanup.py script
        """
        temp_file = os.path.join(config_manager.get_output_dir(), f"deleted_tags_{collection_name}_temp.txt")
        try:
            with open(temp_file, "w") as f:
                for tag in tags:
                    f.write(f"{tag}\n")

            self.logger.info(f"  Cleaning {len(tags)} tags from {collection_name}...")
            cmd = [sys.executable, script_path, "delete", "--file", temp_file, "--collection", collection_name]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                if result.stdout:
                    self.logger.info(f"    {result.stdout}")
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Failed to clean up {collection_name}: {e}")
                if e.stdout:
                    self.logger.error(f"    stdout: {e.stdout}")
                if e.stderr:
                    self.logger.error(f"    stderr: {e.stderr}")
                self.logger.warning(
                    f"    ⚠️  Cleanup of {collection_name} failed - you may need to clean up references manually"
                )

        finally:
            # Clean up temporary file
            try:
                os.remove(temp_file)
            except OSError:
                pass  # Ignore cleanup errors


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Intelligent Docker image deletion with workload analysis")
    parser.add_argument(
        "image",
        nargs="?",
        help="Specific image to delete (format: repository/type:tag, e.g., dominodatalab/environment:abc-123)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually apply changes and delete images (default is dry-run)"
    )
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")
    parser.add_argument(
        "--image-analysis", default=config_manager.get_image_analysis_path(), help="Path to image analysis report"
    )
    parser.add_argument(
        "--output", default=config_manager.get_deletion_analysis_path(), help="Path for deletion analysis report"
    )
    parser.add_argument(
        "--skip-analysis", action="store_true", help="Skip workload analysis and use traditional environments file"
    )
    parser.add_argument(
        "--generate-reports",
        action="store_true",
        help="Force regeneration of image analysis and MongoDB usage reports before running analysis, "
        "even if fresh reports already exist on disk. Use this when the registry has changed since "
        "the last report was generated.",
    )
    parser.add_argument(
        "--input",
        help="File containing ObjectIDs (one per line) to filter images, or pre-generated report file (supports prefixes: environment:, environmentRevision:, model:, modelVersion:, or bare IDs)",
    )
    parser.add_argument(
        "--mongo-cleanup",
        action="store_true",
        help="Also clean up MongoDB records after deleting images (advanced / high-risk; see README)",
    )
    parser.add_argument(
        "--backup", action="store_true", help="Backup images to S3 before deletion (requires --s3-bucket)"
    )
    parser.add_argument(
        "--s3-bucket", help="S3 bucket for backups (optional if configured in config.yaml or S3_BUCKET env var)"
    )
    parser.add_argument("--region", help="AWS region for S3 and ECR (default: from config or us-west-2)")
    parser.add_argument(
        "--enable-docker-deletion",
        action="store_true",
        help="Enable registry deletion by treating registry as in-cluster (overrides auto-detection)",
    )
    parser.add_argument(
        "--registry-statefulset",
        default="docker-registry",
        help="Name of registry StatefulSet/Deployment to modify for deletion (default: docker-registry)",
    )
    parser.add_argument(
        "--run-registry-gc",
        action="store_true",
        help="Run Docker registry garbage collection in the registry pod after deleting images (internal registries only)",
    )
    parser.add_argument(
        "--unused-since-days",
        dest="days",
        type=int,
        metavar="N",
        help='Only consider images as "in-use" if they were used in a workload within the last N days. '
        "If the last usage was more than N days ago, the image will be considered "
        "unused and eligible for deletion. If omitted, any historical usage marks the image as in-use. "
        "This filters based on the last_used, completed, or started timestamp from runs, "
        "and workspace_last_change from workspaces.",
    )
    return parser.parse_args()


def confirm_deletion(count: int = 0, item_type: str = "images"):
    """Ask for user confirmation before deleting images

    Args:
        count: Number of items to be deleted
        item_type: Type of items being deleted

    Returns:
        True if user confirmed, False otherwise
    """
    deleter = IntelligentImageDeleter()
    return deleter.confirm_deletion(count, item_type)


def main():
    """Main function"""
    setup_logging()
    args = parse_arguments()

    # Get S3 configuration from args or config
    s3_bucket = args.s3_bucket or config_manager.get_s3_bucket()
    s3_region = args.region or config_manager.get_s3_region()

    # Validate backup arguments
    if args.backup and not s3_bucket:
        logger = get_logger(__name__)
        logger.error("❌ Error: --s3-bucket is required when --backup is set")
        logger.error("   You can provide it via --s3-bucket flag, S3_BUCKET env var, or config.yaml")
        sys.exit(1)

    # Parse ObjectIDs (typed) from file if provided
    object_ids_map = None
    if args.input:
        object_ids_map = read_typed_object_ids_from_file(args.input)
        env_ids = list(object_ids_map.get("environment", [])) if object_ids_map else []
        env_rev_ids = list(object_ids_map.get("environment_revision", [])) if object_ids_map else []
        model_ids = list(object_ids_map.get("model", [])) if object_ids_map else []
        model_ver_ids = list(object_ids_map.get("model_version", [])) if object_ids_map else []
        if not (env_ids or env_rev_ids or model_ids or model_ver_ids):
            logger = get_logger(__name__)
            logger.error(
                f"Error: No valid ObjectIDs found in file '{args.input}' (prefixes required: environment:, environmentRevision:, model:, modelVersion:)"
            )
            sys.exit(1)
        logger = get_logger(__name__)
        logger.info(
            f"Filtering images by ObjectIDs from file '{args.input}': environment={len(env_ids)}, environmentRevision={len(env_rev_ids)}, model={len(model_ids)}, modelVersion={len(model_ver_ids)}"
        )

    # Get password from env var or config
    password = os.environ.get("REGISTRY_PASSWORD") or config_manager.get_registry_password()

    # Default to dry-run unless --apply is specified
    dry_run = not args.apply

    logger = get_logger(__name__)
    if dry_run:
        logger.info("🔍 DRY RUN MODE (default)")
        logger.info("Images will NOT be deleted. Use --apply to actually delete images.")
    else:
        logger.info("🗑️  DELETE MODE")
        logger.info("Images WILL be deleted!")

        # Require confirmation unless --force is used
        if not args.force:
            if not confirm_deletion():
                logger.info("Deletion cancelled by user.")
                sys.exit(0)
        else:
            logger.warning("⚠️  Force mode enabled - skipping confirmation prompt")

    try:
        # Create deleter
        deleter = IntelligentImageDeleter(
            enable_docker_deletion=args.enable_docker_deletion, registry_statefulset=args.registry_statefulset
        )

        # Handle direct image deletion if image argument is provided
        if args.image:
            logger = get_logger(__name__)
            logger.info(f"🎯 Deleting specific image: {args.image}")

            # Parse image format: repository/type:tag
            if ":" not in args.image:
                logger.error(
                    "❌ Error: Invalid image format. Expected format: repository/type:tag (e.g., dominodatalab/environment:abc-123)"
                )
                sys.exit(1)

            parts = args.image.split(":")
            repository_tag = parts[0]
            tag = parts[1]

            # Extract repository name (remove registry URL if present)
            if "/" in repository_tag:
                repository = repository_tag.split("/", 1)[1]  # Remove registry URL
            else:
                repository = repository_tag

            # Enable deletion in registry (if running in Kubernetes)
            registry_enabled = False
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
                registry_enabled = True

            try:
                # Delete the image
                deleted_tags = []
                logger = get_logger(__name__)
                if dry_run:
                    logger.info(f"  Would delete: {args.image}")
                    deleted_tags = [args.image]
                else:
                    logger.info(f"  Deleting: {args.image}")
                    if deleter.skopeo_client.delete_image(repository, tag):
                        logger.info("    ✅ Deleted successfully")
                        deleted_tags = [args.image]
                    else:
                        logger.warning("    ❌ Failed to delete")

                # Clean up Mongo references for deleted tags
                if deleted_tags and not dry_run and not args.skip_cleanup_mongo:
                    deleter.cleanup_mongo_references(deleted_tags)

            finally:
                # Always disable deletion in registry if it was enabled
                if registry_enabled:
                    deleter.disable_deletion_of_docker_images()

            return

        # Backup-only mode when --backup is provided without --apply
        if (not args.apply) and args.backup:
            # In backup-only mode, target the set of unused images from analysis
            logger = get_logger(__name__)
            logger.info("\n📦 BACKUP-ONLY MODE: Images will be backed up to S3 without deletion.")
            if not args.force:
                resp = input("Proceed with backup only (no deletions)? (yes/no): ").strip().lower()
                if resp not in ["yes", "y"]:
                    logger.info("Operation cancelled by user")
                    sys.exit(0)

            # Load reports for analysis (auto-generate if missing)
            logger.info("📊 Loading image analysis report...")
            image_analysis = deleter.load_image_analysis_report(args.image_analysis)
            if not image_analysis:
                logger.warning("⚠️  Image analysis report not found. Generating it now with image_data_analysis.py ...")
                analysis_script = os.path.join(os.path.dirname(__file__), "image_data_analysis.py")
                try:
                    subprocess.run([sys.executable, analysis_script], check=True)
                    image_analysis = deleter.load_image_analysis_report(args.image_analysis)
                except subprocess.CalledProcessError as e:
                    logger.error(f"❌ Failed to generate image analysis report: {e}")
                    sys.exit(1)

            if not image_analysis:
                logger.error("❌ Missing image analysis report even after regeneration. Aborting.")
                sys.exit(1)

            # Load MongoDB usage reports (auto-generate if missing)
            logger.info("📊 Loading MongoDB usage reports (runs, workspaces, models)...")
            mongodb_reports = deleter.load_mongodb_usage_reports()
            if not any(mongodb_reports.values()):
                logger.warning("⚠️  No MongoDB usage reports found. Generating them now with extract_metadata.py ...")
                extract_script = os.path.join(os.path.dirname(__file__), "..", "scripts", "extract_metadata.py")
                try:
                    subprocess.run([sys.executable, extract_script, "--target", "all"], check=True)
                    mongodb_reports = deleter.load_mongodb_usage_reports()
                except subprocess.CalledProcessError as e:
                    logger.error(f"❌ Failed to generate MongoDB usage reports: {e}")
                    sys.exit(1)

            if not any(mongodb_reports.values()):
                logger.error(
                    "❌ MongoDB usage reports are still missing or empty after regeneration. Aborting to avoid unsafe deletions."
                )
                sys.exit(1)
            else:
                total_records = sum(len(v) for v in mongodb_reports.values())
                logger.info(f"   ✓ Loaded {total_records} MongoDB records")

            # model_version IDs require resolution to slug tags before they can be
            # used as registry filters — raw 24-hex ObjectIds do NOT match Docker tags.
            # Re-use the same two-step resolution as the main analysis path.
            if object_ids_map and object_ids_map.get("model_version"):
                mv_ids_set = set(object_ids_map["model_version"])
                backup_slug_tags: List[str] = []
                backup_seen_mv_ids: set = set()
                for model_record in mongodb_reports.get("models", []):
                    for version in model_record.get("model_active_versions", []):
                        mv_id = normalize_object_id(version.get("model_version_id", ""))
                        if mv_id in mv_ids_set:
                            backup_seen_mv_ids.add(mv_id)
                            slug_tag = version.get("model_environment_tag")
                            if slug_tag:
                                backup_slug_tags.append(slug_tag)
                backup_unresolved = mv_ids_set - backup_seen_mv_ids
                if backup_unresolved:
                    try:
                        from utils.image_usage import ImageUsageService

                        service = ImageUsageService()
                        direct_results = service.collect_model_version_slugs(list(backup_unresolved))
                        for mv_id, slug_tag in direct_results.items():
                            backup_seen_mv_ids.add(mv_id)
                            if slug_tag:
                                backup_slug_tags.append(slug_tag)
                    except Exception as e:
                        logger.warning(f"   ⚠️  Direct MongoDB lookup failed in backup path: {e}")
                if backup_slug_tags:
                    existing_model = list(object_ids_map.get("model", []))
                    object_ids_map["model"] = existing_model + backup_slug_tags

            merged_ids = None
            if object_ids_map:
                merged = set()
                merged.update(object_ids_map.get("environment", []))
                merged.update(object_ids_map.get("environment_revision", []))
                merged.update(object_ids_map.get("model", []))
                # model_version IDs are intentionally excluded: they were resolved above
                if merged:
                    merged_ids = sorted(merged)
                    logger.info(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
                else:
                    logger.error(
                        "❌ ID filter was provided but no IDs could be resolved to Docker image tags. Aborting."
                    )
                    sys.exit(1)
            analysis = deleter.analyze_image_usage(
                image_analysis, merged_ids, object_ids_map, mongodb_reports, recent_days=args.days
            )

            # Prepare tags to backup
            # Unused images are in format: type:tag (e.g., "environment:abc123...") or just "tag" (legacy)
            tags_to_backup = []
            for image_tag in analysis.unused_images:
                # Handle format: "type:tag" or just "tag"
                if ":" in image_tag:
                    tag = image_tag.split(":", 1)[1]
                else:
                    tag = image_tag
                tags_to_backup.append(tag)
            if not tags_to_backup:
                logger.info("No unused images found to back up.")
                sys.exit(0)

            full_repo = f"{deleter.registry_url}/{deleter.repository}"
            cfg_mgr = ConfigManager()
            backup_skopeo_client = SkopeoClient(cfg_mgr)
            try:
                process_backup(
                    skopeo_client=backup_skopeo_client,
                    full_repo=full_repo,
                    tags=tags_to_backup,
                    s3_bucket=s3_bucket,
                    region=s3_region,
                    dry_run=False,
                    delete=False,
                    min_age_days=None,
                    workers=1,
                    tmpdir=None,
                    failed_tags_file=None,
                )
                logger.info(f"✅ Successfully backed up {len(tags_to_backup)} images to S3")
            except Exception as e:
                logger.error(f"❌ Backup failed: {e}")
                sys.exit(1)
            logger.info("\n✅ Backup-only operation completed successfully!")
            return

        if not args.skip_analysis:
            logger = get_logger(__name__)
            # Load analysis reports (auto-generate if missing or if --generate-reports is set)
            logger.info("📊 Loading image analysis report...")
            if args.generate_reports:
                logger.info("📊 --generate-reports set: forcing fresh image analysis report...")
                ensure_image_analysis_reports(max_age_hours=0)
            image_analysis = deleter.load_image_analysis_report(args.image_analysis)

            # Ensure reports are fresh (auto-generate if missing)
            if not image_analysis:
                logger.info("📊 Image analysis report not found. Generating now...")
                ensure_image_analysis_reports()
                image_analysis = deleter.load_image_analysis_report(args.image_analysis)

            if not image_analysis:
                logger.error("❌ Missing image analysis report even after regeneration. Aborting.")
                sys.exit(1)

            # Load MongoDB usage reports (auto-generate if missing or stale, or forced)
            logger.info("📊 Loading MongoDB usage reports (runs, workspaces, models)...")
            ensure_mongodb_reports(max_age_hours=0 if args.generate_reports else 24)
            mongodb_reports = deleter.load_mongodb_usage_reports()

            if not any(mongodb_reports.values()):
                logger.error(
                    "❌ MongoDB usage reports are still missing or empty after regeneration. Aborting to avoid unsafe deletions."
                )
                sys.exit(1)
            else:
                total_records = sum(len(v) for v in mongodb_reports.values())
                logger.info(
                    f"   ✓ Loaded {total_records} MongoDB records (runs: {len(mongodb_reports['runs'])}, workspaces: {len(mongodb_reports['workspaces'])}, models: {len(mongodb_reports['models'])})"
                )

            # Capture the original input types BEFORE resolution mutates object_ids_map.
            # This lets us later detect "input was modelVersion-only" even after resolution
            # has added a synthetic "model" key with the resolved slug tags.
            original_input_types: set = set(object_ids_map.keys()) if object_ids_map else set()
            resolved_slug_tags: List[str] = []  # populated below if model_version IDs present

            # Resolve model_version IDs to their actual Docker tag identifiers.
            # Model version ObjectIDs do not prefix Docker image tags directly.
            # We resolve only to model_environment_tag (the slug image), which is specific
            # to the model version.  We intentionally do NOT resolve to environment_revision_id
            # because the base environment image is shared infrastructure — it may be used by
            # many other model versions, workspaces, and runs not in the input file.  Users who
            # want to target base environment images should use environmentRevision:<id> explicitly.
            if object_ids_map and object_ids_map.get("model_version"):
                mv_ids_set = set(object_ids_map["model_version"])
                seen_mv_ids: set = set()

                # Step 1: try the pre-generated MongoDB report (active/running model versions only)
                for model_record in mongodb_reports.get("models", []):
                    for version in model_record.get("model_active_versions", []):
                        mv_id = normalize_object_id(version.get("model_version_id", ""))
                        if mv_id in mv_ids_set:
                            seen_mv_ids.add(mv_id)
                            slug_tag = version.get("model_environment_tag")
                            if slug_tag:
                                resolved_slug_tags.append(slug_tag)

                # Step 2: for IDs not in the report (stopped/archived versions), query MongoDB directly
                unresolved = mv_ids_set - seen_mv_ids
                if unresolved:
                    logger.info(
                        f"   {len(unresolved)} modelVersion ID(s) not in active-versions report; "
                        "querying MongoDB directly for stopped/archived versions..."
                    )
                    try:
                        from utils.image_usage import ImageUsageService

                        service = ImageUsageService()
                        direct_results = service.collect_model_version_slugs(list(unresolved))
                        for mv_id, slug_tag in direct_results.items():
                            seen_mv_ids.add(mv_id)
                            if slug_tag:
                                resolved_slug_tags.append(slug_tag)
                                logger.debug(f"   Directly resolved modelVersion {mv_id} → {slug_tag}")
                            else:
                                logger.warning(
                                    f"   ⚠️  Model version {mv_id} exists in MongoDB but has no slug tag "
                                    "(the build may not have completed successfully)"
                                )
                        unresolved = mv_ids_set - seen_mv_ids
                    except Exception as e:
                        logger.warning(
                            f"   ⚠️  Direct MongoDB lookup failed: {e}; continuing with partially resolved IDs"
                        )

                if resolved_slug_tags:
                    logger.info(
                        f"   Resolved {len(resolved_slug_tags)} modelVersion ID(s) → slug image tag(s) for model image matching"
                    )
                    existing_model = list(object_ids_map.get("model", []))
                    object_ids_map["model"] = existing_model + resolved_slug_tags

                if unresolved:
                    logger.warning(
                        f"   ⚠️  {len(unresolved)} modelVersion ID(s) not found in MongoDB "
                        f"(they may not exist or never completed a build): "
                        f"{', '.join(sorted(unresolved))}"
                    )

            # Analyze image usage
            logger.info("🔍 Analyzing image usage patterns...")

            # Determine what input types were provided (using ORIGINAL types, captured before
            # resolution added a synthetic "model" key for resolved slug tags).
            has_mv_input = "model_version" in original_input_types
            has_other_input = bool(original_input_types - {"model_version"})

            if has_mv_input and not resolved_slug_tags:
                logger.error(
                    "❌ modelVersion IDs were provided but none could be resolved to Docker image "
                    "tags.  Make sure the model builds completed (metadata.builds.slug must be "
                    "present). Aborting."
                )
                sys.exit(1)

            if has_mv_input and not has_other_input:
                # ── Pure model-version input ──────────────────────────────────────────────
                # Use the dedicated path that works from resolved slug tags only.
                # This completely bypasses image_analysis (final-report.json) so environment
                # images can never appear in model-version deletion candidates.
                logger.info(
                    f"   Input is modelVersion-only: using dedicated model-version analysis "
                    f"({len(resolved_slug_tags)} slug tags, bypassing combined image_analysis report)"
                )
                analysis = deleter.analyze_model_version_usage(resolved_slug_tags, object_ids_map, mongodb_reports)

            elif has_mv_input and has_other_input:
                # ── Mixed input: modelVersion IDs + environment/environmentRevision IDs ───
                # Run both analyses independently so model images are never sourced from
                # the combined image_analysis report (which would risk false positives).
                # The two analyses operate on disjoint candidate sets and are safe to run
                # concurrently.

                # Build the filter/map for the non-model-version part (environments only).
                # Deliberately exclude model slug tags so they don't bleed into the
                # image_analysis-based analysis.
                other_env_ids: Set[str] = set()
                other_env_ids.update(object_ids_map.get("environment", []))
                other_env_ids.update(object_ids_map.get("environment_revision", []))

                if not other_env_ids:
                    logger.error(
                        "❌ Mixed input provided but no environment/environmentRevision IDs could be "
                        "resolved. Aborting."
                    )
                    sys.exit(1)

                other_merged_ids = sorted(other_env_ids)
                # Restrict the type map so analyze_image_usage only sees env-type IDs.
                env_only_map = {k: v for k, v in object_ids_map.items() if k in ("environment", "environment_revision")}

                logger.info(
                    f"   Mixed input: running model-version analysis ({len(resolved_slug_tags)} slugs) "
                    f"and environment analysis ({len(other_merged_ids)} IDs) in parallel"
                )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    mv_future = executor.submit(
                        deleter.analyze_model_version_usage,
                        resolved_slug_tags,
                        object_ids_map,
                        mongodb_reports,
                    )
                    env_future = executor.submit(
                        deleter.analyze_image_usage,
                        image_analysis,
                        other_merged_ids,
                        env_only_map,
                        mongodb_reports,
                        args.days,
                    )
                    mv_analysis = mv_future.result()
                    env_analysis = env_future.result()

                analysis = merge_workload_analyses(mv_analysis, env_analysis)
                logger.info(
                    f"   Merged analyses: {len(analysis.unused_images)} total deletion candidates "
                    f"({len(mv_analysis.unused_images)} model versions + "
                    f"{len(env_analysis.unused_images)} environment images)"
                )

            else:
                # ── No model-version input: environment/environmentRevision/model IDs only ─
                # Use the standard image_analysis-based path with a merged ID filter.
                merged_ids = None
                if object_ids_map:
                    merged = set()
                    merged.update(object_ids_map.get("environment", []))
                    merged.update(object_ids_map.get("environment_revision", []))
                    merged.update(object_ids_map.get("model", []))
                    if merged:
                        merged_ids = sorted(merged)
                        logger.info(f"   Filtering by ObjectIDs: {', '.join(merged_ids)}")
                    else:
                        logger.error(
                            "❌ ID filter was provided but no IDs could be resolved to Docker image tags. " "Aborting."
                        )
                        sys.exit(1)
                analysis = deleter.analyze_image_usage(
                    image_analysis, merged_ids, object_ids_map, mongodb_reports, recent_days=args.days
                )

            # Generate deletion report
            deleter.generate_deletion_report(analysis, args.output)

            # Enable deletion in registry (if running in Kubernetes)
            registry_enabled = False
            if not dry_run:
                deleter.enable_deletion_of_docker_images()
                registry_enabled = True

            try:
                # Delete unused images using SkopeoClient (same as other delete scripts)
                deleted_tags = deleter.delete_unused_images(
                    analysis, password, dry_run=dry_run, backup=args.backup, s3_bucket=s3_bucket, region=s3_region
                )

                # Clean up Mongo references for deleted tags (opt-in via --mongo-cleanup)
                if deleted_tags and not dry_run and args.mongo_cleanup:
                    deleter.cleanup_mongo_references(deleted_tags)

            finally:
                # Always disable deletion in registry if it was enabled
                if registry_enabled:
                    deleter.disable_deletion_of_docker_images()

        else:
            # Use traditional environments file method
            logger = get_logger(__name__)
            logger.info("📋 Using traditional environments file method...")
            # ... existing environments file logic would go here
            logger.warning("Traditional method not yet implemented. Use workload analysis instead.")

        logger = get_logger(__name__)
        if dry_run:
            logger.info("\n✅ DRY RUN COMPLETED")
            logger.info("No images were deleted.")
            logger.info("To actually delete images, run with --apply flag:")
            logger.info("  python delete_image.py --apply")
        else:
            logger.info("\n✅ DELETION COMPLETED")
            logger.info("Images have been deleted from the registry.")

            # Optionally run registry garbage collection for internal registries
            if args.run_registry_gc:
                from utils.registry_maintenance import run_registry_garbage_collection

                logger.info("Running Docker registry garbage collection after image deletion...")
                gc_ok = run_registry_garbage_collection(registry_statefulset=args.registry_statefulset)
                if not gc_ok:
                    logger.warning(
                        "Docker registry garbage collection did not complete successfully; " "see logs for details."
                    )

    except KeyboardInterrupt:
        logger = get_logger(__name__)
        logger.warning("\n⚠️  Deletion interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger = get_logger(__name__)
        logger.error(f"\n❌ Deletion failed: {e}")
        from utils.logging_utils import log_exception

        log_exception(logger, "Error in main", exc_info=e)
        sys.exit(1)


if __name__ == "__main__":
    main()
