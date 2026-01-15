"""
Tests for usage detection logic in delete_image.py

Tests verify that images are correctly identified as used or unused based on MongoDB data.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add python directory to path (also handled by conftest.py for pytest)
_python_dir = Path(__file__).parent.parent / 'python'
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

from scripts.delete_image import IntelligentImageDeleter


class TestUsageDetection:
    """Test usage detection in IntelligentImageDeleter"""
    
    def test_analyze_image_usage_basic(self):
        """Test basic usage detection without age filtering"""
        deleter = IntelligentImageDeleter()
        
        image_analysis = {
            'layer1': {
                'tags': ['tag1', 'tag2'],
                'size': 1000000
            }
        }
        
        mongodb_reports = {
            'runs': [
                {'environment_docker_tag': 'tag1'}
            ],
            'workspaces': [],
            'models': [],
            'scheduler_jobs': [],
            'projects': [],
            'organizations': [],
            'app_versions': []
        }
        
        analysis = deleter.analyze_image_usage(
            image_analysis,
            mongodb_reports=mongodb_reports,
            recent_days=None
        )
        
        # tag1 should be used, tag2 should be unused
        assert 'tag1' in analysis.used_images
        assert 'tag2' not in analysis.used_images
        assert len(analysis.unused_images) > 0
    
    def test_analyze_image_usage_with_age_filtering(self):
        """Test usage detection with age filtering"""
        deleter = IntelligentImageDeleter()
        
        now = datetime.now(timezone.utc)
        recent_date = (now - timedelta(days=5)).isoformat() + 'Z'
        old_date = (now - timedelta(days=35)).isoformat() + 'Z'
        
        image_analysis = {
            'layer1': {
                'tags': ['recent-tag', 'old-tag'],
                'size': 1000000
            }
        }
        
        mongodb_reports = {
            'runs': [
                {
                    'environment_docker_tag': 'recent-tag',
                    'last_used': recent_date
                },
                {
                    'environment_docker_tag': 'old-tag',
                    'last_used': old_date
                }
            ],
            'workspaces': [],
            'models': [],
            'scheduler_jobs': [],
            'projects': [],
            'organizations': [],
            'app_versions': []
        }
        
        analysis = deleter.analyze_image_usage(
            image_analysis,
            mongodb_reports=mongodb_reports,
            recent_days=30
        )
        
        # recent-tag should be used (within 30 days)
        assert 'recent-tag' in analysis.used_images
        # old-tag should be unused (older than 30 days)
        assert 'old-tag' not in analysis.used_images
        # old-tag should be in unused_images
        assert any('old-tag' in tag for tag in analysis.unused_images)
    
    def test_analyze_image_usage_keeps_config_usage(self):
        """Test that config usage (projects, scheduler_jobs) is always kept"""
        deleter = IntelligentImageDeleter()
        
        now = datetime.now(timezone.utc)
        old_date = (now - timedelta(days=100)).isoformat() + 'Z'
        
        image_analysis = {
            'layer1': {
                'tags': ['project-tag'],
                'size': 1000000
            }
        }
        
        mongodb_reports = {
            'runs': [
                {
                    'environment_docker_tag': 'project-tag',
                    'last_used': old_date  # Very old
                }
            ],
            'workspaces': [],
            'models': [],
            'scheduler_jobs': [],
            'projects': [
                {
                    'environment_docker_tag': 'project-tag'  # But in project config
                }
            ],
            'organizations': [],
            'app_versions': []
        }
        
        analysis = deleter.analyze_image_usage(
            image_analysis,
            mongodb_reports=mongodb_reports,
            recent_days=30  # Should filter out old runs
        )
        
        # Should still be used because it's in projects (current config)
        assert 'project-tag' in analysis.used_images
    
    def test_parse_timestamp_edge_cases(self):
        """Test timestamp parsing edge cases"""
        deleter = IntelligentImageDeleter()
        
        # Test various timestamp formats
        assert deleter._parse_timestamp("2024-01-15T10:30:00Z") is not None
        assert deleter._parse_timestamp("2024-01-15T10:30:00+00:00") is not None
        assert deleter._parse_timestamp("") is None
        assert deleter._parse_timestamp(None) is None
        assert deleter._parse_timestamp("invalid") is None
    
    def test_get_most_recent_usage_date_empty(self):
        """Test getting most recent date from empty usage info"""
        deleter = IntelligentImageDeleter()
        
        usage_info = {
            'runs': [],
            'workspaces': [],
            'models': []
        }
        
        most_recent = deleter._get_most_recent_usage_date(usage_info)
        assert most_recent is None
    
    def test_get_most_recent_usage_date_multiple_sources(self):
        """Test getting most recent date across multiple usage sources"""
        deleter = IntelligentImageDeleter()
        
        now = datetime.now(timezone.utc)
        usage_info = {
            'runs': [
                {'last_used': (now - timedelta(days=10)).isoformat() + 'Z'}
            ],
            'workspaces': [
                {'workspace_last_change': (now - timedelta(days=5)).isoformat() + 'Z'}  # Most recent
            ],
            'models': []
        }
        
        most_recent = deleter._get_most_recent_usage_date(usage_info)
        assert most_recent is not None
        # Should be the workspace date (5 days ago)
        assert abs((now - most_recent).days - 5) <= 1
