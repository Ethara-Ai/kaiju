"""
Prepare repos for a commit0 dataset.

For each validated candidate:
1. Fork to Ethara-Ai GitHub org
2. Create a 'commit0_combined' branch
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
from tools.stub import StubTransformer, is_test_file


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


def full_clone(full_name: str, clone_dir: Path, branch: str | None = None) -> Path:
    """Full clone (not shallow) of a repo. Returns repo dir."""
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        # Make sure it's a full clone, not shallow
        shallow_file = repo_dir / ".git" / "shallow"
        if shallow_file.exists():
            logger.info("  Unshallowing existing clone...")
            git(repo_dir, "fetch", "--unshallow", check=False, timeout=300)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    cmd = ["git", "clone", url, str(repo_dir)]
    if branch:
        cmd = ["git", "clone", "--branch", branch, url, str(repo_dir)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError:
        # Retry without branch
        if branch and repo_dir.exists():
            shutil.rmtree(repo_dir)
        cmd = ["git", "clone", url, str(repo_dir)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)

    return repo_dir


# ─── Stub & Commit ───────────────────────────────────────────────────────────


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str | None,
    branch_name: str = "commit0_combined",
) -> tuple[str, str]:
    """
    Create the commit0 branch with stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_combined'
    3. Run stub.py on source files
    4. Commit stubbed version as base_commit
    """
    default_branch = get_default_branch(repo_dir)
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    # Ensure we're on the default branch
    git(repo_dir, "checkout", default_branch)

    # Create or reset the commit0 branch
    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    # Determine which directory to stub
    if src_dir:
        stub_target = repo_dir / src_dir
    else:
        stub_target = repo_dir

    if not stub_target.is_dir():
        raise ValueError(f"src_dir does not exist: {stub_target}")

    # Run stubbing
    logger.info("  Stubbing source in: %s", stub_target.relative_to(repo_dir))
    stubber = StubTransformer(keep_docstrings=True)

    stubbed_count = 0
    errors = 0

    for py_file in sorted(stub_target.rglob("*.py")):
        rel = py_file.relative_to(repo_dir)

        # Skip test files
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

    # Commit the stubbed version
    git(repo_dir, "add", "-A")

    # Check if there are changes
    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
        git(
            repo_dir,
            "commit",
            "-m",
            f"commit0: stub function bodies for {full_name.split('/')[-1]}\n\n"
            f"Automated stubbing: {stubbed_count} files modified\n"
            f"All function/method bodies replaced with `pass`\n"
            f"Reference implementation: {reference_commit[:12]}",
        )
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])

    return base_commit, reference_commit


# ─── Setup/Test Dict Generation ──────────────────────────────────────────────


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

        setup["pip_packages"] = ["pytest", "pytest-json-report"]

    elif setup_py.exists():
        setup["install"] = 'pip install -e "."'
        setup["pip_packages"] = ["pytest", "pytest-json-report"]

    else:
        # Requirements files
        req_files = []
        for f in ["requirements.txt", "requirements-dev.txt", "requirements-test.txt"]:
            if (repo_dir / f).exists():
                req_files.append(f)
        if req_files:
            setup["install"] = " && ".join(f"pip install -r {f}" for f in req_files)
        setup["pip_packages"] = ["pytest", "pytest-json-report"]

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
    """Extract minimum Python version as X.Y string."""
    for config_name in ["pyproject.toml", "setup.cfg", "setup.py"]:
        config = repo_dir / config_name
        if not config.exists():
            continue
        content = config.read_text(errors="replace")

        # Match patterns like >=3.8, >=3.9, etc.
        m = re.search(
            r'(?:requires-python|python_requires)\s*=\s*["\']?>=?\s*(\d+\.\d+)', content
        )
        if m:
            return m.group(1)

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
) -> dict:
    """Create a RepoInstance-compatible dataset entry."""
    repo_name = full_name.split("/")[-1]
    fork_repo_name = fork_name.split("/")[-1]

    return {
        "instance_id": f"commit-0/{repo_name}",
        "repo": fork_repo_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": setup_dict,
        "test": test_dict,
        "src_dir": src_dir or "",
    }


# ─── Push to Fork ────────────────────────────────────────────────────────────


def push_to_fork(
    repo_dir: Path,
    fork_name: str,
    branch: str = "commit0_combined",
    token: str | None = None,
) -> None:
    """Add fork as remote and push the commit0 branch."""
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
) -> list[dict]:
    """Prepare repos for the dataset."""
    token = os.environ.get("GITHUB_TOKEN")
    entries: list[dict] = []

    for i, candidate in enumerate(candidates):
        if max_repos and i >= max_repos:
            break

        # Skip failed candidates
        if candidate.get("status") == "fail":
            logger.info("Skipping failed candidate: %s", candidate["full_name"])
            continue

        full_name = candidate["full_name"]
        analysis = candidate.get("analysis", {})
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

        # Full clone
        try:
            repo_dir = full_clone(full_name, clone_dir)
        except Exception as e:
            logger.error("  Clone failed: %s", e)
            continue

        # Create stubbed branch
        try:
            base_commit, reference_commit = create_stubbed_branch(
                repo_dir,
                full_name,
                src_dir,
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

        # Push to fork
        if not dry_run:
            try:
                # Switch to the stub branch for pushing
                git(repo_dir, "checkout", "commit0_combined")
                push_to_fork(repo_dir, fork_name, token=token)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                # Continue anyway — entry is still useful for local work

        # Create dataset entry
        entry = create_dataset_entry(
            full_name=full_name,
            fork_name=fork_name,
            base_commit=base_commit,
            reference_commit=reference_commit,
            src_dir=src_dir or "",
            setup_dict=setup_dict,
            test_dict=test_dict,
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
                "analysis": None,  # Will be detected during prepare
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
    )

    # Save entries
    output_path = Path(args.output)
    output_path.write_text(json.dumps(entries, indent=2))
    logger.info("Saved %d entries to %s", len(entries), output_path)

    print_entries_summary(entries)


if __name__ == "__main__":
    main()
