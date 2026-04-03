"""
Generate pytest test ID files (.bz2) for custom commit0 repos.

Runs `pytest --collect-only -q` against each repo to discover all test node IDs,
then saves them as bz2-compressed files compatible with commit0's evaluation harness.

Usage:
    # From dataset entries JSON:
    python -m tools.generate_test_ids dataset_entries.json --output-dir ./test_ids

    # From a local repo directory:
    python -m tools.generate_test_ids --repo-dir /path/to/repo --name mylib --output-dir ./test_ids

    # Using Docker (builds image first if needed):
    python -m tools.generate_test_ids dataset_entries.json --docker --output-dir ./test_ids

    # Install into commit0 data directory:
    python -m tools.generate_test_ids dataset_entries.json --install
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import docker
import docker.errors
import platform as platform_mod
import requests.exceptions

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _get_docker_platform() -> str:
    machine = platform_mod.machine()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, "amd64"
    )
    return f"linux/{arch}"


def _parse_collect_output(stdout: str) -> list[str]:
    """Parse pytest --collect-only output in any format (verbose or quiet).

    Handles:
    - Verbose format: ``<Module tests/test_foo.py>::<Class TestFoo>::<Function test_bar>``
    - Quiet format:   ``tests/test_foo.py::TestFoo::test_bar``
    - Mixed output with separator lines, errors, and empty lines
    """
    test_ids: list[str] = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip separator / summary / error lines
        if line.startswith(("=", "-", "no tests ran")):
            continue
        if "error" in line.lower() and "::" not in line:
            continue

        # Verbose format: <Module path>::<Class name>::<Function name>
        if line.startswith("<") and "::" in line:
            # Extract node-id: strip <Type ...> wrappers
            parts = line.split("::")
            id_parts: list[str] = []
            for part in parts:
                part = part.strip()
                if part.startswith("<") and part.endswith(">"):
                    # <Module tests/test_foo.py> → tests/test_foo.py
                    # <Class TestFoo> → TestFoo
                    inner = part[1:-1]
                    # First word is the type, rest is the name
                    idx = inner.find(" ")
                    if idx != -1:
                        id_parts.append(inner[idx + 1 :])
                    else:
                        id_parts.append(inner)
                elif part:
                    id_parts.append(part)
            if id_parts:
                test_ids.append("::".join(id_parts))
            continue

        # Quiet format: path::class::method or path::method
        if "::" in line:
            test_id = line.split(" ")[0]
            if test_id:
                test_ids.append(test_id)

    return test_ids


def collect_test_ids_local(
    repo_dir: Path,
    test_dir: str = "tests",
    test_cmd: str = "pytest",
    timeout: int = 300,
) -> list[str]:
    """Run pytest --collect-only in a local repo directory to discover test IDs.

    Uses verbose output first (handles unittest-style tests), falls back to
    quiet mode if verbose yields nothing.
    """
    # Try verbose first (handles unittest-style tests that don't show :: in -q mode)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "--override-ini=addopts=",
        "-p",
        "no:cacheprovider",
        test_dir,
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("  pytest --collect-only timed out after %ds", timeout)
        return []

    test_ids = _parse_collect_output(result.stdout)

    # Fallback: try quiet mode (faster, works for standard test suites)
    if not test_ids:
        cmd_q = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "--override-ini=addopts=",
            "-p",
            "no:cacheprovider",
            test_dir,
        ]
        try:
            result_q = subprocess.run(
                cmd_q,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            test_ids = _parse_collect_output(result_q.stdout)
        except subprocess.TimeoutExpired:
            pass

    return test_ids


def _find_docker_image(repo_name: str) -> str | None:
    """Find a built Docker image for this repo by searching commit0.repo.<name>.* tags."""
    try:
        client = docker.from_env()
        short_name = repo_name.split("__")[-1].split("-")[0].lower()
        needle = f"commit0.repo.{short_name}."
        for image in client.images.list():
            for tag in image.tags:
                if tag.startswith(needle):
                    return tag
        return None
    except Exception:
        return None


def collect_test_ids_docker(
    repo_name: str,
    test_dir: str = "tests",
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
) -> list[str]:
    """Run pytest --collect-only inside a Docker container.

    If reference_commit is provided, checks out the original (un-stubbed) code first
    so that test collection doesn't fail on import errors from removed functions.
    """
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    checkout = f"git checkout {reference_commit} -- . && " if reference_commit else ""

    client = docker.from_env()

    bash_cmd = (
        f"cd /testbed && source .venv/bin/activate && {checkout}"
        f"python -m pytest --collect-only --override-ini='addopts=' "
        f"-p no:cacheprovider {test_dir} 2>&1; true"
    )

    try:
        raw = client.containers.run(
            image_name,
            command=f"bash -c '{bash_cmd}'",
            remove=True,
            platform=_get_docker_platform(),
        )
        stdout = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        )
    except docker.errors.ContainerError as e:
        raw_err = e.stderr
        stdout = (
            raw_err.decode("utf-8", errors="replace")
            if isinstance(raw_err, bytes)
            else (raw_err or "")
        )
    except requests.exceptions.ReadTimeout:
        logger.warning("  Docker pytest --collect-only timed out after %ds", timeout)
        return []

    test_ids = _parse_collect_output(stdout)

    if not test_ids:
        bash_cmd_q = (
            f"cd /testbed && source .venv/bin/activate && {checkout}"
            f"python -m pytest --collect-only -q --no-header --override-ini='addopts=' "
            f"-p no:cacheprovider {test_dir} 2>&1; true"
        )
        try:
            raw_q = client.containers.run(
                image_name,
                command=f"bash -c '{bash_cmd_q}'",
                remove=True,
                platform=_get_docker_platform(),
            )
            stdout_q = (
                raw_q.decode("utf-8", errors="replace")
                if isinstance(raw_q, bytes)
                else raw_q
            )
        except docker.errors.ContainerError as e:
            raw_err_q = e.stderr
            stdout_q = (
                raw_err_q.decode("utf-8", errors="replace")
                if isinstance(raw_err_q, bytes)
                else (raw_err_q or "")
            )
        except requests.exceptions.ReadTimeout:
            return []
        test_ids = _parse_collect_output(stdout_q)

    return test_ids


def validate_base_commit_docker(
    repo_name: str,
    test_dir: str = "tests",
    image_name: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run pytest --collect-only at base_commit (stubbed code) inside Docker.

    Returns (tests_collected, stderr_snippet).
    If tests_collected == 0, the stubbed code breaks imports and the pipeline will fail.
    """
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    client = docker.from_env()

    bash_cmd = (
        f"cd /testbed && source .venv/bin/activate && "
        f"python -m pytest --collect-only --override-ini='addopts=' "
        f"-p no:cacheprovider {test_dir} 2>&1 | tail -20"
    )

    try:
        raw = client.containers.run(
            image_name,
            command=f"bash -c '{bash_cmd}'",
            remove=True,
            platform=_get_docker_platform(),
        )
        stdout = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        )
    except docker.errors.ContainerError as e:
        raw_err = e.stderr
        stdout = (
            raw_err.decode("utf-8", errors="replace")
            if isinstance(raw_err, bytes)
            else (raw_err or "")
        )
    except requests.exceptions.ReadTimeout:
        return 0, "timeout"

    test_ids = _parse_collect_output(stdout)
    stderr_snippet = stdout[-500:] if stdout else ""
    return len(test_ids), stderr_snippet


