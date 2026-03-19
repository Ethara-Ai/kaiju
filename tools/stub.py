"""
AST-based Python code stubbing tool.

Replaces function/method bodies with `pass` statements while preserving:
- All imports and module-level code
- Class definitions and class-level variables
- Function signatures, decorators, type annotations
- Docstrings (configurable)
- Abstract methods, overloads, protocol stubs (already stubs)

This is the missing tool from commit0 (arXiv:2412.01769, Section 3.2).

Usage:
    python -m tools.stub /path/to/repo /path/to/output [--strip-docstrings] [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import logging
import shutil
import sys
import textwrap
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# Directories to skip
SKIP_DIRS: set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    ".tox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".nox",
}


def is_test_file(path: Path) -> bool:
    """Check if a file is a test file (should be skipped)."""
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "/tests/" in str(path)
        or "/test/" in str(path)
    )


def is_docstring(node: ast.stmt) -> bool:
    """Check if an AST statement is a docstring (string expression)."""
    return isinstance(node, ast.Expr) and isinstance(
        node.value, (ast.Constant, ast.Str)
    )


def is_pure_assignment_init(func_node: ast.FunctionDef) -> bool:
    """Check if an __init__ method only does self.x = ... assignments.

    These are kept because they define instance attributes needed for class structure.
    """
    if func_node.name != "__init__":
        return False

    for stmt in func_node.body:
        # Skip docstrings
        if is_docstring(stmt):
            continue
        # Allow: self.x = expr
        if isinstance(stmt, ast.Assign):
            if all(
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
                for t in stmt.targets
            ):
                continue
        # Allow: self.x: type = expr (annotated assignment)
        if isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
        # Allow: super().__init__(...) calls
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "__init__":
                if isinstance(call.func.value, ast.Call):
                    func = call.func.value
                    if isinstance(func.func, ast.Name) and func.func.id == "super":
                        continue
        # Any other statement means this isn't pure assignment
        return False

    return True


def has_abstractmethod(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @abstractmethod decorator."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "abstractmethod":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod":
            return True
    return False


def has_overload(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @overload decorator."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def is_already_stub(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function body is already a stub (pass, ..., or raise NotImplementedError)."""
    body = func_node.body

    # Skip docstring if present
    start = 1 if body and is_docstring(body[0]) else 0
    remaining = body[start:]

    if not remaining:
        return True

    if len(remaining) == 1:
        stmt = remaining[0]
        # pass
        if isinstance(stmt, ast.Pass):
            return True
        # ... (Ellipsis)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if stmt.value.value is ...:
                return True
        # raise NotImplementedError
        if isinstance(stmt, ast.Raise):
            return True

    return False


class StubTransformer:
    """Transforms Python source by replacing function bodies with pass.

    Uses a line-based approach to preserve comments and formatting:
    1. Parse AST to identify function body ranges
    2. Collect replacement ranges (body start line, body end line)
    3. Reconstruct source with bodies replaced
    """

    def __init__(self, *, keep_docstrings: bool = True) -> None:
        self.keep_docstrings = keep_docstrings
        self.stub_count = 0

    def transform_source(self, source: str, filename: str = "<unknown>") -> str | None:
        """Transform a Python source string, returning stubbed version.

        Returns None if the file has no functions to stub.
        """
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            logger.warning("Syntax error in %s: %s — copying as-is", filename, e)
            return None

        lines = source.splitlines(keepends=True)
        if not lines:
            return source

        # Collect all function bodies to replace
        replacements = self._collect_replacements(tree, lines)

        if not replacements:
            return source

        replacements = self._remove_nested(replacements)
        replacements.sort(key=lambda r: r[0], reverse=True)

        for body_start, body_end, indent_str in replacements:
            new_lines = [f"{indent_str}pass\n"]

            # Replace lines (body_start and body_end are 0-indexed)
            lines[body_start : body_end + 1] = new_lines
            self.stub_count += 1

        return "".join(lines)

    def _collect_replacements(
        self,
        tree: ast.Module,
        lines: list[str],
    ) -> list[tuple[int, int, str]]:
        """Collect (body_start_0idx, body_end_0idx, indent) for each function to stub."""
        replacements: list[tuple[int, int, str]] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Skip functions that should be preserved
            if has_abstractmethod(node):
                continue
            if has_overload(node):
                continue
            if is_already_stub(node):
                continue
            if isinstance(node, ast.FunctionDef) and is_pure_assignment_init(node):
                continue

            body = node.body
            if not body:
                continue

            # Determine body range (1-indexed in AST, convert to 0-indexed)
            body_start_1 = body[0].lineno
            body_end_1 = self._get_end_lineno(body[-1], lines)

            body_start_0 = body_start_1 - 1
            body_end_0 = body_end_1 - 1

            # Determine indentation from the first body statement
            indent_str = self._get_indent(lines, body_start_0)

            # Handle docstring preservation
            # When keep_docstrings is True and body starts with a docstring,
            # adjust body_start_0 to AFTER the docstring. The docstring stays
            # in the source untouched — we only replace the code after it.
            if self.keep_docstrings and body and is_docstring(body[0]):
                doc_node = body[0]
                doc_end_1 = self._get_end_lineno(doc_node, lines)
                doc_end_0 = doc_end_1 - 1

                if len(body) > 1:
                    # There's code after the docstring — stub starts after docstring
                    body_start_0 = doc_end_0 + 1
                else:
                    # Only docstring in body — already a valid stub, skip
                    continue

            replacements.append((body_start_0, body_end_0, indent_str))

        return replacements

    @staticmethod
    def _remove_nested(
        replacements: list[tuple[int, int, str]],
    ) -> list[tuple[int, int, str]]:
        """Filter out replacements whose range is entirely inside another's range."""
        sorted_by_range = sorted(replacements, key=lambda r: (r[0], -r[1]))
        result: list[tuple[int, int, str]] = []

        for start, end, indent in sorted_by_range:
            is_nested = any(ps <= start and end <= pe for ps, pe, _ in result)
            if not is_nested:
                result.append((start, end, indent))

        return result

    @staticmethod
    def _get_end_lineno(node: ast.AST, lines: list[str]) -> int:
        """Get the end line number of an AST node (1-indexed).

        Falls back to scanning for the last non-empty line if end_lineno is not available.
        """
        end = getattr(node, "end_lineno", None)
        if end is not None:
            return end

        # Fallback: use node's line number (imprecise but safe)
        return getattr(node, "lineno", len(lines))

    @staticmethod
    def _get_indent(lines: list[str], line_idx: int) -> str:
        """Extract the leading whitespace from a line."""
        if line_idx < len(lines):
            line = lines[line_idx]
            stripped = line.lstrip()
            return line[: len(line) - len(stripped)]
        return "    "  # Default to 4 spaces


