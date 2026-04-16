from __future__ import annotations

import logging
from typing import Optional

import docker

logger = logging.getLogger(__name__)

_PIP_IMPORT_MAP = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-dateutil": "dateutil",
    "scikit-learn": "sklearn",
    "beautifulsoup4": "bs4",
    "python-dotenv": "dotenv",
    "attrs": "attr",
    "pyjwt": "jwt",
    "python-jose": "jose",
    "python-multipart": "multipart",
    "msgpack-python": "msgpack",
}


def pip_to_import(pip_name: str) -> str:
    normalized = pip_name.lower().split("[")[0]
    normalized = (
        normalized.split(">")[0]
        .split("<")[0]
        .split("=")[0]
        .split("!")[0]
        .split("~")[0]
        .strip()
    )
    return _PIP_IMPORT_MAP.get(normalized, normalized.replace("-", "_"))


def check_imports(
    client: docker.DockerClient,
    image_name: str,
    packages: list[str],
) -> tuple[bool, str]:
    skip_prefixes = ("pytest", "coverage", "pip", "setuptools", "wheel")
    importable = [
        pip_to_import(p)
        for p in packages
        if not any(p.lower().startswith(s) for s in skip_prefixes)
    ]
    if not importable:
        return True, "No packages to check"

    import_stmts = "; ".join(f"import {m}" for m in importable)
    cmd = f'python -c "{import_stmts}"'
    try:
        client.containers.run(image_name, cmd, remove=True, stderr=True, stdout=True)
        return True, f"All {len(importable)} packages importable"
    except docker.errors.ContainerError as e:
        stderr = e.stderr.decode() if e.stderr else str(e)
        return False, f"Import check failed:\n{stderr}"
    except Exception as e:
        return False, f"Import check error: {e}"


def check_python_version(
    client: docker.DockerClient,
    image_name: str,
    expected: str,
) -> tuple[bool, str]:
    version_cmd = "python -c \"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')\""
    try:
        output = client.containers.run(
            image_name, version_cmd, remove=True, stderr=True, stdout=True
        )
        actual = output.decode().strip()
        if actual == expected:
            return True, f"Python {actual}"
        return False, f"Expected Python {expected}, got {actual}"
    except Exception as e:
        return False, f"Python version check error: {e}"


def run_health_checks(
    client: docker.DockerClient,
    image_name: str,
    pip_packages: Optional[list[str]] = None,
    python_version: Optional[str] = None,
) -> list[tuple[bool, str, str]]:
    results: list[tuple[bool, str, str]] = []
    if pip_packages:
        passed, detail = check_imports(client, image_name, pip_packages)
        results.append((passed, "imports", detail))
    if python_version:
        passed, detail = check_python_version(client, image_name, python_version)
        results.append((passed, "python_version", detail))
    return results


__all__ = [
    "check_imports",
    "check_python_version",
    "pip_to_import",
    "run_health_checks",
]