def save_test_ids(
    test_ids: list[str],
    name: str,
    output_dir: Path,
) -> Path:
    """Save test IDs as a bz2-compressed file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    name = name.lower().replace(".", "-")
    output_file = output_dir / f"{name}.bz2"

    content = "\n".join(test_ids)
    with bz2.open(output_file, "wt") as f:
        f.write(content)

    return output_file


def install_test_ids(
    source_dir: Path,
    repo_names: list[str] | None = None,
) -> int:
    """Copy test ID .bz2 files into commit0's data directory."""
    try:
        import commit0

        data_dir = Path(os.path.dirname(commit0.__file__)) / "data" / "test_ids"
    except ImportError:
        logger.error("commit0 package not found — cannot install test IDs")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    for bz2_file in sorted(source_dir.glob("*.bz2")):
        name = bz2_file.stem
        if repo_names and name not in [r.lower().replace(".", "-") for r in repo_names]:
            continue

        dest = data_dir / bz2_file.name
        import shutil

        shutil.copy2(bz2_file, dest)
        logger.info("  Installed: %s -> %s", bz2_file.name, dest)
        installed += 1

    return installed


def _find_repo_dir(
    clone_dir: Path | None,
    fork_repo: str,
    original_repo: str,
) -> Path | None:
    """Locate the cloned repo directory, checking fork name then original name."""
    base = clone_dir or Path("./repos_staging")
    candidates = [fork_repo]
    if original_repo and original_repo != fork_repo:
        candidates.append(original_repo)

    for name in candidates:
        candidate = base / name.replace("/", "__")
        if candidate.is_dir():
            return candidate

    return None


