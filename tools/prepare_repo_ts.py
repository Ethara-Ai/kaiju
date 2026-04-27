"""Prepare TypeScript repos for the commit0 dataset.

Mirrors tools/prepare_repo.py but for TypeScript:
1. Fork to GitHub org
2. Clone repo
3. Detect TS source directory
4. Run ts-morph stubbing via stub_ts_runner.py
5. Commit stubbed version
6. Push to fork

Reuses git helpers from tools.prepare_repo -- ZERO modifications to existing files.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from tools.prepare_repo import (
    git,
    full_clone,
    push_to_fork,
    get_head_sha,
    get_default_branch,
)
from tools.stub_ts_runner import run_stub_ts

DEFAULT_ORG = "Zahgon"

KNOWN_TEST_PACKAGES = {
    "jest",
    "@jest/globals",
    "ts-jest",
    "@types/jest",
    "vitest",
    "@vitest/coverage-v8",
    "mocha",
    "chai",
    "@types/mocha",
}

from commit0.harness.constants_ts import TS_DATASET_BRANCH


def _exec_prefix(pkg_manager: str) -> str:
    """Return the local-binary runner for the given package manager."""
    return {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(pkg_manager, "npx")


def fork_repo_ts(full_name: str, org: str, token: str | None = None) -> str:
    """Fork a repo to a target user or org. Handles both user and org accounts."""
    import time

    fork_name = f"{org}/{full_name.split('/')[-1]}"

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
    except Exception as e:
        logger.warning("  Fork existence check failed for %s: %s", fork_name, e)

    logger.info("  Forking %s to %s...", full_name, org)
    result = subprocess.run(
        ["gh", "repo", "fork", full_name, "--org", org, "--clone=false"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 and "login for a user account" in result.stderr:
        subprocess.run(
            ["gh", "repo", "fork", full_name, "--clone=false"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    elif result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )

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
        except Exception as e:
            logger.warning("  Fork poll failed for %s: %s", fork_name, e)
        time.sleep(2)

    raise RuntimeError(f"Fork {fork_name} not available after 20s")


def detect_ts_src_dir(repo_dir: Path) -> str:
    """Auto-detect the TypeScript source directory within a repo.

    Heuristics (in priority order):
    1. src/ directory containing .ts files
    2. lib/ directory containing .ts files
    3. Root directory if tsconfig.json exists and has .ts files at root
    4. First directory containing index.ts

    Returns
    -------
        Relative path from repo_dir (e.g. "src", "lib", "."), or empty string
        if no TypeScript source is found.

    """
    # Check for tsconfig.json first -- must exist for TS repos
    tsconfig = repo_dir / "tsconfig.json"
    if not tsconfig.exists():
        logger.warning("No tsconfig.json found in %s", repo_dir)
        # Some repos use tsconfig in a subdirectory, still check for .ts files
        pass

    # 1. src/ with .ts files
    src_dir = repo_dir / "src"
    if src_dir.is_dir() and list(src_dir.glob("**/*.ts")):
        return "src"

    # 2. lib/ with .ts files
    lib_dir = repo_dir / "lib"
    if lib_dir.is_dir() and list(lib_dir.glob("**/*.ts")):
        return "lib"

    # 3. Root with .ts files (flat layout)
    root_ts = [f for f in repo_dir.glob("*.ts") if not f.name.endswith(".d.ts")]
    if root_ts:
        return "."

    # 4. First directory with index.ts (packages/*/src pattern)
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        if (child / "index.ts").exists():
            return child.name

    return ""


def detect_ts_test_dirs(repo_dir: Path) -> list[Path]:
    """Find test directories containing TypeScript test files.

    Looks for directories matching common TS test patterns:
    __tests__/, test/, tests/ that contain .test.ts or .spec.ts files.

    Returns
    -------
        List of absolute Paths to test directories.

    """
    test_dirs: list[Path] = []
    test_dir_names = {"__tests__", "test", "tests"}

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.lower() in test_dir_names:
            ts_test_files = (
                list(child.rglob("*.test.ts"))
                + list(child.rglob("*.spec.ts"))
                + list(child.rglob("*.test.tsx"))
                + list(child.rglob("*.spec.tsx"))
                + list(child.rglob("*.test.js"))
                + list(child.rglob("*.spec.js"))
            )
            if ts_test_files:
                test_dirs.append(child)

    return test_dirs


def detect_package_manager(repo_dir: Path) -> str:
    """Detect the package manager from lockfiles.

    Returns: "npm" | "yarn" | "pnpm" | "bun"
    """
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_dir / "yarn.lock").exists():
        return "yarn"
    if (repo_dir / "bun.lockb").exists():
        return "bun"
    return "npm"


def detect_test_framework(repo_dir: Path) -> str:
    """Detect the test framework from package.json and config files.

    Priority: vitest > jest (vitest wins if both present).

    Returns: "jest" | "vitest"
    """
    pkg_path = repo_dir / "package.json"
    if not pkg_path.exists():
        logger.warning("No package.json found in %s, defaulting to jest", repo_dir)
        return "jest"

    try:
        pkg = json.loads(pkg_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Cannot parse package.json in %s, defaulting to jest", repo_dir)
        return "jest"

    dev_deps = pkg.get("devDependencies", {})
    deps = pkg.get("dependencies", {})
    all_deps = {**deps, **dev_deps}

    if "vitest" in all_deps:
        return "vitest"
    if "jest" in all_deps or "@jest/globals" in all_deps:
        return "jest"

    vitest_configs = ["vitest.config.ts", "vitest.config.js", "vitest.config.mts"]
    if any((repo_dir / c).exists() for c in vitest_configs):
        return "vitest"

    jest_configs = [
        "jest.config.ts",
        "jest.config.js",
        "jest.config.mjs",
        "jest.config.cjs",
    ]
    if any((repo_dir / c).exists() for c in jest_configs):
        return "jest"

    if "jest" in pkg:
        return "jest"

    test_script = pkg.get("scripts", {}).get("test", "")
    if "vitest" in test_script:
        return "vitest"
    if "jest" in test_script:
        return "jest"

    return "jest"


_BLOCKED_HOMEPAGE_DOMAINS = ("github.com", "gitlab.com", "npmjs.com", "npmjs.org")


def _detect_spec_url(repo_dir: Path) -> str:
    """Detect documentation URL from package.json homepage field.

    Falls back to npm registry metadata if local package.json has no homepage.
    Returns empty string if no usable URL found.
    """
    pkg_path = repo_dir / "package.json"
    pkg: dict = {}
    pkg_name = ""

    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
            pkg_name = pkg.get("name", "")
        except (json.JSONDecodeError, OSError):
            pass

    for field in ("homepage", "docs", "documentation"):
        val = pkg.get(field, "")
        if val and not any(d in val for d in _BLOCKED_HOMEPAGE_DOMAINS):
            return val

    if pkg_name:
        try:
            import urllib.request

            url = f"https://registry.npmjs.org/{pkg_name}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                npm_homepage = data.get("homepage", "")
                if npm_homepage and not any(
                    d in npm_homepage for d in _BLOCKED_HOMEPAGE_DOMAINS
                ):
                    return npm_homepage
        except Exception:
            pass

    if pkg_name:
        return f"https://www.skypack.dev/view/{pkg_name}"

    return ""


def generate_setup_dict_ts(repo_dir: Path) -> tuple[dict, dict, str]:
    """Build setup and test dicts for a TypeScript repo.

    Returns: (setup_dict, test_dict, test_framework)
    """
    test_framework = detect_test_framework(repo_dir)
    pkg_manager = detect_package_manager(repo_dir)
    install_cmd = f"{pkg_manager} install"

    packages: list[str] = []
    pkg_path = repo_dir / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
            dev_deps = pkg.get("devDependencies", {})
            packages = sorted(p for p in dev_deps if p in KNOWN_TEST_PACKAGES)
        except (json.JSONDecodeError, OSError):
            pass

    test_dirs = detect_ts_test_dirs(repo_dir)
    if test_dirs:
        test_dir = test_dirs[0].name
    else:
        test_dir = "__tests__"

    spec_url = _detect_spec_url(repo_dir)

    setup_dict = {
        "node": "20",
        "install": install_cmd,
        "packages": packages,
        "pre_install": [],
        "specification": spec_url,
    }

    prefix = _exec_prefix(pkg_manager)
    if test_framework == "vitest":
        test_cmd = f"{prefix} vitest run"
    else:
        test_cmd = f"{prefix} jest"

    test_dict = {
        "test_cmd": test_cmd,
        "test_dir": test_dir,
    }

    return setup_dict, test_dict, test_framework


def _collect_extra_scan_dirs(
    repo_dir: Path, src_dir_path: Path, test_dirs: list[Path]
) -> list[Path]:
    """Collect directories to scan for import-time names (not stubbed).

    Mirrors prepare_repo.py lines 313-337: scans sibling packages and test dirs.
    """
    extra: list[Path] = list(test_dirs)

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == src_dir_path or child.name == "node_modules":
            continue
        if child in extra:
            continue
        # Check if it has .ts files (sibling package)
        ts_files = list(child.glob("**/*.ts"))
        if ts_files:
            extra.append(child)

    return extra


def create_ts_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str,
    branch_name: str = TS_DATASET_BRANCH,
) -> tuple[str, str]:
    """Create the commit0 branch with stubbed TypeScript code.

    Mirrors create_stubbed_branch from prepare_repo.py (lines 265-422).

    Returns
    -------
        (base_commit_sha, reference_commit_sha)

    """
    default_branch = get_default_branch(repo_dir)
    git(repo_dir, "checkout", default_branch)
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    src_dir_path = repo_dir / src_dir if src_dir != "." else repo_dir
    if not src_dir_path.is_dir():
        raise ValueError(f"src_dir does not exist: {src_dir_path}")

    test_dirs = detect_ts_test_dirs(repo_dir)
    extra_scan_dirs = _collect_extra_scan_dirs(repo_dir, src_dir_path, test_dirs)

    if extra_scan_dirs:
        logger.info(
            "  Scanning %d extra dirs for import-time names: %s",
            len(extra_scan_dirs),
            [d.name for d in extra_scan_dirs],
        )

    package_json = repo_dir / "package.json"
    if package_json.exists():
        pkg_manager = detect_package_manager(repo_dir)
        logger.info("  Installing dependencies via %s...", pkg_manager)
        install_cmd = [pkg_manager, "install"]
        if pkg_manager != "bun":
            install_cmd.append("--ignore-scripts")
        subprocess.run(
            install_cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    logger.info("  Stubbing TypeScript source in: %s", src_dir)
    report = run_stub_ts(
        src_dir=src_dir_path,
        extra_scan_dirs=extra_scan_dirs if extra_scan_dirs else None,
        verbose=True,
    )

    if report.get("errors"):
        logger.warning(
            "  Stubbing had %d errors: %s",
            len(report["errors"]),
            report["errors"][:3],
        )

    logger.info(
        "  Stub report: %d files processed, %d modified, %d functions stubbed, "
        "%d import-time preserved",
        report.get("files_processed", 0),
        report.get("files_modified", 0),
        report.get("functions_stubbed", 0),
        report.get("functions_preserved", 0),
    )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing -- source may already be stubs?")
        return reference_commit, reference_commit

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
    logger.info("  Diff stats -- lines added: %d, removed: %d", additions, deletions)

    if additions == 0 or deletions == 0:
        raise RuntimeError(
            f"Stubbing verification failed for {full_name}: "
            f"additions={additions}, deletions={deletions}. "
            f"Expected both > 0."
        )

    git(repo_dir, "commit", "-m", "Commit 0")
    base_commit = get_head_sha(repo_dir)
    logger.info("  Base commit (stubbed): %s", base_commit[:12])

    return base_commit, reference_commit


def prepare_ts_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    src_dir_override: str | None = None,
    release_tag: str | None = None,
    dry_run: bool = False,
) -> dict | None:
    """Full pipeline for a single TypeScript repo.

    Fork -> Clone -> Detect src -> Stub -> Commit -> Push -> Return entry.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not dry_run:
        raise EnvironmentError("GITHUB_TOKEN is required for non-dry-run mode")

    logger.info("Processing %s (org=%s)", full_name, org)

    if dry_run:
        fork_name = f"{org}/{full_name.split('/')[-1]}"
        logger.info("  [DRY RUN] Would fork to %s", fork_name)
    else:
        fork_name = fork_repo_ts(full_name, org, token=token)

    repo_dir = full_clone(full_name, clone_dir, tag=release_tag)
    if release_tag:
        logger.info("  Pinned to tag: %s", release_tag)

    src_dir = src_dir_override or detect_ts_src_dir(repo_dir)
    if not src_dir:
        logger.error("  Cannot detect TypeScript source dir for %s", full_name)
        return None
    logger.info("  Source directory: %s", src_dir)

    setup_dict, test_dict, test_framework = generate_setup_dict_ts(repo_dir)
    logger.info(
        "  Test framework: %s, Package manager: %s",
        test_framework,
        setup_dict["install"].split()[0],
    )

    base_commit, reference_commit = create_ts_stubbed_branch(
        repo_dir, full_name, src_dir
    )

    if not dry_run:
        branch_name = TS_DATASET_BRANCH
        git(repo_dir, "checkout", branch_name)
        push_to_fork(repo_dir, fork_name, branch=branch_name, token=token)

    return {
        "instance_id": f"commit-0/{full_name.split('/')[-1]}",
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "src_dir": src_dir,
        "language": "typescript",
        "test_framework": test_framework,
        "setup": setup_dict,
        "test": test_dict,
    }


def main() -> None:
    """CLI entry point for prepare_repo_ts.py."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare TypeScript repos for commit0 dataset"
    )
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"GitHub org for fork (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--src-dir", default=None, help="Override auto-detected src dir"
    )
    parser.add_argument("--tag", default=None, help="Pin to a specific release tag")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Directory for cloned repos (default: repos_staging)",
    )
    parser.add_argument("--output", default=None, help="Output entries JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Skip fork and push")

    args = parser.parse_args()

    result = prepare_ts_repo(
        full_name=args.repo,
        clone_dir=args.clone_dir,
        org=args.org,
        src_dir_override=args.src_dir,
        release_tag=args.tag,
        dry_run=args.dry_run,
    )

    if result is None:
        logger.error("Failed to prepare %s", args.repo)
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
        entries = [result]
        with open(output_path, "w") as f:
            json.dump(entries, f, indent=2)
        logger.info("Wrote entries to %s", output_path)
    else:
        print(json.dumps(result, indent=2))

    logger.info("Done: %s", result["instance_id"])


if __name__ == "__main__":
    main()
