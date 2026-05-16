"""Prepare Go repos for a commit0 dataset.

For each validated Go candidate:
1. Fork to target GitHub org
2. Create a 'commit0_all' branch
3. Apply Go AST stubbing via gostubber binary
4. Commit stubbed version as base_commit
5. Reset to original as reference_commit
6. Generate setup/test dict entries
7. Output dataset entries (GoRepoInstance-compatible)

Usage:
    python -m tools.prepare_repo_go validated.json --output dataset_entries.json
    python -m tools.prepare_repo_go --repo sourcegraph/conc --clone-dir ./repos_staging --output dataset_entries.json
    python -m tools.prepare_repo_go validated.json --dry-run --output dataset_entries.json

Requires:
    - GITHUB_TOKEN env var with repo/fork permissions
    - gh CLI installed (for forking)
    - gostubber binary (built automatically from tools/gostubber/)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import re

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_ORG = "Zahgon"
TOOLS_DIR = Path(__file__).parent


def _find_goimports() -> str:
    """Find goimports binary, checking PATH and common Go install locations."""
    path = shutil.which("goimports")
    if path:
        return path
    for candidate in [
        Path.home() / "go" / "bin" / "goimports",
        Path(os.environ.get("GOPATH", "")) / "bin" / "goimports"
        if os.environ.get("GOPATH")
        else None,
        Path(os.environ.get("GOROOT", "")) / "bin" / "goimports"
        if os.environ.get("GOROOT")
        else None,
    ]:
        if candidate and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "goimports not found. Install with: go install golang.org/x/tools/cmd/goimports@latest "
        "and ensure ~/go/bin is on PATH, or set GOPATH."
    )


sys.path.insert(0, str(TOOLS_DIR.parent))
from tools.stub_go import _ensure_gostubber, stub_go_repo

_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync


def git(repo_dir: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
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
    return git(repo_dir, "rev-parse", "HEAD")


def get_default_branch(repo_dir: Path) -> str:
    try:
        ref = git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.split("/")[-1]
    except subprocess.CalledProcessError:
        for branch in ["main", "master"]:
            try:
                git(repo_dir, "rev-parse", f"refs/remotes/origin/{branch}")
                return branch
            except subprocess.CalledProcessError:
                continue
        return "main"


def fork_repo(full_name: str, org: str) -> str:
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
    except Exception:
        pass

    logger.info("  Forking %s to %s...", full_name, org)
    fork_result = subprocess.run(
        ["gh", "repo", "fork", full_name, "--org", org, "--clone=false"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if fork_result.returncode != 0:
        raise RuntimeError(
            f"gh repo fork failed (exit {fork_result.returncode}) for {full_name} -> {org}.\n"
            f"stdout: {fork_result.stdout.strip()}\n"
            f"stderr: {fork_result.stderr.strip()}"
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
        except Exception:
            pass
        time.sleep(2)

    raise RuntimeError(f"Fork {fork_name} not available after 20s")


def full_clone(
    full_name: str, clone_dir: Path, branch: str | None = None, tag: str | None = None
) -> Path:
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


def detect_go_module(repo_dir: Path) -> dict:
    """Detect Go module info from go.mod."""
    go_mod = repo_dir / "go.mod"
    if not go_mod.exists():
        return {}

    info: dict = {}
    content = go_mod.read_text()
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("module "):
            info["module_path"] = line.split(None, 1)[1]
        elif line.startswith("go "):
            info["go_version"] = line.split(None, 1)[1]
    return info


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    branch_name: str | None = None,
) -> tuple[str, str]:
    """Create the commit0 branch with Go-stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_all'
    3. Run gostubber on .go source files
    4. Commit stubbed version as base_commit
    """
    if branch_name is None:
        branch_name = "commit0_all"

    gostubber_bin = _ensure_gostubber()
    default_branch = get_default_branch(repo_dir)

    git(repo_dir, "checkout", default_branch)
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    logger.info("  Running gostubber on %s...", repo_dir.name)
    stubbed_count = 0
    for go_file in repo_dir.rglob("*.go"):
        rel = go_file.relative_to(repo_dir)
        if any(p in {"vendor", ".git", "testdata"} for p in rel.parts):
            continue
        if go_file.name.endswith("_test.go") or go_file.name == "doc.go":
            continue
        result = subprocess.run(
            [str(gostubber_bin), str(go_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            stubbed_count += 1
    logger.info("  Stubbed %d Go files", stubbed_count)

    logger.info("  Running goimports to clean unused imports...")
    goimports_bin = _find_goimports()
    subprocess.run(
        [goimports_bin, "-w", "."],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )

    for test_file in repo_dir.rglob("*_test.go"):
        subprocess.run(
            ["git", "checkout", "--", str(test_file.relative_to(repo_dir))],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
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
                f"Expected both >0."
            )

        git(repo_dir, "commit", "-m", "Commit 0")
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])
    return base_commit, reference_commit


def push_to_fork(
    repo_dir: Path,
    fork_name: str,
    branch: str | None = None,
    token: str | None = None,
) -> None:
    """Add fork as remote and push the commit0 branch."""
    if branch is None:
        branch = "commit0_all"
    if token:
        fork_url = f"https://x-access-token:{token}@github.com/{fork_name}.git"
    else:
        fork_url = f"https://github.com/{fork_name}.git"

    try:
        git(repo_dir, "remote", "remove", "fork", check=False)
    except Exception:
        pass
    git(repo_dir, "remote", "add", "fork", fork_url)

    logger.info("  Pushing %s to %s...", branch, fork_name)
    git(repo_dir, "push", "-f", "fork", branch, timeout=300)


def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    """Resolve base/reference commits from remote branch via GitHub API."""
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
    except Exception as e:
        logger.debug("Non-critical failure during remote commit resolution: %s", e)
        return None


def build_setup_dict(repo_dir: Path, go_info: dict) -> dict:
    """Build the setup dict for a Go repo (mirrors Python's pip/packages setup)."""
    pre_install: list[str] = []

    apt_deps_file = repo_dir / ".apt-packages"
    if apt_deps_file.exists():
        pre_install = [
            line.strip()
            for line in apt_deps_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    module_path = go_info.get("module_path", "")
    spec_url = _find_docs_url(repo_dir, module_path)

    return {
        "install": "go mod download && go build ./...",
        "packages": "",
        "pip_packages": "",
        "pre_install": pre_install,
        "go_version": go_info.get("go_version", "1.25"),
        "specification": spec_url,
    }


def _find_docs_url(repo_dir: Path, module_path: str) -> str:
    """Try to find documentation URL for a Go repo.

    Priority:
    1. README links to official docs site (godoc.org, pkg.go.dev, or custom docs)
    2. pkg.go.dev page for the module
    """
    readme_names = ["README.md", "README.rst", "README.txt", "README", "readme.md"]
    readme_content = ""
    for name in readme_names:
        readme_file = repo_dir / name
        if readme_file.exists():
            readme_content = readme_file.read_text(errors="replace")
            break

    if readme_content:
        doc_patterns = [
            r'https?://[^\s\)>\]"\']+\.(?:readthedocs|rtfd)\.io[^\s\)>\]"\']*',
            r'https?://[^\s\)>\]"\']*docs?\.[^\s\)>\]"\']+',
            r'https?://pkg\.go\.dev/[^\s\)>\]"\']+',
            r'https?://godoc\.org/[^\s\)>\]"\']+',
            r'https?://(?:github\.com|gitlab\.com)/[^\s\)>\]"\']*/wiki[^\s\)>\]"\"]*',
        ]
        for pattern in doc_patterns:
            m = re.search(pattern, readme_content)
            if m:
                url = m.group(0).rstrip(".,;:!?)")
                if not any(
                    x in url.lower()
                    for x in ["badge", "shields.io", "img.shields", "goreportcard"]
                ):
                    return url

    if module_path:
        return f"https://pkg.go.dev/{module_path}"

    return ""



def _generate_readme_spec_pdf(
    repo_dir: Path,
    specs_dir: str | Path,
    repo_name: str,
) -> Path | None:
    """Generate a spec PDF from the repo README as a fallback when no docs URL is found.

    Reads the full README content and all valid HTTP(S) links present in it,
    then renders them into a bz2-compressed PDF compatible with the spec.pdf.bz2
    format expected by the runtime agent. Returns path to the .pdf.bz2 file,
    or None if no README exists or PyMuPDF is unavailable.
    """
    readme_names = ["README.md", "README.rst", "README.txt", "README", "readme.md"]
    readme_content = ""
    readme_name = "README"
    for name in readme_names:
        candidate = repo_dir / name
        if candidate.exists():
            readme_content = candidate.read_text(errors="replace")
            readme_name = name
            break

    if not readme_content.strip():
        logger.info("  No README found for %s — skipping README spec fallback", repo_name)
        return None

    # Collect all unique HTTP(S) URLs from README, order-preserved
    all_urls = list(dict.fromkeys(re.findall(r'https?://[^\s\)>\]"\"]+', readme_content)))

    # Build spec document: full README content followed by a links appendix
    header = f"{repo_name} — Specification (generated from {readme_name})"
    sep = "=" * len(header)
    doc_lines: list[str] = [header, sep, "", readme_content.strip()]
    if all_urls:
        doc_lines += ["", sep, "Referenced Links", sep, ""]
        doc_lines.extend(f"  {url}" for url in all_urls)
    full_text = "\n".join(doc_lines)

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning(
            "  README spec fallback unavailable — install PyMuPDF: pip install PyMuPDF"
        )
        return None

    import bz2

    # Render text to a multi-page A4 PDF
    page_w, page_h = 595, 842  # A4 in points
    margin = 40
    font_size = 9
    line_h = font_size * 1.35
    max_chars = int((page_w - 2 * margin) / (font_size * 0.52))

    def _wrap(line: str) -> list[str]:
        if len(line) <= max_chars:
            return [line]
        wrapped: list[str] = []
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            wrapped.append(line[:cut])
            line = line[cut:].lstrip()
        if line:
            wrapped.append(line)
        return wrapped

    doc = fitz.open()

    def _new_page():
        p = doc.new_page(width=page_w, height=page_h)
        return p, margin + font_size

    page, y = _new_page()
    for raw_line in full_text.split("\n"):
        for sub in _wrap(raw_line):
            if y + line_h > page_h - margin:
                page, y = _new_page()
            if sub.strip():
                page.insert_text((margin, y), sub, fontsize=font_size, color=(0, 0, 0))
            y += line_h

    pdf_bytes = doc.tobytes()
    doc.close()

    specs_path = Path(specs_dir)
    specs_path.mkdir(parents=True, exist_ok=True)
    out_path = specs_path / f"{repo_name}_readme_spec.pdf.bz2"
    with bz2.open(out_path, "wb") as fh:
        fh.write(pdf_bytes)

    logger.info("  README-based spec written: %s", out_path)
    return out_path


def build_test_dict(repo_dir: Path) -> dict:
    """Build the test dict for a Go repo."""
    return {
        "test_cmd": "go test -json -count=1 ./...",
        "test_dir": ".",
    }


def prepare_single_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    tag: str | None = None,
    specs_dir: str = "./specs",
) -> dict | None:
    logger.info("\n=== Preparing %s ===", full_name)

    try:
        if dry_run:
            forked_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", forked_name)
        else:
            forked_name = fork_repo(full_name, org)

        repo_dir = full_clone(full_name, clone_dir, tag=tag)
        go_info = detect_go_module(repo_dir)

        if not go_info.get("module_path"):
            logger.warning("  No go.mod found — skipping %s", full_name)
            return None

        base_commit, reference_commit = create_stubbed_branch(repo_dir, full_name)

        if not dry_run:
            token = os.environ.get("GITHUB_TOKEN")
            branch_name = "commit0_all"
            try:
                git(repo_dir, "checkout", branch_name)
                push_to_fork(repo_dir, forked_name, branch=branch_name, token=token)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                remote_commits = resolve_commits_from_remote(forked_name, branch_name)
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

        setup_dict = build_setup_dict(repo_dir, go_info)
        test_dict = build_test_dict(repo_dir)

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
                    output_dir=str(specs_dir),
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

                    if not dry_run:
                        token = os.environ.get("GITHUB_TOKEN")
                        try:
                            push_to_fork(
                                repo_dir, forked_name, branch=branch_name, token=token
                            )
                        except Exception as e:
                            logger.warning("  Spec push failed: %s", e)
                else:
                    logger.warning("  Spec scraping returned no output")
            except ImportError:
                logger.warning(
                    "  Skipping spec scrape — install: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium"
                )
            except Exception as e:
                logger.warning("  Spec scraping failed: %s", e)

        # Fallback: generate a README-based spec if URL scraping produced nothing
        if spec_path is None:
            _rname = full_name.split("/")[-1]
            readme_spec_path = _generate_readme_spec_pdf(repo_dir, specs_dir, _rname)
            if readme_spec_path:
                try:
                    branch_name = "commit0_all"
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(readme_spec_path, dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {_rname}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info(
                        "  Updated base_commit with README spec: %s", base_commit[:12]
                    )
                    if not dry_run:
                        token = os.environ.get("GITHUB_TOKEN")
                        try:
                            push_to_fork(
                                repo_dir, forked_name, branch=branch_name, token=token
                            )
                        except Exception as push_err:
                            logger.warning("  README spec push failed: %s", push_err)
                    spec_path = readme_spec_path
                except Exception as commit_err:
                    logger.warning("  README spec commit failed: %s", commit_err)

        repo_name = full_name.split("/")[-1]
        entry = {
            "instance_id": f"{repo_name}_go",
            "repo": forked_name,
            "original_repo": full_name,
            "base_commit": base_commit,
            "reference_commit": reference_commit,
            "setup": setup_dict,
            "test": test_dict,
            "src_dir": ".",
            "language": "go",
        }

        if dry_run:
            entry["base_commit"] = "DRY_RUN"
            entry["reference_commit"] = "DRY_RUN"

        logger.info("  Entry created for %s", full_name)
        return entry

    except Exception as e:
        logger.error("  FAILED to prepare %s: %s", full_name, e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Go repos for a commit0 dataset"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input validated.json from validate_go.py",
    )
    parser.add_argument("--repo", type=str, help="Single repo to prepare (owner/name)")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("./repos_staging"),
        help="Directory for cloning repos (default: ./repos_staging)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="dataset_entries.json",
        help="Output JSON file (default: dataset_entries.json)",
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
        help="Skip GitHub fork and push operations",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Maximum number of repos to prepare",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Git tag to checkout before stubbing",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )

    args = parser.parse_args()

    args.clone_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []

    if args.repo:
        result = prepare_single_repo(
            args.repo,
            args.clone_dir,
            org=args.org,
            dry_run=args.dry_run,
            tag=args.tag,
            specs_dir=args.specs_dir,
        )
        if result:
            entries.append(result)
    elif args.input_file:
        candidates = json.loads(Path(args.input_file).read_text())
        if isinstance(candidates, dict) and "data" in candidates:
            candidates = candidates["data"]

        for i, candidate in enumerate(candidates):
            if args.max_repos and i >= args.max_repos:
                break

            full_name = candidate.get("full_name") or candidate.get("repo", "")
            if not full_name:
                logger.warning("  Skipping entry %d: no full_name or repo", i)
                continue

            result = prepare_single_repo(
                full_name,
                args.clone_dir,
                org=args.org,
                dry_run=args.dry_run,
                tag=candidate.get("tag"),
                specs_dir=args.specs_dir,
            )
            if result:
                entries.append(result)
    else:
        parser.error("Provide either input_file or --repo")

    output_path = Path(args.output)
    output_path.write_text(json.dumps(entries, indent=2))
    logger.info("\nSaved %d entries to %s", len(entries), output_path)


if __name__ == "__main__":
    main()
