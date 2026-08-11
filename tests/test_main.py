"""
Tests for python/main.py — CLI entrypoint.

Focused on the ensure_reports subcommand, which is the only non-trivial logic
that lives in main.py itself (all other subcommands simply exec a script file).

Run with:
    pytest tests/test_main.py
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("SKIP_CONFIG_VALIDATION", "true")

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

from main import main

# ── ensure_reports ─────────────────────────────────────────────────────────────


class TestEnsureReports:
    """ensure_reports: run 'reports' only when final-report.json is stale or missing."""

    def _call(self, tmp_path, extra_args=()):
        """
        Invoke main() with ensure_reports and a patched reports dir.
        Returns (exit_code, run_script_mock).
        """
        with patch("utils.report_utils.get_reports_dir", return_value=tmp_path):
            with patch("main.run_script") as mock_run:
                with patch.object(sys, "argv", ["main.py", "ensure_reports"] + list(extra_args)):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
        return exc_info.value.code, mock_run

    def test_fresh_report_skips_run(self, tmp_path):
        (tmp_path / "final-report.json").write_text("{}")
        code, mock_run = self._call(tmp_path)
        assert code == 0
        mock_run.assert_not_called()

    def test_missing_report_triggers_run(self, tmp_path):
        code, mock_run = self._call(tmp_path)
        assert code == 0
        mock_run.assert_called_once()
        assert "reports.py" in mock_run.call_args[0][0]

    def test_stale_report_triggers_run(self, tmp_path):
        report = tmp_path / "final-report.json"
        report.write_text("{}")
        old_mtime = time.time() - 25 * 3600  # 25 hours ago
        os.utime(report, (old_mtime, old_mtime))

        code, mock_run = self._call(tmp_path)
        assert code == 0
        mock_run.assert_called_once()
        assert "reports.py" in mock_run.call_args[0][0]

    def test_max_age_hours_flag_marks_90min_report_stale(self, tmp_path):
        report = tmp_path / "final-report.json"
        report.write_text("{}")
        ninety_min_ago = time.time() - 90 * 60
        os.utime(report, (ninety_min_ago, ninety_min_ago))

        code, mock_run = self._call(tmp_path, ["--max-age-hours", "1"])
        assert code == 0
        mock_run.assert_called_once()

    def test_max_age_hours_flag_marks_90min_report_fresh(self, tmp_path):
        report = tmp_path / "final-report.json"
        report.write_text("{}")
        ninety_min_ago = time.time() - 90 * 60
        os.utime(report, (ninety_min_ago, ninety_min_ago))

        code, mock_run = self._call(tmp_path, ["--max-age-hours", "2"])
        assert code == 0
        mock_run.assert_not_called()

    def test_ensure_reports_is_a_valid_subcommand(self):
        """ensure_reports must be in the argparse choices or it won't appear in --help."""
        from main import load_script_paths

        assert "ensure_reports" in load_script_paths()
