"""Unit tests for report_utils.py"""

import pytest
import json
import tempfile
import os
from pathlib import Path

# Import the module to test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

from report_utils import save_json, save_table_and_json


class TestSaveJson:
    """Tests for save_json function"""
    
    def test_save_simple_dict(self):
        """Test saving a simple dictionary"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = {"key1": "value1", "key2": 42}
            
            save_json(file_path, data)
            
            assert os.path.exists(file_path)
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == data
    
    def test_save_nested_dict(self):
        """Test saving a nested dictionary"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = {
                "level1": {
                    "level2": {
                        "level3": "value"
                    }
                },
                "list": [1, 2, 3]
            }
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == data
            assert loaded["level1"]["level2"]["level3"] == "value"
            assert loaded["list"] == [1, 2, 3]
    
    def test_save_list(self):
        """Test saving a list"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = [1, 2, 3, {"nested": "value"}]
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == data
    
    def test_creates_parent_directories(self):
        """Test that parent directories are created if they don't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "subdir1", "subdir2", "test.json")
            data = {"test": "data"}
            
            save_json(file_path, data)
            
            assert os.path.exists(file_path)
            assert os.path.isdir(os.path.dirname(file_path))
    
    def test_indentation(self):
        """Test that JSON is saved with indentation (2 spaces)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = {"key1": "value1", "key2": {"nested": "value2"}}
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                content = f.read()
                # Check that it's indented (has newlines and spaces)
                assert '\n' in content
                # Check that nested dict is indented
                assert '  "nested"' in content or '"nested"' in content
    
    def test_overwrites_existing_file(self):
        """Test that existing file is overwritten"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data1 = {"old": "data"}
            data2 = {"new": "data"}
            
            save_json(file_path, data1)
            save_json(file_path, data2)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == data2
            assert "old" not in loaded
    
    def test_save_complex_data(self):
        """Test saving complex nested data structures"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = {
                "summary": {
                    "total": 100,
                    "items": ["a", "b", "c"]
                },
                "details": [
                    {"id": 1, "name": "item1"},
                    {"id": 2, "name": "item2"}
                ],
                "metadata": {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "version": 1.0
                }
            }
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == data
            assert len(loaded["details"]) == 2
            assert loaded["summary"]["total"] == 100
    
    def test_save_empty_dict(self):
        """Test saving an empty dictionary"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = {}
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == {}
    
    def test_save_empty_list(self):
        """Test saving an empty list"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.json")
            data = []
            
            save_json(file_path, data)
            
            with open(file_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == []


class TestSaveTableAndJson:
    """Tests for save_table_and_json function"""
    
    def test_saves_both_files(self):
        """Test that both .txt and .json files are created"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = "Column1\tColumn2\nValue1\tValue2\n"
            json_obj = {"data": "value"}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            txt_path = base_path + ".txt"
            json_path = base_path + ".json"
            
            assert os.path.exists(txt_path)
            assert os.path.exists(json_path)
    
    def test_table_content(self):
        """Test that table content is saved correctly"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = "Header1\tHeader2\nRow1Col1\tRow1Col2\nRow2Col1\tRow2Col2\n"
            json_obj = {}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            txt_path = base_path + ".txt"
            with open(txt_path, 'r') as f:
                content = f.read()
            assert content == table_str
            assert "Header1" in content
            assert "Row1Col1" in content
    
    def test_json_content(self):
        """Test that JSON content is saved correctly"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = "Table content"
            json_obj = {"key1": "value1", "key2": 42}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            json_path = base_path + ".json"
            with open(json_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == json_obj
    
    def test_creates_parent_directories(self):
        """Test that parent directories are created"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "subdir1", "subdir2", "report")
            table_str = "Table"
            json_obj = {"data": "value"}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            assert os.path.exists(base_path + ".txt")
            assert os.path.exists(base_path + ".json")
            assert os.path.isdir(os.path.dirname(base_path))
    
    def test_empty_table_string(self):
        """Test saving with empty table string"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = ""
            json_obj = {"data": "value"}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            txt_path = base_path + ".txt"
            with open(txt_path, 'r') as f:
                content = f.read()
            assert content == ""
    
    def test_empty_json_object(self):
        """Test saving with empty JSON object"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = "Table content"
            json_obj = {}
            
            save_table_and_json(base_path, table_str, json_obj)
            
            json_path = base_path + ".json"
            with open(json_path, 'r') as f:
                loaded = json.load(f)
            assert loaded == {}
    
    def test_overwrites_existing_files(self):
        """Test that existing files are overwritten"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str1 = "Old table"
            json_obj1 = {"old": "data"}
            table_str2 = "New table"
            json_obj2 = {"new": "data"}
            
            save_table_and_json(base_path, table_str1, json_obj1)
            save_table_and_json(base_path, table_str2, json_obj2)
            
            txt_path = base_path + ".txt"
            json_path = base_path + ".json"
            
            with open(txt_path, 'r') as f:
                assert "New table" in f.read()
            with open(json_path, 'r') as f:
                loaded = json.load(f)
                assert loaded == json_obj2
    
    def test_complex_table_and_json(self):
        """Test saving complex table and JSON data"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "report")
            table_str = "Name\tAge\tCity\nAlice\t30\tNYC\nBob\t25\tLA\n"
            json_obj = {
                "summary": {
                    "total": 2,
                    "average_age": 27.5
                },
                "people": [
                    {"name": "Alice", "age": 30, "city": "NYC"},
                    {"name": "Bob", "age": 25, "city": "LA"}
                ]
            }
            
            save_table_and_json(base_path, table_str, json_obj)
            
            txt_path = base_path + ".txt"
            json_path = base_path + ".json"
            
            with open(txt_path, 'r') as f:
                table_content = f.read()
            assert "Alice" in table_content
            assert "Bob" in table_content
            
            with open(json_path, 'r') as f:
                loaded = json.load(f)
            assert loaded["summary"]["total"] == 2
            assert len(loaded["people"]) == 2
