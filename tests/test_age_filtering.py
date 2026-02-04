"""
Tests for age filtering functionality in image usage detection.

Tests verify that images are correctly filtered based on last usage timestamps.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add python directory to path (also handled by conftest.py for pytest)
_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

from utils.image_usage import ImageUsageService


class TestAgeFiltering:
    """Test age filtering in ImageUsageService"""

    def test_parse_timestamp_valid_iso(self):
        """Test parsing valid ISO timestamp strings"""
        service = ImageUsageService()

        # Test with Z suffix
        ts1 = service._parse_timestamp("2024-01-15T10:30:00Z")
        assert ts1 is not None
        assert ts1.year == 2024
        assert ts1.month == 1
        assert ts1.day == 15

        # Test with timezone offset
        ts2 = service._parse_timestamp("2024-01-15T10:30:00+00:00")
        assert ts2 is not None
        assert ts2.year == 2024

    def test_parse_timestamp_invalid(self):
        """Test parsing invalid timestamp strings"""
        service = ImageUsageService()

        assert service._parse_timestamp("") is None
        assert service._parse_timestamp("invalid") is None
        assert service._parse_timestamp(None) is None

    def test_get_most_recent_usage_date_from_runs(self):
        """Test getting most recent usage date from runs"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        usage_info = {
            "runs": [
                {"started": (now - timedelta(days=10)).isoformat() + "Z"},
                {"completed": (now - timedelta(days=5)).isoformat() + "Z"},
                {"last_used": (now - timedelta(days=2)).isoformat() + "Z"},  # Most recent
            ],
            "workspaces": [],
            "models": [],
        }

        most_recent = service._get_most_recent_usage_date(usage_info)
        assert most_recent is not None
        # Should be within 1 day of 2 days ago
        assert abs((now - most_recent).days - 2) <= 1

    def test_get_most_recent_usage_date_from_workspaces(self):
        """Test getting most recent usage date from workspaces"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        usage_info = {
            "runs": [],
            "workspaces": [
                {"workspace_last_change": (now - timedelta(days=7)).isoformat() + "Z"},
                {"workspace_last_change": (now - timedelta(days=3)).isoformat() + "Z"},  # Most recent
            ],
            "models": [],
        }

        most_recent = service._get_most_recent_usage_date(usage_info)
        assert most_recent is not None
        # Should be within 1 day of 3 days ago
        assert abs((now - most_recent).days - 3) <= 1

    def test_get_most_recent_usage_date_prefers_last_used(self):
        """Test that last_used is preferred over completed/started"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        usage_info = {
            "runs": [
                {
                    "started": (now - timedelta(days=10)).isoformat() + "Z",
                    "completed": (now - timedelta(days=8)).isoformat() + "Z",
                    "last_used": (now - timedelta(days=1)).isoformat() + "Z",  # Should use this
                }
            ],
            "workspaces": [],
            "models": [],
        }

        most_recent = service._get_most_recent_usage_date(usage_info)
        assert most_recent is not None
        # Should be within 1 day of 1 day ago (last_used)
        assert abs((now - most_recent).days - 1) <= 1

    def test_check_tags_in_use_with_age_filtering(self):
        """Test that check_tags_in_use filters by age correctly"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        recent_date = (now - timedelta(days=5)).isoformat() + "Z"
        old_date = (now - timedelta(days=35)).isoformat() + "Z"

        # Mock mongodb_reports
        mongodb_reports = {
            "runs": [
                {"environment_docker_tag": "recent-tag", "last_used": recent_date},
                {"environment_docker_tag": "old-tag", "last_used": old_date},
            ],
            "workspaces": [],
            "models": [],
            "scheduler_jobs": [],
            "projects": [],
            "organizations": [],
            "app_versions": [],
        }

        tags = ["recent-tag", "old-tag"]
        in_use_tags, usage_info = service.check_tags_in_use(tags, mongodb_reports, recent_days=30)

        # recent-tag should be in use (within 30 days)
        assert "recent-tag" in in_use_tags
        # old-tag should NOT be in use (older than 30 days)
        assert "old-tag" not in in_use_tags

    def test_check_tags_in_use_keeps_config_usage(self):
        """Test that tags with config usage (projects, scheduler_jobs) are kept even if old"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        old_date = (now - timedelta(days=100)).isoformat() + "Z"

        mongodb_reports = {
            "runs": [{"environment_docker_tag": "old-but-in-project", "last_used": old_date}],
            "workspaces": [],
            "models": [],
            "scheduler_jobs": [],
            "projects": [{"environment_docker_tag": "old-but-in-project"}],
            "organizations": [],
            "app_versions": [],
        }

        tags = ["old-but-in-project"]
        in_use_tags, usage_info = service.check_tags_in_use(tags, mongodb_reports, recent_days=30)

        # Should be kept because it's in projects (current config)
        assert "old-but-in-project" in in_use_tags

    def test_check_tags_in_use_no_age_filtering(self):
        """Test that without recent_days, all usage is considered"""
        service = ImageUsageService()

        now = datetime.now(timezone.utc)
        old_date = (now - timedelta(days=100)).isoformat() + "Z"

        mongodb_reports = {
            "runs": [{"environment_docker_tag": "old-tag", "last_used": old_date}],
            "workspaces": [],
            "models": [],
            "scheduler_jobs": [],
            "projects": [],
            "organizations": [],
            "app_versions": [],
        }

        tags = ["old-tag"]
        in_use_tags, usage_info = service.check_tags_in_use(tags, mongodb_reports, recent_days=None)

        # Should be in use even though it's old (no filtering)
        assert "old-tag" in in_use_tags