def generate_for_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
    validate_base: bool = False,
) -> dict[str, int]:
    """Generate test IDs for all repos in a dataset entries JSON file."""
    data = json.loads(dataset_path.read_text())

    if isinstance(data, dict) and "data" in data:
        entries = data["data"]
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Unknown dataset format in {dataset_path}")

    results: dict[str, int] = {}

    for i, entry in enumerate(entries):
        if max_repos and i >= max_repos:
            break

        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        test_dir = entry.get("test", {}).get("test_dir", "tests")
        instance_id = entry.get("instance_id", repo_name)

        logger.info(
            "\n[%d/%d] Collecting test IDs for %s...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            instance_id,
        )

        if use_docker:
            test_ids = collect_test_ids_docker(
                repo_name=repo_name,
                test_dir=test_dir,
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
            )
        else:
            repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))

            if not repo_dir or not repo_dir.is_dir():
                logger.warning(
                    "  Repo dir not found — skipping (tried fork + original name)"
                )
                results[repo_name] = 0
                continue

            reference_commit = entry.get("reference_commit")
            if reference_commit:
                try:
                    subprocess.run(
                        ["git", "checkout", reference_commit],
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                except Exception as e:
                    logger.warning("  Could not checkout reference_commit: %s", e)

            test_ids = collect_test_ids_local(
                repo_dir=repo_dir,
                test_dir=test_dir,
                timeout=timeout,
            )

            if not test_ids:
                docker_image = _find_docker_image(repo_name)
                if docker_image:
                    logger.info(
                        "  Local collection returned 0 — retrying in Docker (%s)",
                        docker_image,
                    )
                    test_ids = collect_test_ids_docker(
                        repo_name=repo_name,
                        test_dir=test_dir,
                        image_name=docker_image,
                        reference_commit=entry.get("reference_commit"),
                        timeout=timeout,
                    )

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)

            if validate_base and use_docker:
                base_collected, stderr = validate_base_commit_docker(
                    repo_name=repo_name,
                    test_dir=test_dir,
                    timeout=timeout,
                )
                if base_collected == 0:
                    logger.warning(
                        "  ⚠ BASE COMMIT VALIDATION FAILED: 0 tests collected at base_commit (stubbed code)."
                        " The import chain is broken — pipeline will produce 0%% pass rate."
                    )
                    logger.warning("  Last output: %s", stderr[:200])
                    results[repo_name] = -len(test_ids)
                else:
                    logger.info(
                        "  ✓ Base commit validation: %d tests collected at base_commit",
                        base_collected,
                    )
        else:
            logger.warning("  No test IDs collected for %s", repo_name)
            results[repo_name] = 0

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate pytest test ID files for commit0 repos"
    )
    parser.add_argument(
        "dataset_file",
        nargs="?",
        help="Input dataset_entries.json or custom_dataset.json",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        help="Generate for a single local repo directory",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="Repo name (required with --repo-dir)",
    )
    parser.add_argument(
        "--test-dir",
        type=str,
        default="tests",
        help="Test directory within repo (default: tests)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./test_ids",
        help="Output directory for .bz2 files (default: ./test_ids)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default=None,
        help="Directory where repos are cloned (default: ./repos_staging)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run pytest inside Docker containers (requires built images)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install generated .bz2 files into commit0's data directory",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per repo for pytest collection (default: 300s)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to process",
    )
    parser.add_argument(
        "--validate-base",
        action="store_true",
        help="After collecting IDs at reference_commit, validate that base_commit (stubbed code) can also collect tests. Requires --docker.",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    if args.repo_dir:
        if not args.name:
            parser.error("--name is required with --repo-dir")
        repo_dir = Path(args.repo_dir)
        logger.info("Collecting test IDs from %s...", repo_dir)

        test_ids = collect_test_ids_local(
            repo_dir=repo_dir,
            test_dir=args.test_dir,
            timeout=args.timeout,
        )
        if test_ids:
            out_file = save_test_ids(test_ids, args.name, output_dir)
            logger.info("Saved %d test IDs to %s", len(test_ids), out_file)
        else:
            logger.error("No test IDs collected")
            sys.exit(1)

    elif args.dataset_file:
        dataset_path = Path(args.dataset_file)
        if not dataset_path.exists():
            parser.error(f"File not found: {dataset_path}")

        clone_dir = Path(args.clone_dir) if args.clone_dir else None

        results = generate_for_dataset(
            dataset_path=dataset_path,
            output_dir=output_dir,
            use_docker=args.docker,
            clone_dir=clone_dir,
            timeout=args.timeout,
            max_repos=args.max_repos,
            validate_base=args.validate_base,
        )

        total = sum(results.values())
        repos_with_tests = sum(1 for v in results.values() if v > 0)
        logger.info(
            "\nDone: %d test IDs across %d repos (%d repos had no tests)",
            total,
            len(results),
            len(results) - repos_with_tests,
        )
    else:
        parser.error("Provide either dataset_file or --repo-dir")
        return

    if args.install:
        installed = install_test_ids(output_dir)
        logger.info("Installed %d test ID files into commit0 data directory", installed)


if __name__ == "__main__":
    main()
