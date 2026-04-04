"""
Prepare repos for a commit0 dataset.

For each validated candidate:
1. Fork to Ethara-Ai GitHub org
2. Create a 'commit0_all' branch
3. Apply AST stubbing (replace function bodies with pass)
4. Commit stubbed version as base_commit
5. Reset to original as reference_commit
6. Generate setup/test dict entries
7. Output dataset entries (RepoInstance-compatible)

Usage:
    # From validated.json (output of validate.py):
    python -m tools.prepare_repo validated.json --output dataset_entries.json

    # Single repo:
    python -m tools.prepare_repo --repo pallets/flask --clone-dir ./repos_staging --output dataset_entries.json

    # Dry run (no GitHub fork, no push):
    python -m tools.prepare_repo validated.json --dry-run --output dataset_entries.json

Requires:
    - GITHUB_TOKEN env var with repo/fork permissions
    - gh CLI installed (for forking)
    - stub.py working (imported as module)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# GitHub org to fork repos into
DEFAULT_ORG = "Ethara-Ai"

# Import stub module
TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR.parent))
from tools.stub import StubTransformer, is_test_file, collect_import_time_names

# Lazy import for spec scraping (optional dependency)
_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync


# ─── Git Helpers ──────────────────────────────────────────────────────────────


def git(repo_dir: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    """Run a git command in repo_dir, return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
    return result.stdout.strip()


def get_head_sha(repo_dir: Path) -> str:
    """Get current HEAD commit SHA."""
    return git(repo_dir, "rev-parse", "HEAD")


def get_default_branch(repo_dir: Path) -> str:
    """Get the default branch name."""
    try:
        ref = git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.split("/")[-1]
    except subprocess.CalledProcessError:
        # Fallback: check common names
        for branch in ["main", "master"]:
            try:
                git(repo_dir, "rev-parse", f"refs/remotes/origin/{branch}")
                return branch
            except subprocess.CalledProcessError:
                continue
        return "main"


# ─── Fork & Clone ────────────────────────────────────────────────────────────


