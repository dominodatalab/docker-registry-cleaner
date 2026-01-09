"""Integration tests for deletion logic"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call
import json
import tempfile
import os

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

from delete_image import IntelligentImageDeleter, WorkloadAnalysis
from image_data_analysis import ImageAnalyzer


class TestDeletionLogic:
    """Integration tests for deletion logic in IntelligentImageDeleter"""
    
    def setup_method(self):
        """Set up test fixtures"""
        with patch('delete_image.SkopeoClient'):
            self.deleter = IntelligentImageDeleter(
                registry_url="http://test-registry",
                repository="test-repo"
            )
    
    def test_analyze_image_usage_identifies_unused_images(self):
        """Test that analyze_image_usage correctly identifies unused images"""
        # Mock workload report - tag1 is used, tag2 is not
        workload_report = {
            "image_tags": {
                "tag1": {"count": 1, "pods": ["pod1"]},
                "tag2": {"count": 0, "pods": []}
            }
        }
        
        # Mock image analysis - both tags exist
        image_analysis = {
            "layer1": {
                "size": 1000,
                "tags": ["tag1", "tag2"],
                "environments": ["env1", "env2"]
            }
        }
        
        analysis = self.deleter.analyze_image_usage(workload_report, image_analysis)
        
        assert "tag1" in analysis.used_images
        assert "tag2" not in analysis.used_images
        assert "tag2" in analysis.unused_images or any("tag2" in img for img in analysis.unused_images)
    
    def test_analyze_image_usage_with_object_id_filtering(self):
        """Test that ObjectID filtering works correctly"""
        workload_report = {
            "image_tags": {
                "507f1f77bcf86cd799439011": {"count": 1},
                "507f1f77bcf86cd799439012": {"count": 0},
                "507f1f77bcf86cd799439999": {"count": 0}  # Different ObjectID
            }
        }
        
        image_analysis = {
            "layer1": {
                "size": 1000,
                "tags": ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012", "507f1f77bcf86cd799439999"],
                "environments": []
            }
        }
        
        object_ids = ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"]
        object_ids_map = {"environment": ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"]}
        
        analysis = self.deleter.analyze_image_usage(
            workload_report, 
            image_analysis, 
            object_ids=object_ids,
            object_ids_map=object_ids_map
        )
        
        # Should only include filtered ObjectIDs
        unused_tags = [tag.split(':')[1] if ':' in tag else tag for tag in analysis.unused_images]
        assert "507f1f77bcf86cd799439999" not in unused_tags
    
    @patch('delete_image.ImageAnalyzer')
    def test_calculate_freed_space_correctly_uses_image_analyzer(self, mock_analyzer_class):
        """Test that freed space calculation uses ImageAnalyzer correctly"""
        # Setup mock analyzer
        mock_analyzer = MagicMock()
        mock_analyzer_class.return_value = mock_analyzer
        
        # Mock the analyze_image method
        mock_analyzer.analyze_image.return_value = True
        
        # Mock freed_space_if_deleted to return test values
        def mock_freed_space(image_ids):
            # Simple mock: return 1000 * number of images
            return len(image_ids) * 1000
        
        mock_analyzer.freed_space_if_deleted.side_effect = mock_freed_space
        
        unused_images = {"environment:tag1", "environment:tag2"}
        
        total_freed, individual_sizes = self.deleter._calculate_freed_space_correctly(
            unused_images,
            object_ids_map={"environment": ["507f1f77bcf86cd799439011"]}
        )
        
        # Verify ImageAnalyzer was created
        mock_analyzer_class.assert_called_once()
        
        # Verify analyze_image was called for both image types
        assert mock_analyzer.analyze_image.call_count == 2  # environment and model
        
        # Verify freed_space_if_deleted was called
        assert mock_analyzer.freed_space_if_deleted.called
        
        # Verify individual sizes were calculated
        assert len(individual_sizes) == 2
        assert "environment:tag1" in individual_sizes
        assert "environment:tag2" in individual_sizes
    
    def test_deletion_report_generation(self):
        """Test that deletion report is generated correctly"""
        analysis = WorkloadAnalysis(
            used_images={"tag1"},
            unused_images={"environment:tag2", "environment:tag3"},
            total_size_saved=5000,
            image_usage_stats={
                "environment:tag2": {"size": 2000, "status": "unused"},
                "environment:tag3": {"size": 3000, "status": "unused"}
            }
        )
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_path = f.name
        
        try:
            self.deleter.generate_deletion_report(analysis, temp_path)
            
            assert os.path.exists(temp_path)
            with open(temp_path, 'r') as f:
                report = json.load(f)
            
            assert report["summary"]["unused_images"] == 2
            assert report["summary"]["total_size_saved"] == 5000
            assert len(report["unused_images"]) == 2
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    
    @patch('delete_image.SkopeoClient')
    def test_delete_unused_images_dry_run(self, mock_skopeo_class):
        """Test dry-run deletion doesn't actually delete"""
        mock_skopeo = MagicMock()
        mock_skopeo_class.return_value = mock_skopeo
        
        deleter = IntelligentImageDeleter(
            registry_url="http://test-registry",
            repository="test-repo"
        )
        deleter.skopeo_client = mock_skopeo
        
        analysis = WorkloadAnalysis(
            used_images=set(),
            unused_images={"environment:tag1", "environment:tag2"},
            total_size_saved=5000,
            image_usage_stats={
                "environment:tag1": {"size": 2000, "status": "unused"},
                "environment:tag2": {"size": 3000, "status": "unused"}
            }
        )
        
        deleted_tags = deleter.delete_unused_images(analysis, password=None, dry_run=True)
        
        # In dry-run, should return empty list (no actual deletions)
        assert deleted_tags == []
        # Skopeo delete_image should not be called
        assert not mock_skopeo.delete_image.called
    
    @patch('delete_image.SkopeoClient')
    def test_delete_unused_images_actual_deletion(self, mock_skopeo_class):
        """Test actual deletion calls skopeo correctly"""
        mock_skopeo = MagicMock()
        mock_skopeo_class.return_value = mock_skopeo
        mock_skopeo.delete_image.return_value = True
        
        deleter = IntelligentImageDeleter(
            registry_url="http://test-registry",
            repository="test-repo"
        )
        deleter.skopeo_client = mock_skopeo
        
        analysis = WorkloadAnalysis(
            used_images=set(),
            unused_images={"environment:tag1"},
            total_size_saved=2000,
            image_usage_stats={
                "environment:tag1": {"size": 2000, "status": "unused"}
            }
        )
        
        deleted_tags = deleter.delete_unused_images(analysis, password=None, dry_run=False)
        
        # Should call delete_image with correct repository and tag
        mock_skopeo.delete_image.assert_called_once_with("test-repo/environment", "tag1")
        assert len(deleted_tags) == 1
        assert "test-repo/environment:tag1" in deleted_tags
    
    @patch('delete_image.SkopeoClient')
    def test_delete_unused_images_handles_failures(self, mock_skopeo_class):
        """Test that deletion failures are handled correctly"""
        mock_skopeo = MagicMock()
        mock_skopeo_class.return_value = mock_skopeo
        mock_skopeo.delete_image.return_value = False  # Simulate failure
        
        deleter = IntelligentImageDeleter(
            registry_url="http://test-registry",
            repository="test-repo"
        )
        deleter.skopeo_client = mock_skopeo
        
        analysis = WorkloadAnalysis(
            used_images=set(),
            unused_images={"environment:tag1"},
            total_size_saved=2000,
            image_usage_stats={
                "environment:tag1": {"size": 2000, "status": "unused"}
            }
        )
        
        deleted_tags = deleter.delete_unused_images(analysis, password=None, dry_run=False)
        
        # Should attempt deletion but return empty list on failure
        mock_skopeo.delete_image.assert_called_once()
        assert deleted_tags == []
    
    def test_save_deletion_results(self):
        """Test that deletion results are saved to JSON"""
        analysis = WorkloadAnalysis(
            used_images={"tag1"},
            unused_images={"environment:tag2"},
            total_size_saved=2000,
            image_usage_stats={
                "environment:tag2": {"size": 2000, "status": "unused"}
            }
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "results.json")
            
            results_file = self.deleter.save_deletion_results(
                analysis=analysis,
                deleted_tags=["test-repo/environment:tag2"],
                successful_deletions=1,
                failed_deletions=0,
                total_size_deleted=2000,
                dry_run=False,
                output_file=output_file
            )
            
            assert os.path.exists(results_file)
            with open(results_file, 'r') as f:
                results = json.load(f)
            
            assert results["dry_run"] == False
            assert results["summary"]["successful_deletions"] == 1
            assert results["summary"]["total_size_saved_bytes"] == 2000
            assert len(results["deleted_images"]) == 1
            assert results["deleted_images"][0]["tag"] == "environment:tag2"