def stub_file(
    source_path: Path,
    output_path: Path,
    *,
    keep_docstrings: bool = True,
    dry_run: bool = False,
) -> tuple[bool, int]:
    """Stub a single Python file.

    Returns (was_modified, stub_count).
    """
    try:
        source = source_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        logger.warning("Cannot read %s: %s — skipping", source_path, e)
        return False, 0

    transformer = StubTransformer(keep_docstrings=keep_docstrings)
    result = transformer.transform_source(source, str(source_path))

    if result is None:
        # Syntax error — copy as-is
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, output_path)
        return False, 0

    if result == source:
        # No functions to stub — copy as-is
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(result, encoding="utf-8")
        return False, 0

    # Verify the result is valid Python
    try:
        ast.parse(result, str(output_path))
    except SyntaxError as e:
        logger.error(
            "Stubbing produced invalid Python for %s: %s — copying original",
            source_path,
            e,
        )
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, output_path)
        return False, 0

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result, encoding="utf-8")

    return True, transformer.stub_count


def stub_directory(
    source_dir: Path,
    output_dir: Path,
    *,
    keep_docstrings: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Stub all Python files in a directory tree.

    Returns summary stats.
    """
    stats = {
        "files_processed": 0,
        "files_modified": 0,
        "files_skipped": 0,
        "files_copied": 0,
        "total_stubs": 0,
        "test_files_skipped": 0,
        "errors": 0,
    }

    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()

    for py_file in sorted(source_dir.rglob("*.py")):
        # Skip directories we don't want
        rel_parts = py_file.relative_to(source_dir).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue

        # Skip .pyi files
        if py_file.suffix == ".pyi":
            continue

        # Skip test files
        rel_path = py_file.relative_to(source_dir)
        if is_test_file(py_file):
            stats["test_files_skipped"] += 1
            # Copy test files as-is (they need to work for evaluation)
            if not dry_run:
                out = output_dir / rel_path
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(py_file, out)
            if verbose:
                logger.info("  [TEST] %s — copied as-is", rel_path)
            continue

        stats["files_processed"] += 1

        out_path = output_dir / rel_path
        modified, count = stub_file(
            py_file,
            out_path,
            keep_docstrings=keep_docstrings,
            dry_run=dry_run,
        )

        if modified:
            stats["files_modified"] += 1
            stats["total_stubs"] += count
            if verbose:
                logger.info("  [STUB] %s — %d function(s) stubbed", rel_path, count)
        else:
            stats["files_copied"] += 1
            if verbose:
                logger.info("  [COPY] %s — no functions to stub", rel_path)

    # Copy non-Python files needed for the project
    _copy_non_python_files(source_dir, output_dir, dry_run=dry_run)

    return stats


def _copy_non_python_files(
    source_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Copy essential non-Python files (configs, data, etc.)."""
    # Essential config files to copy
    config_patterns = [
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "MANIFEST.in",
        "requirements*.txt",
        "tox.ini",
        "pytest.ini",
        "conftest.py",
        ".coveragerc",
        "Makefile",
        "LICENSE*",
        "README*",
    ]

    for pattern in config_patterns:
        for f in source_dir.glob(pattern):
            if f.is_file():
                rel = f.relative_to(source_dir)
                if not dry_run:
                    out = output_dir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)


def print_summary(stats: dict, output_dir: Path) -> None:
    """Print a human-readable summary."""
    print(f"\n{'=' * 60}")
    print("STUBBING SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Output directory:     {output_dir}")
    print(f"  Files processed:      {stats['files_processed']}")
    print(f"  Files modified:       {stats['files_modified']}")
    print(f"  Files copied (no fn): {stats['files_copied']}")
    print(f"  Test files skipped:   {stats['test_files_skipped']}")
    print(f"  Total stubs created:  {stats['total_stubs']}")
    if stats["errors"]:
        print(f"  Errors:               {stats['errors']}")
    print(f"{'=' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stub Python function bodies with pass statements"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Source directory (repo root)",
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Output directory for stubbed files",
    )
    parser.add_argument(
        "--strip-docstrings",
        action="store_true",
        help="Remove docstrings from stubbed functions (default: keep them)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-file details",
    )

    args = parser.parse_args()

    if not args.source.is_dir():
        logger.error("Source directory does not exist: %s", args.source)
        sys.exit(1)

    if args.output.exists() and not args.dry_run:
        logger.warning(
            "Output directory exists: %s — files may be overwritten", args.output
        )

    logger.info("Stubbing %s → %s", args.source, args.output)

    stats = stub_directory(
        args.source,
        args.output,
        keep_docstrings=not args.strip_docstrings,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    print_summary(stats, args.output)


if __name__ == "__main__":
    main()
