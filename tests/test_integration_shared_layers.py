"""Integration tests for shared layer calculation logic"""

"""Integration tests for shared layer calculation logic"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from collections import Counter

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

from image_data_analysis import ImageAnalyzer


class TestSharedLayerCalculation:
    """Integration tests for shared layer calculation in ImageAnalyzer"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.analyzer = ImageAnalyzer("http://test-registry", "test-repo")
        # Clear any existing data
        self.analyzer.layers = {}
        self.analyzer.images = {}
        self.analyzer.image_layers = []
    
    def _add_image_with_layers(self, image_id: str, layers: list):
        """Helper to add an image with specified layers to the analyzer
        
        Args:
            image_id: Image ID (e.g., "environment:tag1")
            layers: List of (layer_id, size_bytes) tuples
        """
        self.analyzer.images[image_id] = {
            'repository': f"test-repo/{image_id.split(':')[0]}",
            'tag': image_id.split(':')[1],
            'digest': f"sha256:{image_id}"
        }
        
        for order_index, (layer_id, size_bytes) in enumerate(layers):
            # Add or update layer
            if layer_id in self.analyzer.layers:
                self.analyzer.layers[layer_id]['ref_count'] += 1
            else:
                self.analyzer.layers[layer_id] = {
                    'size_bytes': size_bytes,
                    'ref_count': 1
                }
            
            # Add image-to-layer mapping
            self.analyzer.image_layers.append({
                'image_id': image_id,
                'layer_id': layer_id,
                'order_index': order_index
            })
    
    def test_single_image_no_shared_layers(self):
        """Test freed space calculation for a single image with no shared layers"""
        # Image with 3 unique layers
        self._add_image_with_layers("environment:tag1", [
            ("layer1", 1000),
            ("layer2", 2000),
            ("layer3", 3000)
        ])
        
        freed = self.analyzer.freed_space_if_deleted(["environment:tag1"])
        
        assert freed == 6000  # All 3 layers should be freed
    
    def test_multiple_images_no_shared_layers(self):
        """Test freed space when deleting multiple images with no shared layers"""
        self._add_image_with_layers("environment:tag1", [
            ("layer1", 1000),
            ("layer2", 2000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("layer3", 3000),
            ("layer4", 4000)
        ])
        
        freed = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2"])
        
        assert freed == 10000  # All layers from both images
    
    def test_shared_base_layer(self):
        """Test freed space calculation with shared base layer"""
        # Both images share a base layer
        self._add_image_with_layers("environment:tag1", [
            ("base_layer", 5000),  # Shared
            ("unique_layer1", 1000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("base_layer", 5000),  # Shared (same layer_id)
            ("unique_layer2", 2000)
        ])
        
        # Delete tag1 - should free unique_layer1, but not base_layer (still used by tag2)
        # base_layer has ref_count=2 (used by both), deleting tag1 means 1 deletion
        # Since 2 != 1, base_layer is NOT freed
        freed_tag1 = self.analyzer.freed_space_if_deleted(["environment:tag1"])
        assert freed_tag1 == 1000  # Only unique_layer1
        
        # Delete tag2 - should free unique_layer2, but not base_layer (still used by tag1)
        # base_layer still has ref_count=2 (used by both), deleting tag2 means 1 deletion
        # Since 2 != 1, base_layer is NOT freed
        freed_tag2 = self.analyzer.freed_space_if_deleted(["environment:tag2"])
        assert freed_tag2 == 2000  # Only unique_layer2
        
        # Delete both - should free all layers
        # base_layer has ref_count=2, deleting both means 2 deletions
        # Since 2 == 2, base_layer IS freed
        freed_both = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2"])
        assert freed_both == 8000  # base_layer (5000) + unique_layer1 (1000) + unique_layer2 (2000)
    
    def test_shared_layer_multiple_images(self):
        """Test shared layer used by 3 images, deleting 2"""
        # Create 3 images sharing a base layer
        self._add_image_with_layers("environment:tag1", [
            ("base_layer", 5000),
            ("unique1", 1000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("base_layer", 5000),
            ("unique2", 2000)
        ])
        self._add_image_with_layers("environment:tag3", [
            ("base_layer", 5000),
            ("unique3", 3000)
        ])
        
        # Delete tag1 and tag2 - base_layer still used by tag3, so shouldn't be freed
        freed = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2"])
        assert freed == 3000  # Only unique1 (1000) + unique2 (2000)
        
        # Delete all three - now base_layer should be freed
        freed_all = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2", "environment:tag3"])
        assert freed_all == 16000  # base_layer (5000) + unique1 (1000) + unique2 (2000) + unique3 (3000)
    
    def test_partially_shared_layers(self):
        """Test images with some shared and some unique layers"""
        # Image 1: layers A, B, C
        # Image 2: layers A, B, D (shares A and B with Image 1)
        # Image 3: layers A, E (shares A with both)
        self._add_image_with_layers("environment:tag1", [
            ("layerA", 1000),
            ("layerB", 2000),
            ("layerC", 3000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("layerA", 1000),
            ("layerB", 2000),
            ("layerD", 4000)
        ])
        self._add_image_with_layers("environment:tag3", [
            ("layerA", 1000),
            ("layerE", 5000)
        ])
        
        # Delete tag1 - should free layerC, but not A or B (used by tag2)
        freed_tag1 = self.analyzer.freed_space_if_deleted(["environment:tag1"])
        assert freed_tag1 == 3000  # Only layerC
        
        # Delete tag1 and tag2 - should free layerC and layerD, but not A (used by tag3) or B (only used by tag1 and tag2)
        freed_tag1_tag2 = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2"])
        assert freed_tag1_tag2 == 9000  # layerC (3000) + layerD (4000) + layerB (2000)
        
        # Delete all - should free everything
        freed_all = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2", "environment:tag3"])
        assert freed_all == 16000  # All layers: A(1000) + B(2000) + C(3000) + D(4000) + E(5000)
    
    def test_individual_vs_total_calculation(self):
        """Test that individual tag sizes sum correctly vs total calculation"""
        # Two images sharing a base layer
        self._add_image_with_layers("environment:tag1", [
            ("base_layer", 5000),
            ("unique1", 1000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("base_layer", 5000),
            ("unique2", 2000)
        ])
        
        # Individual sizes (what would be freed if only that image was deleted)
        size_tag1 = self.analyzer.freed_space_if_deleted(["environment:tag1"])
        size_tag2 = self.analyzer.freed_space_if_deleted(["environment:tag2"])
        
        # Total size (what would be freed if both were deleted)
        size_both = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag2"])
        
        # Individual sizes should be less than total (due to shared layer)
        assert size_tag1 == 1000  # Only unique1
        assert size_tag2 == 2000  # Only unique2
        assert size_both == 8000  # base_layer + unique1 + unique2
        
        # Total should equal sum of unique parts + shared layer
        # size_both = base_layer (5000) + unique1 (1000) + unique2 (2000) = 8000
        assert size_both == 5000 + 1000 + 2000
        # But individual sizes don't sum to total (they're 1000 + 2000 = 3000, not 8000)
        assert size_tag1 + size_tag2 < size_both
    
    def test_empty_deletion_list(self):
        """Test that deleting no images frees no space"""
        self._add_image_with_layers("environment:tag1", [
            ("layer1", 1000),
            ("layer2", 2000)
        ])
        
        freed = self.analyzer.freed_space_if_deleted([])
        assert freed == 0
    
    def test_nonexistent_image(self):
        """Test that deleting non-existent image frees no space"""
        self._add_image_with_layers("environment:tag1", [
            ("layer1", 1000)
        ])
        
        freed = self.analyzer.freed_space_if_deleted(["environment:nonexistent"])
        assert freed == 0
    
    def test_complex_scenario(self):
        """Test a complex scenario with multiple images and various sharing patterns"""
        # Image 1: A, B, C
        # Image 2: A, B, D
        # Image 3: A, C, E
        # Image 4: F, G (completely separate)
        self._add_image_with_layers("environment:tag1", [
            ("layerA", 1000),
            ("layerB", 2000),
            ("layerC", 3000)
        ])
        self._add_image_with_layers("environment:tag2", [
            ("layerA", 1000),
            ("layerB", 2000),
            ("layerD", 4000)
        ])
        self._add_image_with_layers("environment:tag3", [
            ("layerA", 1000),
            ("layerC", 3000),
            ("layerE", 5000)
        ])
        self._add_image_with_layers("environment:tag4", [
            ("layerF", 6000),
            ("layerG", 7000)
        ])
        
        # Delete tag1 and tag4
        # tag1: A(used by tag2,3), B(used by tag2), C(used by tag3) - none freed
        # tag4: F, G - both freed
        freed = self.analyzer.freed_space_if_deleted(["environment:tag1", "environment:tag4"])
        assert freed == 13000  # layerF (6000) + layerG (7000)
        
        # Delete tag1, tag2, tag3 (all that share layers)
        # A: used by all 3, deleting all 3 -> freed
        # B: used by tag1,2, deleting both -> freed
        # C: used by tag1,3, deleting both -> freed
        # D: used by tag2 only -> freed
        # E: used by tag3 only -> freed
        freed_all_shared = self.analyzer.freed_space_if_deleted([
            "environment:tag1", "environment:tag2", "environment:tag3"
        ])
        assert freed_all_shared == 16000  # A(1000) + B(2000) + C(3000) + D(4000) + E(5000)
        
        # Delete all 4
        freed_all = self.analyzer.freed_space_if_deleted([
            "environment:tag1", "environment:tag2", "environment:tag3", "environment:tag4"
        ])
        assert freed_all == 29000  # All layers


class TestSharedLayerCalculationWithRealisticData:
    """Integration tests with more realistic data structures"""
    
    def test_ref_count_tracking(self):
        """Test that reference counts are correctly tracked"""
        analyzer = ImageAnalyzer("http://test-registry", "test-repo")
        analyzer.layers = {}
        analyzer.images = {}
        analyzer.image_layers = []
        
        # Add same layer to multiple images
        layer_id = "sha256:base123"
        layer_size = 1000000
        
        # Image 1 uses the layer
        analyzer.images["environment:tag1"] = {'repository': 'test-repo/environment', 'tag': 'tag1', 'digest': 'sha256:img1'}
        analyzer.layers[layer_id] = {'size_bytes': layer_size, 'ref_count': 1}
        analyzer.image_layers.append({'image_id': 'environment:tag1', 'layer_id': layer_id, 'order_index': 0})
        
        # Image 2 uses the same layer
        analyzer.images["environment:tag2"] = {'repository': 'test-repo/environment', 'tag': 'tag2', 'digest': 'sha256:img2'}
        analyzer.layers[layer_id]['ref_count'] += 1
        analyzer.image_layers.append({'image_id': 'environment:tag2', 'layer_id': layer_id, 'order_index': 0})
        
        # Verify ref_count
        assert analyzer.layers[layer_id]['ref_count'] == 2
        
        # Delete tag1 - layer should still have ref_count 1 (used by tag2)
        freed = analyzer.freed_space_if_deleted(["environment:tag1"])
        assert freed == 0  # Layer still referenced by tag2
        
        # Delete tag2 - now layer should be freed
        freed = analyzer.freed_space_if_deleted(["environment:tag2"])
        assert freed == layer_size  # Layer no longer referenced
    
    def test_layer_order_preservation(self):
        """Test that layer order is preserved in image_layers"""
        analyzer = ImageAnalyzer("http://test-registry", "test-repo")
        analyzer.layers = {}
        analyzer.images = {}
        analyzer.image_layers = []
        
        # Add image with layers in specific order
        image_id = "environment:tag1"
        analyzer.images[image_id] = {'repository': 'test-repo/environment', 'tag': 'tag1', 'digest': 'sha256:img1'}
        
        layers = [("layer1", 1000), ("layer2", 2000), ("layer3", 3000)]
        for order_index, (layer_id, size) in enumerate(layers):
            analyzer.layers[layer_id] = {'size_bytes': size, 'ref_count': 1}
            analyzer.image_layers.append({
                'image_id': image_id,
                'layer_id': layer_id,
                'order_index': order_index
            })
        
        # Verify order
        image_layers = [il for il in analyzer.image_layers if il['image_id'] == image_id]
        assert len(image_layers) == 3
        assert image_layers[0]['layer_id'] == "layer1"
        assert image_layers[1]['layer_id'] == "layer2"
        assert image_layers[2]['layer_id'] == "layer3"
        
        # Verify all layers are freed when image is deleted
        freed = analyzer.freed_space_if_deleted([image_id])
        assert freed == 6000
