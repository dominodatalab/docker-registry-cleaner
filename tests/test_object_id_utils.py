"""Unit tests for object_id_utils.py"""

import os

# Import the module to test
import sys
import tempfile
from pathlib import Path

import pytest
from bson import ObjectId

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

from utils.object_id_utils import (
    filter_values_by_object_ids,
    read_object_ids_from_file,
    read_typed_object_ids_from_file,
    starts_with_any_object_id,
    validate_object_id,
)


class TestValidateObjectId:
    """Tests for validate_object_id function"""

    def test_valid_object_id(self):
        """Test validation of a valid 24-character hex ObjectID"""
        valid_id = "507f1f77bcf86cd799439011"
        result = validate_object_id(valid_id)
        assert isinstance(result, ObjectId)
        assert str(result) == valid_id

    def test_valid_object_id_with_field_name(self):
        """Test validation with custom field name"""
        valid_id = "507f1f77bcf86cd799439011"
        result = validate_object_id(valid_id, field_name="TestID")
        assert isinstance(result, ObjectId)

    def test_empty_string(self):
        """Test that empty string raises ValueError"""
        with pytest.raises(ValueError, match="is required"):
            validate_object_id("")

    def test_wrong_length_short(self):
        """Test that ObjectID shorter than 24 chars raises ValueError"""
        with pytest.raises(ValueError, match="not 24 characters"):
            validate_object_id("507f1f77bcf86cd79943901")  # 23 chars

    def test_wrong_length_long(self):
        """Test that ObjectID longer than 24 chars raises ValueError"""
        with pytest.raises(ValueError, match="not 24 characters"):
            validate_object_id("507f1f77bcf86cd7994390112")  # 25 chars

    def test_invalid_hex_characters(self):
        """Test that non-hex characters raise ValueError"""
        with pytest.raises(ValueError, match="not a valid hexadecimal"):
            validate_object_id("507f1f77bcf86cd79943901g")  # 'g' is not hex

    def test_invalid_hex_characters_uppercase(self):
        """Test that uppercase non-hex characters raise ValueError"""
        with pytest.raises(ValueError, match="not a valid hexadecimal"):
            validate_object_id("507f1f77bcf86cd79943901G")  # 'G' is not hex

    def test_valid_uppercase_hex(self):
        """Test that uppercase hex characters are valid"""
        valid_id = "507F1F77BCF86CD799439011"
        result = validate_object_id(valid_id)
        assert isinstance(result, ObjectId)


