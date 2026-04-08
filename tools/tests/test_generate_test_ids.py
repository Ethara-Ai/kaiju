"""Tests for generate_test_ids module."""

import pytest
from tools.generate_test_ids import _parse_collect_output


class TestParseCollectOutput:
    def test_quiet_format(self):
        """Standard quiet output with path::test lines."""
        output = (
            "tests/test_foo.py::test_bar\n"
            "tests/test_foo.py::TestClass::test_method\n"
            "\n"
            "== 2 tests collected in 0.01s ==\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_foo.py::test_bar",
            "tests/test_foo.py::TestClass::test_method",
        ]

    def test_verbose_format(self):
        """Verbose output with <Module>::<Class>::<Function> tags."""
        output = (
            "<Module tests/test_foo.py>::<Function test_bar>\n"
            "<Module tests/test_foo.py>::<Class TestBaz>::<Function test_qux>\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_foo.py::test_bar",
            "tests/test_foo.py::TestBaz::test_qux",
        ]

    def test_empty_output(self):
        result = _parse_collect_output("")
        assert result == []

    def test_only_summary_lines(self):
        """When output only contains summary (no individual IDs)."""
        output = "== 42 tests collected in 1.23s ==\n"
        result = _parse_collect_output(output)
        assert result == []

    def test_mixed_with_errors(self):
        """Error lines should be filtered out."""
        output = (
            "ERROR: could not collect tests\n"
            "tests/test_a.py::test_one\n"
            "== 1 test collected, 3 errors ==\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_a.py::test_one"]

    def test_separator_lines_filtered(self):
        """Lines starting with = or - are separator/summary lines."""
        output = (
            "======= test session starts =======\n"
            "tests/test_a.py::test_one\n"
            "-------\n"
            "== 1 test collected ==\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_a.py::test_one"]

    def test_parametrized_ids(self):
        """Parametrized test IDs with brackets."""
        output = "tests/test_foo.py::test_bar[param1-param2]\n"
        result = _parse_collect_output(output)
        assert result == ["tests/test_foo.py::test_bar[param1-param2]"]
