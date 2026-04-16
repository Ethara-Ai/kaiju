import logging
import sys

import docker
from typing import Iterator, Union

from commit0.harness.constants import RepoInstance, SimpleInstance, SPLIT
from commit0.harness.docker_build import build_repo_images
from commit0.harness.health_check import run_health_checks
from commit0.harness.spec import make_spec
from commit0.harness.utils import load_dataset_from_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    split: str,
    num_workers: int,
    verbose: int,
) -> None:
    dataset: Iterator[Union[RepoInstance, SimpleInstance]] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    specs = []
    dataset_name = dataset_name.lower()
    if "swe" in dataset_name:
        dataset_type = "swebench"
    elif (
        "humaneval" in dataset_name
        or "mbpp" in dataset_name
        or "bigcodebench" in dataset_name
        or "codecontests" in dataset_name
    ):
        dataset_type = "simple"
    else:
        dataset_type = "commit0"
    for example in dataset:
        if "swe" in dataset_name or dataset_type == "simple":
            if split != "all" and split not in example["instance_id"]:
                continue
        else:
            repo_name = example["repo"].split("/")[-1]
            if split != "all":
                if split in SPLIT:
                    if repo_name not in SPLIT[split]:
                        continue
                else:
                    pass
        spec = make_spec(example, dataset_type, absolute=True)
        specs.append(spec)

    client = docker.from_env()
    successful, failed = build_repo_images(
        client, specs, dataset_type, num_workers, verbose
    )

    health_failures: list[str] = []
    for spec in specs:
        image_key = spec.repo_image_key
        if image_key in failed:
            continue
        setup = spec._get_setup_dict()
        pip_packages = setup.get("pip_packages", [])
        python_version = setup.get("python")
        results = run_health_checks(
            client, image_key, pip_packages=pip_packages, python_version=python_version
        )
        for passed, check_name, detail in results:
            if not passed:
                logger.error(
                    "Health check FAILED [%s] for %s: %s", check_name, image_key, detail
                )
                health_failures.append(image_key)
            else:
                logger.info(
                    "Health check passed [%s] for %s: %s", check_name, image_key, detail
                )

    all_failed = list(failed) + health_failures
    if all_failed:
        logger.error(
            "Failed to build or verify %d image(s): %s",
            len(all_failed),
            all_failed,
        )
        sys.exit(1)


__all__ = []
