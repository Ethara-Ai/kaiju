"""Python wrapper for the TypeScript stubbing engine.

Invokes tools/stub_ts.ts via npx ts-node, captures the JSON report from
stdout, and returns it as a dict. Stderr is used for diagnostic logging.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent

MAX_STDERR_LOG_CHARS = 2000
MAX_STDOUT_LOG_CHARS = 500


def run_stub_ts(
    src_dir: Path,
    extra_scan_dirs: list[Path] | None = None,
    mode: str = "all",
    verbose: bool = False,
    timeout: int = 300,
) -> dict:
    """Run the ts-morph stubbing engine via subprocess.

    Args:
    ----
        src_dir: Absolute path to the TypeScript source directory to stub.
        extra_scan_dirs: Additional directories to scan for import-time names
                         (e.g. test dirs, sibling packages). Not stubbed.
        mode: Stubbing mode. Only "all" is supported for TypeScript.
        verbose: Enable debug logging in the TS engine (sent to stderr).
        timeout: Maximum seconds to wait for the subprocess.

    Returns:
    -------
        The JSON report dict from stub_ts.ts with keys:
        files_processed, files_modified, functions_stubbed,
        functions_preserved, import_time_names, errors.

    Raises:
    ------
        RuntimeError: If the subprocess fails or returns invalid JSON.

    """
    stub_ts_path = TOOLS_DIR / "stub_ts.ts"
    if not stub_ts_path.exists():
        raise FileNotFoundError(f"TypeScript stubber not found: {stub_ts_path}")

    cmd = [
        "npx",
        "ts-node",
        str(stub_ts_path),
        "--src-dir",
        str(src_dir),
        "--mode",
        mode,
    ]
    if extra_scan_dirs:
        cmd.extend(
            [
                "--extra-scan-dirs",
                ",".join(str(d) for d in extra_scan_dirs),
            ]
        )
    if verbose:
        cmd.append("--verbose")

    cwd = str(PROJECT_ROOT)

    logger.info("Running TS stubber: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )

    if result.returncode != 0:
        logger.error(
            "TS stubber failed (rc=%d):\nstderr: %s\nstdout: %s",
            result.returncode,
            result.stderr[:MAX_STDERR_LOG_CHARS],
            result.stdout[:MAX_STDOUT_LOG_CHARS],
        )
        raise RuntimeError(
            f"TS stubber failed (rc={result.returncode}): "
            f"{result.stderr[:MAX_STDOUT_LOG_CHARS]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError("TS stubber produced no output on stdout")

    json_start = stdout.find("{")
    json_end = stdout.rfind("}")
    if json_start < 0 or json_end < 0 or json_end <= json_start:
        logger.error(
            "TS stubber output not valid JSON:\n%s", stdout[:MAX_STDOUT_LOG_CHARS]
        )
        raise RuntimeError("TS stubber output contains no JSON object")

    json_str = stdout[json_start : json_end + 1]

    try:
        report = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(
            "TS stubber output not valid JSON:\n%s", json_str[:MAX_STDOUT_LOG_CHARS]
        )
        raise RuntimeError(f"TS stubber output not JSON: {e}") from e

    logger.info(
        "TS stubbing complete: %d files processed, %d modified, "
        "%d functions stubbed, %d import-time preserved, %d errors",
        report.get("files_processed", 0),
        report.get("files_modified", 0),
        report.get("functions_stubbed", 0),
        report.get("functions_preserved", 0),
        len(report.get("errors", [])),
    )

    if result.stderr and verbose:
        logger.debug("TS stubber stderr:\n%s", result.stderr[:MAX_STDERR_LOG_CHARS])

    return report
