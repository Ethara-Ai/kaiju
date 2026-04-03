import logging
import os
import platform as _platform
import re
import shutil
import subprocess
import traceback
import docker
import docker.errors
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from commit0.harness.constants import (
    BASE_IMAGE_BUILD_DIR,
    REPO_IMAGE_BUILD_DIR,
    OCI_IMAGE_DIR,
)
from commit0.harness.spec import get_specs_from_dataset
from commit0.harness.utils import setup_logger, close_logger

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def _native_platform() -> str:
    """Return the Docker platform string for the current machine architecture."""
    machine = _platform.machine()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


def _safe_builder_args() -> list[str]:
    """Return ``['--builder', name]`` for a builder that supports ``--load``.

    ``docker-container`` builders cannot ``--load`` into the daemon.  If the
    current default builder uses that driver, fall back to the well-known
    ``default`` or ``desktop-linux`` builders which always use the ``docker``
    driver.  When no such builder exists, return an empty list so buildx
    uses whatever is currently active (best-effort).
    """
    try:
        result = subprocess.run(
            ["docker", "buildx", "inspect"],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        if "docker-container" in output:
            # Current builder uses docker-container driver — can't --load.
            # Try builders that use the docker driver.
            for candidate in ("desktop-linux", "default"):
                probe = subprocess.run(
                    ["docker", "buildx", "inspect", candidate],
                    capture_output=True,
                    text=True,
                )
                probe_out = probe.stdout + probe.stderr
                if probe.returncode == 0 and "docker-container" not in probe_out:
                    return ["--builder", candidate]
            return []  # best-effort: let buildx figure it out
    except Exception:
        pass
    return []  # current builder is fine


PROXY_ENV_KEYS = [
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
]


def _mitm_disabled() -> bool:
    return os.environ.get("COMMIT0_MITM_DISABLED", "").strip() in ("1", "true", "yes")


def get_proxy_env() -> dict[str, str]:
    """Collect proxy-related env vars from the host. Used for both build args and runtime env.

    Returns empty dict if COMMIT0_MITM_DISABLED=1.
    """
    if _mitm_disabled():
        return {}
    return {k: os.environ[k] for k in PROXY_ENV_KEYS if os.environ.get(k)}


def _is_pem_cert(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            first_line = f.readline()
        return b"-----BEGIN CERTIFICATE-----" in first_line
    except (OSError, IOError):
        return False


def _resolve_mitm_ca_cert() -> Optional[Path]:
    """Find the MITM CA certificate.

    Search order:
      1. MITM_CA_CERT env var (explicit path)
      2. ~/.mitmproxy/mitmproxy-ca-cert.pem (mitmproxy default)

    Returns None if disabled via COMMIT0_MITM_DISABLED=1 or no valid cert found.
    """
    if _mitm_disabled():
        return None

    env_path = os.environ.get("MITM_CA_CERT")
    if env_path:
        p = Path(env_path)
        if p.is_file() and _is_pem_cert(p):
            return p

    default_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if default_path.is_file() and _is_pem_cert(default_path):
        return default_path

    return None


class BuildImageError(Exception):
    def __init__(self, image_name: str, message: str, logger: logging.Logger):
        super().__init__(message)
        self.super_str = super().__str__()
        self.image_name = image_name
        self.log_path = ""  # logger.log_file
        self.logger = logger

    def __str__(self):
        return (
            f"Error building image {self.image_name}: {self.super_str}\n"
            f"Check ({self.log_path}) for more information."
        )


def build_image(
    image_name: str,
    setup_scripts: dict,
    dockerfile: str,
    platform: str,
    client: docker.DockerClient,
    build_dir: Path,
    nocache: bool = False,
    mitm_ca_cert: Optional[Path] = None,
) -> None:
    """Builds a docker image with the given name, setup scripts, dockerfile, and platform.

    Produces two outputs:
      1. A multi-arch OCI tarball (linux/amd64 + linux/arm64) for pushing to a container registry.
      2. A native-arch image loaded into the local Docker daemon for immediate use.

    Args:
    ----
        image_name (str): Name of the image to build
        setup_scripts (dict): Dictionary of setup script names to setup script contents
        dockerfile (str): Contents of the Dockerfile
        platform (str): Comma-separated platforms for the OCI tarball (e.g. "linux/amd64,linux/arm64")
        client (docker.DockerClient): Docker client to use for building the image
        build_dir (Path): Directory for the build context (will also contain logs, scripts, and artifacts)
        nocache (bool): Whether to use the cache when building
        mitm_ca_cert (Path): Pre-resolved path to a MITM CA certificate PEM file

    """
    logger = setup_logger(image_name, build_dir / "build_image.log")
    logger.info(
        f"Building image {image_name}\n"
        f"Using dockerfile:\n{dockerfile}\n"
        f"Adding ({len(setup_scripts)}) setup scripts to image build repo"
    )

    for setup_script_name, setup_script in setup_scripts.items():
        logger.info(f"[SETUP SCRIPT] {setup_script_name}:\n{setup_script}")
    try:
        for setup_script_name, setup_script in setup_scripts.items():
            setup_script_path = build_dir / setup_script_name
            with open(setup_script_path, "w") as f:
                f.write(setup_script)
            if setup_script_name not in dockerfile:
                logger.warning(
                    f"Setup script {setup_script_name} may not be used in Dockerfile"
                )

        dockerfile_path = build_dir / "Dockerfile"
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile)

        if mitm_ca_cert:
            shutil.copy2(mitm_ca_cert, build_dir / "mitm-ca.crt")
            logger.info(f"Injecting MITM CA cert from {mitm_ca_cert}")

        buildargs = get_proxy_env()
        if buildargs:
            logger.info(f"Forwarding proxy build args: {list(buildargs.keys())}")

        logger.info(
            f"Building docker image {image_name} in {build_dir} with platform {platform}"
        )

        buildarg_flags: list[str] = []
        for k, v in buildargs.items():
            buildarg_flags.extend(["--build-arg", f"{k}={v}"])

        nocache_flags = ["--no-cache"] if nocache else []
        builder_flags = _safe_builder_args()

        # Step 1: Build multi-arch OCI tarball for ECR push (non-fatal)
        oci_dir = OCI_IMAGE_DIR / image_name.replace(":", "__")
        oci_dir.mkdir(parents=True, exist_ok=True)
        oci_tar_path = oci_dir / f"{image_name.replace(':', '__')}.tar"

        oci_cmd = [
            "docker",
            "buildx",
            "build",
            *builder_flags,
            "--platform",
            platform,
            "--tag",
            image_name,
            "--output",
            f"type=oci,dest={oci_tar_path}",
            *nocache_flags,
            *buildarg_flags,
            str(build_dir),
        ]
        logger.info(f"Building OCI tarball: {' '.join(oci_cmd)}")
        oci_result = subprocess.run(oci_cmd, capture_output=True, text=True)
        for line in (oci_result.stderr or "").splitlines():
            logger.info(ansi_escape.sub("", line))
        if oci_result.returncode != 0:
            logger.warning(
                f"OCI tarball build failed (non-fatal, skipping ECR export): "
                f"{oci_result.stderr.splitlines()[-1] if oci_result.stderr else 'unknown error'}"
            )
        else:
            logger.info(f"OCI tarball saved to {oci_tar_path}")

        # Step 2: Load native-arch image into local daemon for immediate use
        native = _native_platform()
        load_cmd = [
            "docker",
            "buildx",
            "build",
            *builder_flags,
            "--platform",
            native,
            "--tag",
            image_name,
            "--load",
            *nocache_flags,
            *buildarg_flags,
            str(build_dir),
        ]
        logger.info(f"Loading native image ({native}): {' '.join(load_cmd)}")
        load_result = subprocess.run(load_cmd, capture_output=True, text=True)
        for line in (load_result.stderr or "").splitlines():
            logger.info(ansi_escape.sub("", line))
        if load_result.returncode != 0:
            raise BuildImageError(image_name, load_result.stderr, logger)

        logger.info("Image built successfully!")
    except BuildImageError:
        raise
    except Exception as e:
        logger.error(f"Error building image {image_name}: {e}")
        raise BuildImageError(image_name, str(e), logger) from e
    finally:
        close_logger(logger)


def build_base_images(
    client: docker.DockerClient,
    dataset: list,
    dataset_type: str,
    mitm_ca_cert: Optional[Path] = None,
) -> None:
    """Builds the base images required for the dataset if they do not already exist.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench
        mitm_ca_cert (Path): Pre-resolved MITM CA cert path (or None)

    """
    test_specs = get_specs_from_dataset(dataset, dataset_type, absolute=True)
    base_images = {
        x.base_image_key: (x.base_dockerfile, x.platform) for x in test_specs
    }

    for image_name, (dockerfile, platform) in base_images.items():
        try:
            client.images.get(image_name)
            if mitm_ca_cert:
                print(
                    f"WARNING: Base image {image_name} already exists but MITM CA cert "
                    f"was found at {mitm_ca_cert}. If the cert was added after the base "
                    f"image was built, delete the old image: docker rmi {image_name}"
                )
            else:
                print(f"Base image {image_name} already exists, skipping build.")
            continue
        except docker.errors.ImageNotFound:
            pass
        print(f"Building base image ({image_name})")
        build_image(
            image_name=image_name,
            setup_scripts={},
            dockerfile=dockerfile,
            platform=platform,
            client=client,
            build_dir=BASE_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
            mitm_ca_cert=mitm_ca_cert,
        )
    print("Base images built successfully.")


def get_repo_configs_to_build(
    client: docker.DockerClient, dataset: list, dataset_type: str
) -> dict[str, Any]:
    """Returns a dictionary of image names to build scripts and dockerfiles for repo images.
    Returns only the repo images that need to be built.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench

    """
    image_scripts = dict()
    test_specs = get_specs_from_dataset(dataset, dataset_type, absolute=True)

    for test_spec in test_specs:
        try:
            client.images.get(test_spec.base_image_key)
        except docker.errors.ImageNotFound:
            raise Exception(
                f"Base image {test_spec.base_image_key} not found for {test_spec.repo_image_key}\n."
                "Please build the base images first."
            )

        image_exists = False
        try:
            client.images.get(test_spec.repo_image_key)
            image_exists = True
        except docker.errors.ImageNotFound:
            pass
        if not image_exists:
            image_scripts[test_spec.repo_image_key] = {
                "setup_script": test_spec.setup_script,
                "dockerfile": test_spec.repo_dockerfile,
                "platform": test_spec.platform,
            }
    return image_scripts


def build_repo_images(
    client: docker.DockerClient,
    dataset: list,
    dataset_type: str,
    max_workers: int = 4,
    verbose: int = 1,
) -> tuple[list[str], list[str]]:
    """Builds the repo images required for the dataset if they do not already exist.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench
        max_workers (int): Maximum number of workers to use for building images
        verbose (int): Level of verbosity

    Return:
    ------
        successful: a list of docker image keys for which build were successful
        failed: a list of docker image keys for which build failed

    """
    # Resolve MITM cert ONCE — consistent across all parallel builds
    mitm_ca_cert = _resolve_mitm_ca_cert()
    if mitm_ca_cert:
        print(f"MITM CA cert: {mitm_ca_cert}")
    proxy_env = get_proxy_env()
    if proxy_env:
        print(f"Proxy env vars detected: {list(proxy_env.keys())}")
    if mitm_ca_cert and not proxy_env:
        print(
            "WARNING: MITM CA cert found but no proxy env vars (http_proxy/https_proxy) "
            "are set. The cert will be installed but traffic won't route through a proxy."
        )

    build_base_images(client, dataset, dataset_type, mitm_ca_cert=mitm_ca_cert)
    configs_to_build = get_repo_configs_to_build(client, dataset, dataset_type)
    if len(configs_to_build) == 0:
        print("No repo images need to be built.")
        return [], []
    print(f"Total repo images to build: {len(configs_to_build)}")

    successful, failed = list(), list()
    with tqdm(
        total=len(configs_to_build), smoothing=0, desc="Building repo images"
    ) as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    build_image,
                    image_name,
                    {"setup.sh": config["setup_script"]},
                    config["dockerfile"],
                    config["platform"],
                    client,
                    REPO_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
                    False,  # nocache
                    mitm_ca_cert,
                ): image_name
                for image_name, config in configs_to_build.items()
            }

            for future in as_completed(futures):
                pbar.update(1)
                try:
                    future.result()
                    successful.append(futures[future])
                except BuildImageError as e:
                    print(f"BuildImageError {e.image_name}")
                    traceback.print_exc()
                    failed.append(futures[future])
                    continue
                except Exception:
                    print("Error building image")
                    traceback.print_exc()
                    failed.append(futures[future])
                    continue

    if len(failed) == 0:
        print("All repo images built successfully.")
    else:
        print(f"{len(failed)} repo images failed to build.")

    return successful, failed


__all__ = []
