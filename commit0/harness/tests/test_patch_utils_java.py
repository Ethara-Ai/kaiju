from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.patch_utils_java"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


class TestGenerateJavaPatch:
    @patch(f"{MODULE}.subprocess.run")
    def test_basic_diff(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_run.return_value = _completed(stdout="diff --git a/Foo.java b/Foo.java\n+x")
        from commit0.harness.patch_utils_java import generate_java_patch

        result = generate_java_patch(str(tmp_path))
        assert result is not None
        assert "Foo.java" in result

    @patch(f"{MODULE}.subprocess.run")
    def test_include_only_java_filters(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_run.return_value = _completed(stdout="diff")
        from commit0.harness.patch_utils_java import generate_java_patch

        generate_java_patch(str(tmp_path), include_only_java=True)
        cmd = mock_run.call_args[0][0]
        assert "--" in cmd
        assert "*.java" in cmd

    @patch(f"{MODULE}.subprocess.run")
    def test_no_changes_empty(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_run.return_value = _completed(stdout="   ")
        from commit0.harness.patch_utils_java import generate_java_patch

        result = generate_java_patch(str(tmp_path))
        assert result is None

    @patch(f"{MODULE}.subprocess.run")
    def test_base_branch_param(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_run.return_value = _completed(stdout="diff")
        from commit0.harness.patch_utils_java import generate_java_patch

        generate_java_patch(str(tmp_path), base_branch="my-branch")
        cmd = mock_run.call_args[0][0]
        assert "my-branch" in cmd

    @patch(f"{MODULE}.subprocess.run")
    def test_non_java_excluded(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_run.return_value = _completed(stdout="diff")
        from commit0.harness.patch_utils_java import generate_java_patch

        generate_java_patch(str(tmp_path), include_only_java=False)
        cmd = mock_run.call_args[0][0]
        assert "*.java" not in cmd


class TestApplyJavaPatch:
    @patch(f"{MODULE}.subprocess.run")
    def test_successful_apply(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = _completed(returncode=0)
        from commit0.harness.patch_utils_java import apply_java_patch

        result = apply_java_patch(str(tmp_path), "diff content")
        assert result is True
        assert mock_run.call_count == 2

    @patch(f"{MODULE}.subprocess.run")
    def test_check_fails_raises(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = _completed(returncode=1, stderr="bad patch")
        from commit0.harness.patch_utils_java import apply_java_patch

        result = apply_java_patch(str(tmp_path), "bad diff")
        assert result is False

    @patch(f"{MODULE}.subprocess.run")
    def test_empty_patch_succeeds(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = _completed(returncode=0)
        from commit0.harness.patch_utils_java import apply_java_patch

        result = apply_java_patch(str(tmp_path), "")
        assert result is True


class TestFilterPatchJavaOnly:
    def test_keeps_java_files(self) -> None:
        from commit0.harness.patch_utils_java import filter_patch_java_only

        patch = (
            "diff --git a/Foo.java b/Foo.java\n"
            "+public class Foo {}\n"
        )
        result = filter_patch_java_only(patch)
        assert "Foo.java" in result
        assert "+public class Foo {}" in result

    def test_removes_non_java(self) -> None:
        from commit0.harness.patch_utils_java import filter_patch_java_only

        patch = (
            "diff --git a/readme.md b/readme.md\n"
            "+# Hello\n"
            "diff --git a/Bar.java b/Bar.java\n"
            "+class Bar {}\n"
        )
        result = filter_patch_java_only(patch)
        assert "readme.md" not in result
        assert "Bar.java" in result

    def test_empty_patch_returns_empty(self) -> None:
        from commit0.harness.patch_utils_java import filter_patch_java_only

        result = filter_patch_java_only("")
        assert result == ""


class TestEdgeCases:
    def test_filter_none_raises_attribute_error(self) -> None:
        from commit0.harness.patch_utils_java import filter_patch_java_only

        with pytest.raises(AttributeError):
            filter_patch_java_only(None)  # type: ignore[arg-type]

    def test_non_java_patch_returns_empty(self) -> None:
        from commit0.harness.patch_utils_java import filter_patch_java_only

        patch = (
            "diff --git a/pom.xml b/pom.xml\n"
            "+<dependency>\n"
        )
        result = filter_patch_java_only(patch)
        assert result.strip() == ""
