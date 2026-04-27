import logging
import subprocess
from pathlib import Path
from typing import Optional

from commit0.harness.constants_java import JAVA_BASE_BRANCH

logger = logging.getLogger(__name__)


def generate_java_patch(
    repo_dir: str,
    base_branch: str = JAVA_BASE_BRANCH,
    include_only_java: bool = True,
) -> Optional[str]:
    p = Path(repo_dir)
    if not (p / ".git").exists():
        logger.error(f"Not a git repository: {repo_dir}")
        return None

    cmd = ["git", "diff", base_branch]
    if include_only_java:
        cmd.extend(["--", "*.java"])

    result = subprocess.run(cmd, cwd=p, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"git diff failed: {result.stderr}")
        return None

    return result.stdout if result.stdout.strip() else None


def apply_java_patch(
    repo_dir: str,
    patch_content: str,
) -> bool:
    p = Path(repo_dir)
    try:
        result = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=patch_content,
            cwd=p,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Patch check failed: {result.stderr}")
            return False

        result = subprocess.run(
            ["git", "apply", "-"],
            input=patch_content,
            cwd=p,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Failed to apply patch: {e}")
        return False


def filter_patch_java_only(patch_content: str) -> str:
    lines = patch_content.split("\n")
    filtered = []
    include_hunk = False

    for line in lines:
        if line.startswith("diff --git"):
            include_hunk = line.endswith(".java") or ".java " in line
        if include_hunk:
            filtered.append(line)

    return "\n".join(filtered)