class TestReadObjectIdsFromFile:
    """Tests for read_object_ids_from_file function"""

    def test_simple_object_ids(self):
        """Test reading simple ObjectIDs (one per line)"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("507f1f77bcf86cd799439011\n")
            f.write("507f1f77bcf86cd799439012\n")
            f.write("507f1f77bcf86cd799439013\n")
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 3
            assert "507f1f77bcf86cd799439011" in result
            assert "507f1f77bcf86cd799439012" in result
            assert "507f1f77bcf86cd799439013" in result
        finally:
            os.unlink(temp_path)

    def test_with_typed_prefixes(self):
        """Test reading ObjectIDs with type prefixes (prefixes are ignored)"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("environment:507f1f77bcf86cd799439011\n")
            f.write("model:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 2
            assert "507f1f77bcf86cd799439011" in result
            assert "507f1f77bcf86cd799439012" in result
        finally:
            os.unlink(temp_path)

    def test_with_comments(self):
        """Test that lines starting with # are ignored"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("# This is a comment\n")
            f.write("507f1f77bcf86cd799439011\n")
            f.write("  # Another comment\n")
            f.write("507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 2
            assert "507f1f77bcf86cd799439011" in result
            assert "507f1f77bcf86cd799439012" in result
        finally:
            os.unlink(temp_path)

    def test_with_empty_lines(self):
        """Test that empty lines are ignored"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("507f1f77bcf86cd799439011\n")
            f.write("\n")
            f.write("507f1f77bcf86cd799439012\n")
            f.write("   \n")  # Whitespace only
            f.write("507f1f77bcf86cd799439013\n")
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 3
        finally:
            os.unlink(temp_path)

    def test_with_invalid_object_ids(self):
        """Test that invalid ObjectIDs are logged but don't stop processing"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("507f1f77bcf86cd799439011\n")
            f.write("invalid_id\n")  # Too short
            f.write("507f1f77bcf86cd799439012\n")
            f.write("507f1f77bcf86cd79943901g\n")  # Invalid hex
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 2
            assert "507f1f77bcf86cd799439011" in result
            assert "507f1f77bcf86cd799439012" in result
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self):
        """Test that missing file returns empty list"""
        result = read_object_ids_from_file("/nonexistent/path/file.txt")
        assert result == []

    def test_mixed_formats(self):
        """Test file with mixed formats (bare IDs and typed)"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("507f1f77bcf86cd799439011\n")
            f.write("environment:507f1f77bcf86cd799439012\n")
            f.write("model:507f1f77bcf86cd799439013\n")
            temp_path = f.name

        try:
            result = read_object_ids_from_file(temp_path)
            assert len(result) == 3
        finally:
            os.unlink(temp_path)


class TestReadTypedObjectIdsFromFile:
    """Tests for read_typed_object_ids_from_file function"""

    def test_environment_prefixes(self):
        """Test reading environment ObjectIDs with various prefix formats"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("environment:507f1f77bcf86cd799439011\n")
            f.write("env:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert len(result["environment"]) == 2
            assert "507f1f77bcf86cd799439011" in result["environment"]
            assert "507f1f77bcf86cd799439012" in result["environment"]
        finally:
            os.unlink(temp_path)

    def test_environment_revision_prefixes(self):
        """Test reading environmentRevision ObjectIDs with various prefix formats"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("environmentRevision:507f1f77bcf86cd799439011\n")
            f.write("environment_revision:507f1f77bcf86cd799439012\n")
            f.write("envrevision:507f1f77bcf86cd799439013\n")
            f.write("env_rev:507f1f77bcf86cd799439014\n")
            f.write("envrev:507f1f77bcf86cd799439015\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment_revision" in result
            assert len(result["environment_revision"]) == 5
        finally:
            os.unlink(temp_path)

    def test_model_prefixes(self):
        """Test reading model ObjectIDs"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("model:507f1f77bcf86cd799439011\n")
            f.write("model:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "model" in result
            assert len(result["model"]) == 2
        finally:
            os.unlink(temp_path)

    def test_model_version_prefixes(self):
        """Test reading modelVersion ObjectIDs with various prefix formats"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("modelVersion:507f1f77bcf86cd799439011\n")
            f.write("model_version:507f1f77bcf86cd799439012\n")
            f.write("model_ver:507f1f77bcf86cd799439013\n")
            f.write("modelver:507f1f77bcf86cd799439014\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "model_version" in result
            assert len(result["model_version"]) == 4
        finally:
            os.unlink(temp_path)

    def test_mixed_types(self):
        """Test reading file with multiple types"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("environment:507f1f77bcf86cd799439011\n")
            f.write("model:507f1f77bcf86cd799439012\n")
            f.write("environmentRevision:507f1f77bcf86cd799439013\n")
            f.write("modelVersion:507f1f77bcf86cd799439014\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert "model" in result
            assert "environment_revision" in result
            assert "model_version" in result
            assert len(result["environment"]) == 1
            assert len(result["model"]) == 1
        finally:
            os.unlink(temp_path)

    def test_bare_ids_ignored(self):
        """Test that bare IDs (without prefix) are ignored"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("507f1f77bcf86cd799439011\n")  # Bare ID - should be ignored
            f.write("environment:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert len(result["environment"]) == 1
            assert "507f1f77bcf86cd799439012" in result["environment"]
            # Bare ID should not appear in any category
            all_ids = []
            for ids in result.values():
                all_ids.extend(ids)
            assert "507f1f77bcf86cd799439011" not in all_ids
        finally:
            os.unlink(temp_path)

    def test_unknown_prefix(self):
        """Test that unknown prefixes are logged and skipped"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("unknown:507f1f77bcf86cd799439011\n")
            f.write("environment:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert len(result["environment"]) == 1
            # Unknown prefix should not appear
            assert "unknown" not in result
        finally:
            os.unlink(temp_path)

    def test_case_insensitive_prefixes(self):
        """Test that prefixes are case-insensitive"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("ENVIRONMENT:507f1f77bcf86cd799439011\n")
            f.write("Model:507f1f77bcf86cd799439012\n")
            f.write("ENVIRONMENTREVISION:507f1f77bcf86cd799439013\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert "model" in result
            assert "environment_revision" in result
        finally:
            os.unlink(temp_path)

    def test_empty_keys_removed(self):
        """Test that empty keys are removed from result"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("environment:507f1f77bcf86cd799439011\n")
            # No model or other types
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert "environment" in result
            assert "model" not in result
            assert "environment_revision" not in result
            assert "model_version" not in result
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self):
        """Test that missing file returns empty dict"""
        result = read_typed_object_ids_from_file("/nonexistent/path/file.txt")
        assert result == {}

    def test_with_comments_and_empty_lines(self):
        """Test that comments and empty lines are handled"""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("# Comment line\n")
            f.write("environment:507f1f77bcf86cd799439011\n")
            f.write("\n")
            f.write("model:507f1f77bcf86cd799439012\n")
            temp_path = f.name

        try:
            result = read_typed_object_ids_from_file(temp_path)
            assert len(result["environment"]) == 1
            assert len(result["model"]) == 1
        finally:
            os.unlink(temp_path)


class TestFilterValuesByObjectIds:
    """Tests for filter_values_by_object_ids function"""

    def test_empty_object_ids_returns_all(self):
        """Test that empty object_ids list returns all values"""
        values = ["abc123", "def456", "ghi789"]
        result = filter_values_by_object_ids(values, [])
        assert result == values

    def test_none_object_ids_returns_all(self):
        """Test that None object_ids returns all values"""
        values = ["abc123", "def456", "ghi789"]
        result = filter_values_by_object_ids(values, None)
        assert result == values

    def test_single_match(self):
        """Test filtering with single matching ObjectID"""
        values = ["507f1f77bcf86cd799439011-1", "507f1f77bcf86cd799439012-1", "507f1f77bcf86cd799439013-1"]
        object_ids = ["507f1f77bcf86cd799439011"]
        result = filter_values_by_object_ids(values, object_ids)
        assert len(result) == 1
        assert "507f1f77bcf86cd799439011-1" in result

    def test_multiple_matches(self):
        """Test filtering with multiple matching ObjectIDs"""
        values = ["507f1f77bcf86cd799439011-1", "507f1f77bcf86cd799439012-1", "507f1f77bcf86cd799439013-1"]
        object_ids = ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"]
        result = filter_values_by_object_ids(values, object_ids)
        assert len(result) == 2
        assert "507f1f77bcf86cd799439011-1" in result
        assert "507f1f77bcf86cd799439012-1" in result

    def test_no_matches(self):
        """Test filtering with no matches"""
        values = ["507f1f77bcf86cd799439011-1", "507f1f77bcf86cd799439012-1"]
        object_ids = ["507f1f77bcf86cd799439999"]
        result = filter_values_by_object_ids(values, object_ids)
        assert result == []

    def test_exact_match(self):
        """Test that exact matches are included"""
        values = ["507f1f77bcf86cd799439011"]
        object_ids = ["507f1f77bcf86cd799439011"]
        result = filter_values_by_object_ids(values, object_ids)
        assert len(result) == 1
        assert "507f1f77bcf86cd799439011" in result

    def test_prefix_matching(self):
        """Test that values starting with ObjectID are matched"""
        values = ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439011-1", "507f1f77bcf86cd799439011-v2"]
        object_ids = ["507f1f77bcf86cd799439011"]
        result = filter_values_by_object_ids(values, object_ids)
        assert len(result) == 3
        assert all(v.startswith("507f1f77bcf86cd799439011") for v in result)


class TestStartsWithAnyObjectId:
    """Tests for starts_with_any_object_id function"""

    def test_empty_object_ids_returns_false(self):
        """Test that empty object_ids returns False"""
        assert starts_with_any_object_id("507f1f77bcf86cd799439011", []) == False

    def test_none_object_ids_returns_false(self):
        """Test that None object_ids returns False"""
        assert starts_with_any_object_id("507f1f77bcf86cd799439011", None) == False

    def test_single_match(self):
        """Test with single matching ObjectID"""
        assert starts_with_any_object_id("507f1f77bcf86cd799439011-1", ["507f1f77bcf86cd799439011"]) == True

    def test_multiple_object_ids_one_match(self):
        """Test with multiple ObjectIDs where one matches"""
        assert (
            starts_with_any_object_id(
                "507f1f77bcf86cd799439011-1", ["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"]
            )
            == True
        )

    def test_no_match(self):
        """Test with no matching ObjectID"""
        assert starts_with_any_object_id("507f1f77bcf86cd799439011-1", ["507f1f77bcf86cd799439999"]) == False

    def test_exact_match(self):
        """Test that exact match returns True"""
        assert starts_with_any_object_id("507f1f77bcf86cd799439011", ["507f1f77bcf86cd799439011"]) == True

    def test_prefix_not_at_start(self):
        """Test that ObjectID in middle doesn't match"""
        assert starts_with_any_object_id("prefix-507f1f77bcf86cd799439011", ["507f1f77bcf86cd799439011"]) == False
