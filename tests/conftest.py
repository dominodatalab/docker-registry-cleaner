"""
Pytest configuration file.

Sets up the Python path so test files can import from the python/ directory.
"""
import sys
from pathlib import Path

# Add python directory to path for all tests
_python_dir = Path(__file__).parent.parent / 'python'
_python_dir_abs = str(_python_dir.absolute())
if _python_dir_abs not in sys.path:
    sys.path.insert(0, _python_dir_abs)
