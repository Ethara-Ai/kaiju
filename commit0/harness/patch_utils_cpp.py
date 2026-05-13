"""C++-specific patch handling utilities.

Wraps the generic patch generation from utils.py with C++-specific
filtering: excludes ``build/``, ``cmake-build-*/``, ``builddir/``,
and ``.cache/`` build artifacts from patches.
"""

import re

import git

from commit0.harness.utils import generate_patch_between_commits

_BUILD_DIR_PATTERN = re.compile(r"\s[ab]/(build/|cmake-build-[^/]*/|builddir/|\.cache/)")


def generate_cpp_patch(
    repo_dir: str, base_commit: str, target_commit: str
) -> str:
    """Generate a patch between two commits with C++-specific filtering.

    Calls :func:`generate_patch_between_commits` and post-processes the
    result to strip ``build/``, ``cmake-build-*/``, ``builddir/``, and
    ``.cache/`` directory changes.

    Parameters
    ----------
    repo_dir : str
        Path to the local git repository.
    base_commit : str
        The old commit hash or reference.
    target_commit : str
        The new commit hash or reference.

    Returns
    -------
    str
        Filtered patch string.

    """
    repo = git.Repo(repo_dir)
    raw_patch = generate_patch_between_commits(repo, base_commit, target_commit)
    return _filter_build_dirs(raw_patch)


def validate_cpp_patch(patch_content: str) -> bool:
    """Validate that a patch does not contain build directory artifacts.

    Checks
    ------
    * No diff headers referencing paths under ``build/``, ``cmake-build-*/``,
      ``builddir/``, or ``.cache/``.
    * No binary blob markers from build artifacts inside those directories.

    Parameters
    ----------
    patch_content : str
        The unified-diff patch text to validate.

    Returns
    -------
    bool
        ``True`` if the patch is clean, ``False`` otherwise.

    """
    for line in patch_content.splitlines():
        if line.startswith("diff --git") and _BUILD_DIR_PATTERN.search(line):
            return False
        if re.match(r"^(\+\+\+|---)\s+(a|b)/(build/|cmake-build-[^/]*/|builddir/|\.cache/)", line):
            return False
        if line.startswith("Binary files") and _BUILD_DIR_PATTERN.search(line):
            return False
    return True


def _filter_build_dirs(patch: str) -> str:
    """Remove hunks that belong to build artifact directories.

    Splits the patch into per-file sections (delimited by
    ``diff --git ...`` lines) and drops any section whose path falls
    under ``build/``, ``cmake-build-*/``, ``builddir/``, or ``.cache/``.
    """
    if not patch.strip():
        return patch

    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)

    kept: list[str] = []
    for section in sections:
        if not section.strip():
            continue
        if _section_is_build_dir(section):
            continue
        kept.append(section)

    return "".join(kept) if kept else "\n\n"


def _section_is_build_dir(section: str) -> bool:
    """Return True if *section* modifies a path under a build directory."""
    first_line = section.split("\n", 1)[0]
    if not first_line.startswith("diff --git"):
        return False
    return bool(_BUILD_DIR_PATTERN.search(first_line))