class TestDeletionFlowIntegration:
    """Integration tests for the complete deletion flow"""
    
    @patch('delete_image.ImageAnalyzer')
    @patch('delete_image.SkopeoClient')
    def test_complete_deletion_flow(self, mock_skopeo_class, mock_analyzer_class):
        """Test the complete flow from analysis to deletion"""
        # Setup mocks
        mock_skopeo = MagicMock()
        mock_skopeo_class.return_value = mock_skopeo
        mock_skopeo.delete_image.return_value = True
        
        mock_analyzer = MagicMock()
        mock_analyzer_class.return_value = mock_analyzer
        mock_analyzer.analyze_image.return_value = True
        mock_analyzer.freed_space_if_deleted.side_effect = lambda ids: len(ids) * 1000
        
        deleter = IntelligentImageDeleter(
            registry_url="http://test-registry",
            repository="test-repo"
        )
        deleter.skopeo_client = mock_skopeo
        
        # Create workload and image analysis
        workload_report = {
            "image_tags": {
                "tag1": {"count": 1},
                "tag2": {"count": 0}
            }
        }
        
        image_analysis = {
            "layer1": {
                "size": 1000,
                "tags": ["tag1", "tag2"],
                "environments": []
            }
        }
        
        # Analyze
        analysis = deleter.analyze_image_usage(workload_report, image_analysis)
        
        # Verify unused images identified
        assert "tag2" in [tag.split(':')[1] if ':' in tag else tag for tag in analysis.unused_images]
        
        # Delete (dry-run)
        deleted_tags = deleter.delete_unused_images(analysis, password=None, dry_run=True)
        
        # Verify no actual deletion in dry-run
        assert not mock_skopeo.delete_image.called
    
    def test_object_id_type_mapping(self):
        """Test that ObjectID type mapping works correctly"""
        workload_report = {"image_tags": {}}
        image_analysis = {
            "layer1": {
                "size": 1000,
                "tags": ["507f1f77bcf86cd799439011"],
                "environments": []
            }
        }
        
        object_ids = ["507f1f77bcf86cd799439011"]
        object_ids_map = {
            "environment": ["507f1f77bcf86cd799439011"]
        }
        
        analysis = self.deleter.analyze_image_usage(
            workload_report,
            image_analysis,
            object_ids=object_ids,
            object_ids_map=object_ids_map
        )
        
        # Should have type prefix in unused_images
        unused_with_prefix = [tag for tag in analysis.unused_images if tag.startswith("environment:")]
        assert len(unused_with_prefix) > 0
