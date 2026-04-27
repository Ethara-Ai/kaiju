"""Tests for tools.stub_ts_runner — subprocess wrapper for the ts-morph
stubbing engine.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "tools.stub_ts_runner"


class TestRunStubTs:
    def test_success(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 10,
            "files_modified": 5,
            "functions_stubbed": 20,
            "functions_preserved": 3,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                result = run_stub_ts(src_dir=tmp_path / "src")

        assert result["files_processed"] == 10
        assert result["functions_stubbed"] == 20

    def test_extra_scan_dirs(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 0,
            "files_modified": 0,
            "functions_stubbed": 0,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                run_stub_ts(
                    src_dir=tmp_path / "src",
                    extra_scan_dirs=[tmp_path / "tests", tmp_path / "utils"],
                )

        cmd = mock_run.call_args[0][0]
        assert "--extra-scan-dirs" in cmd
        scan_arg = cmd[cmd.index("--extra-scan-dirs") + 1]
        assert "tests" in scan_arg
        assert "utils" in scan_arg

    def test_verbose_flag(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 0,
            "files_modified": 0,
            "functions_stubbed": 0,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = "debug info"

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                run_stub_ts(src_dir=tmp_path / "src", verbose=True)

        cmd = mock_run.call_args[0][0]
        assert "--verbose" in cmd

    def test_nonzero_exit_raises(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "SyntaxError: unexpected token"

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="TS stubber failed"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_empty_stdout_raises(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="no output"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json at all"
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="no JSON object"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_json_with_preamble(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 1,
            "files_modified": 1,
            "functions_stubbed": 2,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ts-node debug info\n" + json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                result = run_stub_ts(src_dir=tmp_path / "src")

        assert result["files_processed"] == 1

    def test_missing_stub_ts_raises(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="not found"):
                run_stub_ts(src_dir=tmp_path / "src")

    def test_timeout_passed_to_subprocess(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 0,
            "files_modified": 0,
            "functions_stubbed": 0,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                run_stub_ts(src_dir=tmp_path / "src", timeout=600)

        assert mock_run.call_args[1]["timeout"] == 600

    def test_subprocess_timeout_propagates(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        with patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 300),
        ):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(subprocess.TimeoutExpired):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_mode_argument(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 0,
            "files_modified": 0,
            "functions_stubbed": 0,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                run_stub_ts(src_dir=tmp_path / "src", mode="all")

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--mode")
        assert cmd[idx + 1] == "all"

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"files_processed": 1, "broken'
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="no JSON object"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_report_with_errors(self, tmp_path: Path) -> None:
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 5,
            "files_modified": 3,
            "functions_stubbed": 8,
            "functions_preserved": 1,
            "errors": ["Failed to parse src/complex.ts"],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                result = run_stub_ts(src_dir=tmp_path / "src")

        assert len(result["errors"]) == 1

    def test_invalid_json_between_braces_raises(self, tmp_path: Path) -> None:
        """Stdout has braces but content is not valid JSON → JSONDecodeError path."""
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "some preamble { not: valid, json } end"
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="not JSON"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_multiple_json_objects_in_stdout_raises(self, tmp_path: Path) -> None:
        """Two JSON objects back-to-back: first '{' to last '}' spans invalid JSON."""
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"a":1} {"b":2}'
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="not JSON"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_nested_braces_extracts_correct_json(self, tmp_path: Path) -> None:
        """Debug output with braces before real JSON; rfind('}') grabs the right end."""
        from tools.stub_ts_runner import run_stub_ts

        report = {"files_processed": 1, "nested": {"a": 1}}

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "debug output { warning } \n" + json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="not JSON"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_whitespace_only_stdout_raises(self, tmp_path: Path) -> None:
        """Stdout with only whitespace after strip → 'no output' error."""
        from tools.stub_ts_runner import run_stub_ts

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   \n\t  \n  "
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                with pytest.raises(RuntimeError, match="no output"):
                    run_stub_ts(src_dir=tmp_path / "src")

    def test_verbose_false_no_verbose_flag(self, tmp_path: Path) -> None:
        """verbose=False should NOT add --verbose to the command."""
        from tools.stub_ts_runner import run_stub_ts

        report = {
            "files_processed": 0,
            "files_modified": 0,
            "functions_stubbed": 0,
            "functions_preserved": 0,
            "errors": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(report)
        mock_result.stderr = ""

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            with patch(f"{MODULE}.TOOLS_DIR", tmp_path):
                (tmp_path / "stub_ts.ts").write_text("")
                run_stub_ts(src_dir=tmp_path / "src", verbose=False)

        cmd = mock_run.call_args[0][0]
        assert "--verbose" not in cmd
