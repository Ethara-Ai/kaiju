"""Go-specific agent utilities for commit0.

Mirrors agent_utils.py but operates on Go source files, Go stub markers,
and Go-specific tooling (goimports, staticcheck, go vet).
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import git
import yaml

from agent.class_types import AgentConfig
from commit0.harness.constants_go import (
    GO_SKIP_FILENAMES,
    GO_STUB_MARKER,
    GO_SOURCE_EXT,
    GO_TEST_FILE_SUFFIX,
    Language,
)

logger = logging.getLogger(__name__)

EXCLUDED_DIRS = {
    ".git",
    "vendor",
    "testdata",
    "internal/testdata",
    "node_modules",
    ".github",
    ".vscode",
}

PROMPT_HEADER = "## Task\n"
REFERENCE_HEADER = "## Reference Information\n"
REPO_INFO_HEADER = "### Repository Structure\n"
TEST_INFO_HEADER = "### Test Information\n"
SPEC_INFO_HEADER = "### Specification\n"
LINT_INFO_HEADER = "### Lint Results\n"

GO_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "go_system_prompt.md"


def collect_go_files(directory: str) -> list[str]:
    """Collect all .go source files excluding tests, vendor, and doc files."""
    go_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for f in files:
            if not f.endswith(GO_SOURCE_EXT):
                continue
            if f.endswith(GO_TEST_FILE_SUFFIX):
                continue
            if f in GO_SKIP_FILENAMES:
                continue
            go_files.append(os.path.join(root, f))
    return sorted(go_files)


def collect_go_test_files(directory: str) -> list[str]:
    """Collect all Go test files."""
    test_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for f in files:
            if f.endswith(GO_TEST_FILE_SUFFIX):
                test_files.append(os.path.join(root, f))
    return sorted(test_files)


def extract_go_function_stubs(file_path: str) -> list[dict]:
    """Extract stubbed Go functions from a file by scanning for the stub marker.

    Returns a list of dicts with keys: name, file, line, signature.
    """
    stubs = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return stubs

    func_pattern = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(")
    current_func = None
    current_func_line = 0
    current_func_sig = ""

    for i, line in enumerate(lines, 1):
        m = func_pattern.match(line)
        if m:
            current_func = m.group(1)
            current_func_line = i
            current_func_sig = line.rstrip()
        if current_func and GO_STUB_MARKER in line:
            stubs.append(
                {
                    "name": current_func,
                    "file": file_path,
                    "line": current_func_line,
                    "signature": current_func_sig,
                }
            )
            current_func = None

    return stubs


def get_dir_info(
    base_dir: str,
    src_dir: str = ".",
    max_length: int = 10000,
    show_stubs: bool = False,
) -> str:
    """Build a tree-style directory listing of Go source files."""
    target = os.path.join(base_dir, src_dir) if src_dir != "." else base_dir
    lines: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        rel = os.path.relpath(root, base_dir)
        depth = rel.count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(root)}/")
        for f in sorted(files):
            if not f.endswith(GO_SOURCE_EXT):
                continue
            if f.endswith(GO_TEST_FILE_SUFFIX) or f in GO_SKIP_FILENAMES:
                continue
            fpath = os.path.join(root, f)
            sub_indent = "  " * (depth + 1)
            lines.append(f"{sub_indent}{f}")
            if show_stubs:
                for stub in extract_go_function_stubs(fpath):
                    lines.append(f"{sub_indent}  [STUB] {stub['signature']}")

    result = "\n".join(lines)
    if len(result) > max_length:
        result = result[:max_length] + "\n... (truncated)"
    return result


def _find_go_files_to_edit(
    base_dir: str,
    src_dir: str = ".",
) -> list[str]:
    """Find Go source files that are candidates for editing (non-test, non-vendor)."""
    target = os.path.join(base_dir, src_dir) if src_dir != "." else base_dir
    return collect_go_files(target)


def get_target_edit_files(
    local_repo: str,
    src_dir: str,
    branch: str,
    reference_commit: str,
) -> list[str]:
    """Find Go files containing stub markers that differ from the reference commit.

    Unlike Python's topological sort approach, Go files are returned in
    filesystem order since Go has no equivalent of import_deps.ModuleSet.
    """
    repo = git.Repo(local_repo)
    all_go_files = _find_go_files_to_edit(local_repo, src_dir)

    stubbed_files = []
    for fpath in all_go_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if GO_STUB_MARKER in content:
                stubbed_files.append(fpath)
        except OSError:
            continue

    if not stubbed_files:
        return all_go_files

    try:
        ref_tree = repo.commit(reference_commit).tree
    except Exception:
        return stubbed_files

    target_files = []
    for fpath in stubbed_files:
        rel_path = os.path.relpath(fpath, local_repo)
        try:
            ref_blob = ref_tree / rel_path
            ref_content = ref_blob.data_stream.read().decode("utf-8", errors="replace")
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                current_content = f.read()
            if current_content != ref_content:
                target_files.append(fpath)
        except (KeyError, Exception):
            target_files.append(fpath)

    return target_files if target_files else stubbed_files


def get_target_edit_files_from_patch(
    local_repo: str,
    patch: str,
) -> list[str]:
    """Extract Go files to edit from a git diff patch string."""
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            fpath = line[6:]
            if fpath.endswith(GO_SOURCE_EXT) and not fpath.endswith(
                GO_TEST_FILE_SUFFIX
            ):
                full_path = os.path.join(local_repo, fpath)
                if os.path.exists(full_path):
                    files.append(full_path)
    return files


_CLI_GO_PATH = str(Path(__file__).resolve().parent.parent / "commit0" / "cli_go.py")


def get_go_lint_cmd(
    repo: str,
    commit0_config_file: str,
) -> str:
    return (
        f"python {_CLI_GO_PATH} lint {repo} --commit0-config-file {commit0_config_file}"
    )


def get_go_message(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: list[str],
    commit0_config_file: str = ".commit0.go.yaml",
) -> str:
    """Build the agent message/prompt for Go repos.

    Includes repository info, test info, lint info, and spec info
    based on agent_config settings.
    """
    parts: list[str] = []

    if GO_SYSTEM_PROMPT_PATH.exists():
        parts.append(GO_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip())
        parts.append("")

    parts.append(PROMPT_HEADER)
    if agent_config.use_user_prompt and agent_config.user_prompt:
        parts.append(agent_config.user_prompt)
    else:
        parts.append(
            "You need to complete the implementations for all stubbed functions "
            '(those containing the marker `"STUB: not implemented"`) and pass '
            "the unit tests.\n"
            "Do not change the names or signatures of existing functions.\n"
            "IMPORTANT: You must NEVER modify, edit, or delete any test files "
            "(files matching *_test.go). Test files are read-only and define "
            "the expected behavior."
        )
    parts.append("")

    if agent_config.use_repo_info:
        parts.append(REFERENCE_HEADER)
        parts.append(REPO_INFO_HEADER)
        repo_info = get_dir_info(
            repo_path,
            max_length=agent_config.max_repo_info_length,
            show_stubs=True,
        )
        parts.append(repo_info)
        parts.append("")

    if agent_config.use_unit_tests_info and test_files:
        parts.append(TEST_INFO_HEADER)
        test_info_parts: list[str] = []
        total_len = 0
        for tf in test_files:
            rel = os.path.relpath(tf, repo_path)
            try:
                with open(tf, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                entry = f"### {rel}\n```go\n{content}\n```\n"
                if total_len + len(entry) > agent_config.max_unit_tests_info_length:
                    test_info_parts.append("... (truncated)")
                    break
                test_info_parts.append(entry)
                total_len += len(entry)
            except OSError:
                continue
        parts.append("\n".join(test_info_parts))
        parts.append("")

    if agent_config.use_lint_info and commit0_config_file:
        parts.append(LINT_INFO_HEADER)
        repo_name = os.path.basename(repo_path)
        lint_cmd = get_go_lint_cmd(repo_name, commit0_config_file)
        try:
            result = subprocess.run(
                lint_cmd.split(),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=repo_path,
            )
            lint_output = (result.stdout + result.stderr).strip()
            if len(lint_output) > agent_config.max_lint_info_length:
                lint_output = (
                    lint_output[: agent_config.max_lint_info_length]
                    + "\n... (truncated)"
                )
            parts.append(f"```\n{lint_output}\n```")
        except (subprocess.TimeoutExpired, OSError):
            parts.append("(lint results unavailable)")
        parts.append("")

    return "\n".join(parts)


def create_branch(repo: git.Repo, branch: str, override: bool = False) -> None:
    """Create or checkout a branch for the agent to work on."""
    if branch in repo.heads:
        if override:
            repo.git.checkout(branch)
            repo.git.reset("--hard", "HEAD~0")
        else:
            repo.git.checkout(branch)
    else:
        repo.git.checkout("-b", branch)


def get_changed_files(repo: git.Repo, branch: str) -> list[str]:
    """Get list of Go files changed on the given branch vs its merge-base."""
    try:
        merge_base = repo.git.merge_base(branch, "HEAD")
        diff_output = repo.git.diff("--name-only", merge_base, branch)
        return [
            f
            for f in diff_output.splitlines()
            if f.endswith(GO_SOURCE_EXT) and not f.endswith(GO_TEST_FILE_SUFFIX)
        ]
    except git.GitCommandError:
        return []


def write_agent_config(config_file: str, config: dict) -> None:
    """Write agent config to YAML file."""
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False)
    logger.info(f"Agent config written to {config_file}")


def read_yaml_config(config_file: str) -> dict:
    """Read YAML config file."""
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_agent_config(config_file: str) -> AgentConfig:
    """Load and validate agent config from YAML file."""
    raw = read_yaml_config(config_file)
    return AgentConfig(**raw)
