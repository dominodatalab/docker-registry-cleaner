# Tests

This directory contains unit and integration tests for the docker-registry-cleaner project.

## Setup

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

## Running Tests

Run all tests:

```bash
pytest
```

Run specific test file:

```bash
pytest tests/test_object_id_utils.py
pytest tests/test_report_utils.py
pytest tests/test_integration_shared_layers.py
pytest tests/test_integration_deletion_logic.py
```

Run only unit tests:

```bash
pytest tests/test_object_id_utils.py tests/test_report_utils.py
```

Run only integration tests:

```bash
pytest tests/test_integration_shared_layers.py tests/test_integration_deletion_logic.py
```

Run with coverage report:

```bash
pytest --cov=python --cov-report=html
```

View coverage report:

```bash
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

## Test Structure

### Unit Tests
- `test_object_id_utils.py` - Tests for ObjectID validation and file parsing
- `test_report_utils.py` - Tests for JSON and report file operations

### Integration Tests
- `test_integration_shared_layers.py` - Tests for shared layer calculation logic
- `test_integration_deletion_logic.py` - Tests for deletion flow and component interactions

## Writing New Tests

Follow these conventions:

1. Test files should be named `test_*.py`
2. Test classes should be named `Test*`
3. Test functions should be named `test_*`
4. Use descriptive test names that explain what is being tested
5. Use fixtures for common setup/teardown
6. Use temporary files/directories for file I/O tests

Example:

```python
def test_function_name_behavior(self):
    """Test description"""
    # Arrange
    input_data = "test"
    
    # Act
    result = function_under_test(input_data)
    
    # Assert
    assert result == expected_output
```
