from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ErrorCategory(Enum):
    CODE = "code"
    ENVIRONMENT = "env"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    line: str
    category: ErrorCategory
    reason: Optional[str]


PYRIGHT_ENV_RULES = frozenset(
    {
        "reportMissingImports",
        "reportMissingModuleSource",
        "reportMissingTypeStubs",
    }
)

PYRIGHT_CODE_RULES = frozenset(
    {
        "reportGeneralClassIssue",
        "reportUndefinedVariable",
        "reportAttributeAccessIssue",
        "reportIndexIssue",
        "reportCallIssue",
        "reportReturnType",
        "reportAssignmentType",
        "reportArgumentType",
        "reportOptionalMemberAccess",
        "reportOptionalSubscript",
        "reportOptionalCall",
    }
)


def classify_pyright_line(
    line: str,
    project_package: str,
    known_deps: set[str],
) -> ClassifiedError:
    m = re.search(r"\[(\w+)\]$", line.strip())
    if not m:
        return ClassifiedError(line, ErrorCategory.UNKNOWN, None)

    rule_code = m.group(1)

    if rule_code in PYRIGHT_ENV_RULES:
        import_match = re.search(r'Import "(\w+)"', line)
        if import_match:
            import_name = import_match.group(1).lower()
            if import_name == project_package.lower():
                return ClassifiedError(
                    line,
                    ErrorCategory.CODE,
                    f"Missing import of own package '{import_name}'",
                )
            if import_name in known_deps:
                return ClassifiedError(
                    line,
                    ErrorCategory.ENVIRONMENT,
                    f"'{import_name}' is a known dependency — env issue",
                )
        return ClassifiedError(
            line, ErrorCategory.ENVIRONMENT, f"Rule {rule_code} is environment-related"
        )

    if rule_code in PYRIGHT_CODE_RULES:
        return ClassifiedError(
            line, ErrorCategory.CODE, f"Rule {rule_code} is code-related"
        )

    return ClassifiedError(line, ErrorCategory.UNKNOWN, f"Unknown rule {rule_code}")


@dataclass
class FilterResult:
    output: str
    suppressed_count: int
    code_error_count: int


def filter_lint_output(
    raw_output: str,
    project_package: str,
    known_deps: set[str],
    keep_unknown: bool = True,
) -> FilterResult:
    lines = raw_output.splitlines()
    filtered: list[str] = []
    suppressed_count = 0
    code_error_count = 0

    for line in lines:
        if re.search(r"- (error|warning|information):", line):
            classified = classify_pyright_line(line, project_package, known_deps)
            if classified.category == ErrorCategory.ENVIRONMENT:
                suppressed_count += 1
                continue
            if classified.category == ErrorCategory.UNKNOWN and not keep_unknown:
                suppressed_count += 1
                continue
            if classified.category == ErrorCategory.CODE:
                code_error_count += 1
        filtered.append(line)

    if suppressed_count > 0:
        filtered.append(
            f"\n[commit0] Suppressed {suppressed_count} environment-related "
            f"lint error(s) (missing external dependencies)."
        )

    return FilterResult(
        output="\n".join(filtered),
        suppressed_count=suppressed_count,
        code_error_count=code_error_count,
    )


__all__ = [
    "ClassifiedError",
    "ErrorCategory",
    "FilterResult",
    "PYRIGHT_CODE_RULES",
    "PYRIGHT_ENV_RULES",
    "classify_pyright_line",
    "filter_lint_output",
]
