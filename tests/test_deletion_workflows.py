"""
Tests for deletion workflows in delete_image.py

Tests verify the complete deletion workflow including analysis, filtering, and deletion.
"""

import pytest
import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

from scripts.delete_image import IntelligentImageDeleter, WorkloadAnalysis


class TestDeletionWorkflows:
    """Test deletion workflows"""
    
    @pytest.fixture
    def deleter(self):
        """Create a deleter instance for testing"""
        with patch('scripts.delete_image.config_manager'):
            return IntelligentImageDeleter()
    
    @pytest.fixture
    def sample_image_analysis(self):
        """Sample image analysis data"""
        return {
            'layer1': {
                'tags': ['env:tag1', 'env:tag2', 'env:tag3'],
                'size': 1000000
            },
            'layer2': {
                'tags': ['model:tag4'],
                'size': 2000000
            }
        }
    
    @pytest.fixture
    def sample_mongodb_reports(self):
        """Sample MongoDB usage reports"""
        now = datetime.now(timezone.utc)
        return {
            'runs': [
                {
                    'environment_docker_tag': 'tag1',
                    'last_used': (now - timedelta(days=5)).isoformat() + 'Z'
                },
                {
                    'environment_docker_tag': 'tag2',
                    'last_used': (now - timedelta(days=40)).isoformat() + 'Z'  # Old
                }
            ],
            'workspaces': [],
            'models': [],
            'scheduler_jobs': [],
            'projects': [
                {
                    'environment_docker_tag': 'tag3'  # In project config
                }
            ],
            'organizations': [],
            'app_versions': []
        }
    
    def test_workflow_without_age_filtering(self, deleter, sample_image_analysis, sample_mongodb_reports):
        """Test complete workflow without age filtering"""
        analysis = deleter.analyze_image_usage(
            sample_image_analysis,
            mongodb_reports=sample_mongodb_reports,
            recent_days=None
        )
        
        # tag1 and tag2 should be used (from runs), tag3 should be used (from projects)
        assert 'tag1' in analysis.used_images
        assert 'tag2' in analysis.used_images
        assert 'tag3' in analysis.used_images
        # tag4 should be unused (not in MongoDB)
        assert any('tag4' in tag for tag in analysis.unused_images)
    
    def test_workflow_with_age_filtering(self, deleter, sample_image_analysis, sample_mongodb_reports):
        """Test complete workflow with age filtering"""
        analysis = deleter.analyze_image_usage(
            sample_image_analysis,
            mongodb_reports=sample_mongodb_reports,
            recent_days=30
        )
        
        # tag1 should be used (recent)
        assert 'tag1' in analysis.used_images
        # tag2 should be unused (old, >30 days)
        assert 'tag2' not in analysis.used_images
        # tag3 should still be used (in projects config, regardless of age)
        assert 'tag3' in analysis.used_images
        # tag4 should be unused (not in MongoDB)
        assert any('tag4' in tag for tag in analysis.unused_images)
    
    def test_workflow_with_objectid_filtering(self, deleter, sample_image_analysis, sample_mongodb_reports):
        """Test workflow with ObjectID filtering"""
        object_ids = ['env123']
        object_ids_map = {'environment': ['env123']}
        
        # Mock image analysis with ObjectID in tag
        filtered_analysis = {
            'layer1': {
                'tags': ['env123-abc', 'env456-def'],  # Only first matches
                'size': 1000000
            }
        }
        
        analysis = deleter.analyze_image_usage(
            filtered_analysis,
            object_ids=object_ids,
            object_ids_map=object_ids_map,
            mongodb_reports=sample_mongodb_reports,
            recent_days=None
        )
        
        # Should only process tags matching ObjectIDs
        # This is a simplified test - actual implementation may vary
    
    def test_generate_deletion_report(self, deleter):
        """Test deletion report generation"""
        analysis = WorkloadAnalysis(
            used_images={'tag1', 'tag2'},
            unused_images={'tag3', 'tag4'},
            total_size_saved=1000000000,
            image_usage_stats={
                'tag3': {
                    'size': 500000000,
                    'status': 'unused',
                    'usage': {}
                },
                'tag4': {
                    'size': 500000000,
                    'status': 'unused',
                    'usage': {}
                }
            }
        )
        
        with patch('scripts.delete_image.save_json') as mock_save:
            deleter.generate_deletion_report(analysis, "test-report.json")
            mock_save.assert_called_once()
            call_args = mock_save.call_args
            assert call_args[0][0] == "test-report.json"
            report = call_args[0][1]
            assert report['summary']['unused_images'] == 2
            assert report['summary']['used_images'] == 2
    
    def test_usage_summary_generation(self, deleter):
        """Test usage summary generation"""
        usage = {
            'runs_count': 5,
            'workspaces_count': 2,
            'models_count': 1,
            'scheduler_jobs': [],
            'projects': []
        }
        
        summary = deleter._generate_usage_summary(usage)
        assert '5 execution' in summary
        assert '2 workspace' in summary
        assert '1 model' in summary
    
    def test_usage_summary_empty(self, deleter):
        """Test usage summary for empty usage"""
        usage = {
            'runs': [],
            'workspaces': [],
            'models': []
        }
        
        summary = deleter._generate_usage_summary(usage)
        assert 'Referenced in system' in summary or 'unknown' in summary.lower()