def fork_repo(full_name: str, org: str, token: str | None = None) -> str:
    """Fork a repo to the target org using gh CLI. Returns fork full_name."""
    fork_name = f"{org}/{full_name.split('/')[-1]}"

    # Check if fork already exists
    try:
        result = subprocess.run(
            ["gh", "repo", "view", fork_name, "--json", "name"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("  Fork already exists: %s", fork_name)
            return fork_name
    except Exception:
        pass

    # Create fork
    logger.info("  Forking %s to %s...", full_name, org)
    subprocess.run(
        ["gh", "repo", "fork", full_name, "--org", org, "--clone=false"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    # Wait for fork to be available
    for _ in range(10):
        try:
            result = subprocess.run(
                ["gh", "repo", "view", fork_name, "--json", "name"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info("  Fork ready: %s", fork_name)
                return fork_name
        except Exception:
            pass
        time.sleep(2)

    raise RuntimeError(f"Fork {fork_name} not available after 20s")


def full_clone(
    full_name: str, clone_dir: Path, branch: str | None = None, tag: str | None = None
) -> Path:
    """Full clone (not shallow) of a repo. Returns repo dir."""
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        shallow_file = repo_dir / ".git" / "shallow"
        if shallow_file.exists():
            logger.info("  Unshallowing existing clone...")
            git(repo_dir, "fetch", "--unshallow", check=False, timeout=300)
        if tag:
            git(repo_dir, "fetch", "--tags", timeout=120)
            git(repo_dir, "checkout", tag, check=False)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    ref = tag or branch
    cmd = ["git", "clone", url, str(repo_dir)]
    if ref:
        cmd = ["git", "clone", "--branch", ref, url, str(repo_dir)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError:
        if ref and repo_dir.exists():
            shutil.rmtree(repo_dir)
        cmd = ["git", "clone", url, str(repo_dir)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
        if tag:
            git(repo_dir, "checkout", tag, check=False)

    return repo_dir


# ─── Stub & Commit ───────────────────────────────────────────────────────────


def _dir_exists_exact(parent: Path, name: str) -> bool:
    """Check if a directory exists with exact case (even on case-insensitive macOS)."""
    if not (parent / name).is_dir():
        return False
    try:
        return name in os.listdir(parent)
    except OSError:
        return False


def detect_src_dir(repo_dir: Path, full_name: str) -> str:
    """Auto-detect the source directory within a repo."""
    package_name = full_name.split("/")[-1].replace("-", "_")

    # 1. Check src/{package_name}/ layout (exact case)
    src_parent = repo_dir / "src"
    if _dir_exists_exact(src_parent, package_name):
        return f"src/{package_name}"

    # 2. Check src/{package_name}/ with lowercase
    if package_name != package_name.lower() and _dir_exists_exact(
        src_parent, package_name.lower()
    ):
        return f"src/{package_name.lower()}"

    # 3. Flat layout: {package_name}/ at repo root (must contain __init__.py)
    if (
        _dir_exists_exact(repo_dir, package_name)
        and (repo_dir / package_name / "__init__.py").exists()
    ):
        return package_name

    if (
        package_name != package_name.lower()
        and _dir_exists_exact(repo_dir, package_name.lower())
        and (repo_dir / package_name.lower() / "__init__.py").exists()
    ):
        return package_name.lower()

    # 4. Fallback: scan for directories with __init__.py that aren't test dirs
    test_names = {"test", "tests", "testing", "test_utils", "conftest"}
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if child.name.lower() in test_names:
            continue
        if (child / "__init__.py").exists():
            return child.name

    # 5. Check inside src/ for any package
    src_dir = repo_dir / "src"
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            if (child / "__init__.py").exists():
                return f"src/{child.name}"

    return ""


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str | None,
    branch_name: str | None = None,
    removal_mode: str = "combined",
) -> tuple[str, str]:
    """
    Create the commit0 branch with stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_{removal_mode}'
    3. Run stub.py on source files
    4. Commit stubbed version as base_commit
    """
    if branch_name is None:
        branch_name = "commit0_all"
    default_branch = get_default_branch(repo_dir)
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    git(repo_dir, "checkout", default_branch)

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    if src_dir:
        stub_target = repo_dir / src_dir
    else:
        stub_target = repo_dir

    if not stub_target.is_dir():
        raise ValueError(f"src_dir does not exist: {stub_target}")

    logger.info(
        "  Stubbing source in: %s (mode=%s)",
        stub_target.relative_to(repo_dir),
        removal_mode,
    )

    extra_scan_dirs: list[Path] = []
    test_dir_names = {"test", "tests", "testing"}
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == stub_target:
            continue
        if child.name.lower() in test_dir_names:
            extra_scan_dirs.append(child)
            continue
        if (child / "__init__.py").exists():
            extra_scan_dirs.append(child)
    src_parent = repo_dir / "src"
    if src_parent.is_dir() and stub_target.parent == src_parent:
        for child in sorted(src_parent.iterdir()):
            if not child.is_dir() or child == stub_target:
                continue
            if (child / "__init__.py").exists():
                extra_scan_dirs.append(child)
    if extra_scan_dirs:
        logger.info(
            "  Scanning %d extra dirs: %s",
            len(extra_scan_dirs),
            [d.name for d in extra_scan_dirs],
        )

    import_time_names = collect_import_time_names(
        stub_target, extra_scan_dirs=extra_scan_dirs
    )
    if import_time_names:
        logger.info(
            "  Preserving %d import-time functions: %s",
            len(import_time_names),
            ", ".join(sorted(import_time_names)[:15]),
        )
    stubber = StubTransformer(
        keep_docstrings=True,
        removal_mode=removal_mode,
        import_time_names=import_time_names,
    )

    stubbed_count = 0
    removed_count = 0
    errors = 0

    for py_file in sorted(stub_target.rglob("*.py")):
        rel = py_file.relative_to(repo_dir)

        if is_test_file(py_file):
            continue

        try:
            original = py_file.read_text(errors="replace")
            result = stubber.transform_source(original, str(rel))

            if result is not None and result != original:
                py_file.write_text(result)
                stubbed_count += 1
        except Exception as e:
            logger.warning("  Error stubbing %s: %s", rel, e)
            errors += 1

    logger.info("  Stubbed %d files (%d errors)", stubbed_count, errors)

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
        # Verify that stubbing actually modified code (should have both + and - lines)
        diff_output = git(repo_dir, "diff", "--cached", "--stat")
        diff_patch = git(repo_dir, "diff", "--cached")
        additions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        logger.info(
            "  Diff stats — lines added: %d, lines removed: %d", additions, deletions
        )
        if additions == 0 or deletions == 0:
            raise RuntimeError(
                f"Stubbing verification failed for {full_name}: "
                f"additions={additions}, deletions={deletions}. "
                f"Expected both >0 (stubbing should replace code with pass)."
            )

        git(
            repo_dir,
            "commit",
            "-m",
            "Commit 0",
        )
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])

    return base_commit, reference_commit


def quick_import_check(repo_dir: Path, src_dir: str) -> tuple[bool, str]:
    """Check if stubbed code can be imported without errors.

    Returns (success, error_message).
    """
    # Derive package name from src_dir
    # src_dir could be "src/package_name" or "package_name"
    parts = src_dir.split("/")
    package_name = parts[-1]

    # Some packages use hyphens in dir names but underscores in imports
    import_name = package_name.replace("-", "_")

    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {import_name}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode == 0:
            return True, ""
        error = (
            result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
        )
        return False, error
    except subprocess.TimeoutExpired:
        return False, "import timed out after 30s"
    except Exception as e:
        return False, str(e)


# ─── Setup/Test Dict Generation ──────────────────────────────────────────────


def extract_test_dependencies(repo_dir: Path) -> list[str]:
    """Extract test dependencies from project config files."""
    base_deps = {"pytest", "pytest-json-report"}
    found_deps: dict[str, str] = {}
    for dep in base_deps:
        found_deps[dep] = dep

    test_group_names = {"test", "testing", "tests", "dev"}

    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)

            optional_deps = data.get("project", {}).get("optional-dependencies", {})
            for group_name in test_group_names:
                for dep_str in optional_deps.get(group_name, []):
                    name = re.split(r"[><=!~;\s\[]", dep_str.strip())[0].strip()
                    if name and name.lower() not in found_deps:
                        found_deps[name.lower()] = dep_str.strip()

            dep_groups = data.get("dependency-groups", {})
            for group_name in test_group_names:
                for dep_entry in dep_groups.get(group_name, []):
                    if isinstance(dep_entry, str):
                        name = re.split(r"[><=!~;\s\[]", dep_entry.strip())[0].strip()
                        if name and name.lower() not in found_deps:
                            found_deps[name.lower()] = dep_entry.strip()
        except Exception as e:
            logger.debug("  Could not parse pyproject.toml for deps: %s", e)

    for req_filename in [
        "requirements-test.txt",
        "requirements-tests.txt",
        "requirements-dev.txt",
    ]:
        req_file = repo_dir / req_filename
        if req_file.exists():
            try:
                for line in req_file.read_text(errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    name = re.split(r"[><=!~;\s\[]", line)[0].strip()
                    if name and name.lower() not in found_deps:
                        found_deps[name.lower()] = line
            except Exception:
                pass

    for dep in base_deps:
        if dep not in found_deps:
            found_deps[dep] = dep

    return sorted(found_deps.values(), key=lambda s: s.lower())


def generate_setup_dict(repo_dir: Path, full_name: str) -> dict:
    """
    Generate the 'setup' dict for a RepoInstance.

    Inspects pyproject.toml/setup.py/setup.cfg for install instructions.
    """
    setup: dict = {
        "install": "",
        "packages": "",
        "pip_packages": [],
        "pre_install": [],
        "python": "3.12",
        "specification": "",
    }

    repo_name = full_name.split("/")[-1]

    # Detect Python version
    python_ver = _detect_python_version(repo_dir)
    if python_ver:
        setup["python"] = python_ver

    # Detect install method
    pyproject = repo_dir / "pyproject.toml"
    setup_py = repo_dir / "setup.py"

    if pyproject.exists():
        content = pyproject.read_text(errors="replace")

        # Detect extras
        extras = []
        for name in ["test", "testing", "tests", "dev", "develop", "all"]:
            if re.search(rf"\b{name}\b\s*=\s*\[", content):
                extras.append(name)

        if extras:
            # Prefer test extras over dev (less bloat)
            test_extras = [e for e in extras if e in ("test", "testing", "tests")]
            if test_extras:
                setup["install"] = f'pip install -e ".[{",".join(test_extras)}]"'
            else:
                setup["install"] = f'pip install -e ".[{extras[0]}]"'
        else:
            setup["install"] = 'pip install -e "."'

        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    elif setup_py.exists():
        setup["install"] = 'pip install -e "."'
        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    else:
        # Requirements files
        req_files = []
        for f in [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements-tests.txt",
        ]:
            if (repo_dir / f).exists():
                req_files.append(f)
        if req_files:
            setup["install"] = " && ".join(f"pip install -r {f}" for f in req_files)
        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    # Check for system deps (common patterns)
    pre_install = []
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        # Common C-extension deps
        if any(pkg in content for pkg in ["cython", "numpy", "scipy"]):
            pre_install.append("apt-get update && apt-get install -y build-essential")
        if "lxml" in content:
            pre_install.append(
                "apt-get update && apt-get install -y libxml2-dev libxslt1-dev"
            )
    setup["pre_install"] = pre_install

    # Documentation URL
    homepage = _find_docs_url(repo_dir, full_name)
    if homepage:
        setup["specification"] = homepage

    return setup


def generate_test_dict(repo_dir: Path, test_dir: str | None) -> dict:
    """Generate the 'test' dict for a RepoInstance."""
    test = {
        "test_cmd": "pytest",
        "test_dir": test_dir or "tests",
    }

    # Check for custom pytest config
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        # Look for testpaths
        m = re.search(r"testpaths\s*=\s*\[([^\]]+)\]", content)
        if m:
            paths = re.findall(r'"([^"]+)"', m.group(1))
            if paths:
                test["test_dir"] = paths[0]

    # Check for pytest.ini or setup.cfg with [tool:pytest]
    for cfg_name in ["pytest.ini", "setup.cfg"]:
        cfg = repo_dir / cfg_name
        if cfg.exists():
            content = cfg.read_text(errors="replace")
            m = re.search(r"testpaths\s*=\s*(.+)", content)
            if m:
                test["test_dir"] = m.group(1).strip().split()[0]
                break

    return test


def _detect_python_version(repo_dir: Path) -> str | None:
    """Extract minimum Python version as X.Y string.

    Clamps to versions available in the Docker base image (3.10, 3.12).
    """
    AVAILABLE_VERSIONS = [(3, 10), (3, 12)]

    for config_name in ["pyproject.toml", "setup.cfg", "setup.py"]:
        config = repo_dir / config_name
        if not config.exists():
            continue
        content = config.read_text(errors="replace")

        m = re.search(
            r'(?:requires-python|python_requires)\s*=\s*["\']?>=?\s*(\d+\.\d+)', content
        )
        if m:
            raw = m.group(1)
            parts = raw.split(".")
            major, minor = int(parts[0]), int(parts[1])
            for av_major, av_minor in AVAILABLE_VERSIONS:
                if (av_major, av_minor) >= (major, minor):
                    return f"{av_major}.{av_minor}"
            return f"{AVAILABLE_VERSIONS[-1][0]}.{AVAILABLE_VERSIONS[-1][1]}"

    return None


def _find_docs_url(repo_dir: Path, full_name: str) -> str:
    """Try to find the documentation URL."""
    # Check pyproject.toml for URLs
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        for pattern in [
            r'[Dd]ocumentation\s*=\s*["\']([^"\']+)["\']',
            r'[Hh]omepage\s*=\s*["\']([^"\']+)["\']',
        ]:
            m = re.search(pattern, content)
            if m:
                return m.group(1)

    # Fallback to readthedocs
    repo_name = full_name.split("/")[-1]
    return f"https://{repo_name}.readthedocs.io/"


# ─── Dataset Entry ────────────────────────────────────────────────────────────


def create_dataset_entry(
    full_name: str,
    fork_name: str,
    base_commit: str,
    reference_commit: str,
    src_dir: str,
    setup_dict: dict,
    test_dict: dict,
    pinned_tag: str | None = None,
) -> dict:
    repo_name = full_name.split("/")[-1]

    entry = {
        "instance_id": f"commit-0/{repo_name}",
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": setup_dict,
        "test": test_dict,
        "src_dir": src_dir or "",
    }
    if pinned_tag:
        entry["pinned_tag"] = pinned_tag
    return entry


# ─── Push to Fork ────────────────────────────────────────────────────────────


def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/branches/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        branch_data = json.loads(result.stdout)
        sha = branch_data["commit"]["sha"]

        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/commits/{sha}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        commit_data = json.loads(result.stdout)
        parent_sha = commit_data["parents"][0]["sha"]

        return (sha, parent_sha)
    except Exception:
        return None


def push_to_fork(
    repo_dir: Path,
    fork_name: str,
    branch: str | None = None,
    removal_mode: str = "combined",
    token: str | None = None,
) -> None:
    """Add fork as remote and push the commit0 branch."""
    if branch is None:
        branch = "commit0_all"
    # Add fork as remote
    if token:
        fork_url = f"https://x-access-token:{token}@github.com/{fork_name}.git"
    else:
        fork_url = f"https://github.com/{fork_name}.git"

    try:
        git(repo_dir, "remote", "remove", "fork", check=False)
    except Exception:
        pass
    git(repo_dir, "remote", "add", "fork", fork_url)

    # Push branch
    logger.info("  Pushing %s to %s...", branch, fork_name)
    git(repo_dir, "push", "-f", "fork", branch, timeout=300)


# ─── Main ────────────────────────────────────────────────────────────────────


def prepare_repos(
    candidates: list[dict],
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    max_repos: int | None = None,
    removal_mode: str = "combined",
    specs_dir: str = "./specs",
) -> list[dict]:
    """Prepare repos for the dataset."""
    token = os.environ.get("GITHUB_TOKEN")
    entries: list[dict] = []

    _get_scrape_func()

    for i, candidate in enumerate(candidates):
        if max_repos and i >= max_repos:
            break

        # Skip candidates that didn't pass validation
        status = candidate.get("status", "")
        if status in ("fail", "clone_failed", "analysis_failed", "pending"):
            logger.info(
                "Skipping candidate %s (status=%s)", candidate["full_name"], status
            )
            continue

        full_name = candidate["full_name"]
        analysis = candidate.get("analysis") or {}
        src_dir = analysis.get("src_dir")
        test_dir = analysis.get("test_dir")

        logger.info(
            "\n[%d/%d] Preparing %s...",
            i + 1,
            min(len(candidates), max_repos or len(candidates)),
            full_name,
        )

        # Fork
        if dry_run:
            fork_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", fork_name)
        else:
            try:
                fork_name = fork_repo(full_name, org, token=token)
            except Exception as e:
                logger.error("  Fork failed: %s", e)
                continue

        # Full clone (pinned to release tag if available)
        release_tag = candidate.get("release_tag") or analysis.get("release_tag")
        try:
            repo_dir = full_clone(full_name, clone_dir, tag=release_tag)
            if release_tag:
                logger.info("  Pinned to tag: %s", release_tag)
        except Exception as e:
            logger.error("  Clone failed: %s", e)
            continue

        src_dir = candidate.get("src_dir_override") or src_dir
        if not src_dir:
            src_dir = detect_src_dir(repo_dir, full_name)
            if src_dir:
                logger.info("  Auto-detected src_dir: %s", src_dir)

        if not src_dir:
            logger.error(
                "  FATAL: src_dir is empty for %s. "
                "Cannot determine source directory. Use --src-dir to specify manually.",
                full_name,
            )
            continue

        # Create stubbed branch
        try:
            base_commit, reference_commit = create_stubbed_branch(
                repo_dir,
                full_name,
                src_dir,
                removal_mode=removal_mode,
            )
        except Exception as e:
            logger.error("  Stubbing failed: %s", e)
            continue

        # Generate setup/test dicts
        # Switch back to original for accurate analysis
        default_branch = get_default_branch(repo_dir)
        git(repo_dir, "checkout", default_branch)

        setup_dict = generate_setup_dict(repo_dir, full_name)
        test_dict = generate_test_dict(repo_dir, test_dir)

        # Scrape spec PDF and commit into repo
        spec_path = None
        if setup_dict.get("specification"):
            repo_name = full_name.split("/")[-1]
            docs_url = setup_dict["specification"]
            logger.info("  Scraping spec from: %s", docs_url)
            try:
                scrape_fn = _get_scrape_func()
                spec_path = scrape_fn(
                    base_url=docs_url,
                    name=repo_name,
                    output_dir=specs_dir,
                    compress=True,
                )
                if spec_path:
                    logger.info("  Spec saved: %s", spec_path)
                    branch_name = "commit0_all"
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(spec_path, dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_name}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info("  Updated base_commit with spec: %s", base_commit[:12])
                else:
                    logger.warning("  Spec scraping returned no output")
            except Exception as e:
                logger.warning("  Spec scraping failed: %s", e)

        # Push to fork
        if not dry_run:
            branch_name = "commit0_all"
            try:
                git(repo_dir, "checkout", branch_name)
                push_to_fork(repo_dir, fork_name, branch=branch_name, token=token)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                remote_commits = resolve_commits_from_remote(fork_name, branch_name)
                if remote_commits:
                    base_commit, reference_commit = remote_commits
                    logger.info(
                        "  Resolved commits from remote: base=%s, ref=%s",
                        base_commit[:12],
                        reference_commit[:12],
                    )
                else:
                    logger.warning(
                        "  No remote branch found — using local commits only"
                    )

        # Create dataset entry
        entry = create_dataset_entry(
            full_name=full_name,
            fork_name=fork_name,
            base_commit=base_commit,
            reference_commit=reference_commit,
            src_dir=src_dir or "",
            setup_dict=setup_dict,
            test_dict=test_dict,
            pinned_tag=release_tag,
        )

        logger.info("  Entry created: instance_id=%s", entry["instance_id"])
        logger.info(
            "  base_commit=%s, reference_commit=%s",
            base_commit[:12],
            reference_commit[:12],
        )
        entries.append(entry)

    return entries


def print_entries_summary(entries: list[dict]) -> None:
    """Print summary of prepared dataset entries."""
    print(f"\n{'=' * 90}")
    print(f"PREPARED ENTRIES: {len(entries)}")
    print(f"{'=' * 90}\n")

    print(
        f"{'#':>3}  {'instance_id':<35} {'original_repo':<35} {'python':>7} {'base_commit':>12}"
    )
    print("-" * 100)

    for i, e in enumerate(entries, 1):
        print(
            f"{i:>3}  {e['instance_id']:<35} {e['original_repo']:<35} "
            f"{e['setup'].get('python', '?'):>7} {e['base_commit'][:12]:>12}"
        )

    print(f"\n{'=' * 90}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare repos for commit0 dataset")
    parser.add_argument(
        "validated_file",
        nargs="?",
        help="Input validated.json from validate.py",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Prepare a single repo (e.g., pallets/flask)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="dataset_entries.json",
        help="Output JSON file (default: dataset_entries.json)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default="./repos_staging",
        help="Directory to clone repos into (default: ./repos_staging)",
    )
    parser.add_argument(
        "--org",
        type=str,
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip forking and pushing (just clone, stub, generate entries)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to prepare",
    )
    parser.add_argument(
        "--removal-mode",
        type=str,
        choices=["all", "docstring", "combined"],
        default="combined",
        help="Stub removal mode: all (replace all bodies), docstring (only functions with docstrings), combined (stub documented + remove undocumented). Default: combined",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Pin to a specific git tag (overrides auto-detected release_tag)",
    )
    parser.add_argument(
        "--commit",
        type=str,
        default=None,
        help="Pin to a specific git commit SHA",
    )
    parser.add_argument(
        "--src-dir",
        type=str,
        default=None,
        help="Source directory within repo (e.g., 'src/flask'). Auto-detected if omitted.",
    )

    args = parser.parse_args()

    # Load candidates
    if args.repo:
        candidates = [
            {
                "full_name": args.repo,
                "name": args.repo.split("/")[-1],
                "owner": args.repo.split("/")[0],
                "stars": 0,
                "default_branch": "main",
                "status": "pass",
                "analysis": None,
                "release_tag": args.tag or args.commit,
                "src_dir_override": args.src_dir,
            }
        ]
    elif args.validated_file:
        candidates = json.loads(Path(args.validated_file).read_text())
    else:
        parser.error("Provide either validated_file or --repo")
        return

    # If analysis is missing (e.g., --repo mode), do quick analysis
    for c in candidates:
        if c.get("analysis") is None and c.get("status") != "fail":
            c["status"] = "pass"
            # Analysis will happen during prepare using src_dir detection

    clone_dir = Path(args.clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)

    entries = prepare_repos(
        candidates,
        clone_dir=clone_dir,
        org=args.org,
        dry_run=args.dry_run,
        max_repos=args.max_repos,
        removal_mode=args.removal_mode,
        specs_dir=args.specs_dir,
    )

    # Save entries
    output_path = Path(args.output)
    output_path.write_text(json.dumps(entries, indent=2))
    logger.info("Saved %d entries to %s", len(entries), output_path)

    print_entries_summary(entries)


if __name__ == "__main__":
    main()
