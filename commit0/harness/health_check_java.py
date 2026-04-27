import logging
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

JAVA_TOOL_CHECKS = {
    "java": ["java", "-version"],
    "javac": ["javac", "-version"],
    "mvn": ["mvn", "--version"],
    "gradle": ["gradle", "--version"],
}


def check_java_toolchain() -> Dict[str, Optional[str]]:
    results = {}
    for tool, cmd in JAVA_TOOL_CHECKS.items():
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            output = result.stdout.strip() or result.stderr.strip()
            results[tool] = output.split("\n")[0] if output else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            results[tool] = None
    return results


def check_java_version(required: str = "17") -> bool:
    result = check_java_toolchain()
    java_output = result.get("java")
    if not java_output:
        return False
    return required in java_output


def check_docker_java_images(prefix: str = "commit0-java") -> List[str]:
    try:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [
            line for line in result.stdout.strip().split("\n")
            if line.startswith(prefix)
        ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def health_check_java() -> Dict[str, bool]:
    toolchain = check_java_toolchain()
    return {
        "java_installed": toolchain.get("java") is not None,
        "javac_installed": toolchain.get("javac") is not None,
        "maven_installed": toolchain.get("mvn") is not None,
        "gradle_installed": toolchain.get("gradle") is not None,
        "docker_available": len(check_docker_java_images()) > 0,
    }
