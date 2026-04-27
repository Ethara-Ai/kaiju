"""TypeScript build orchestrator — co-located alongside build.py."""

import logging
import sys
from typing import Union

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_ts import DEFAULT_NODE_VERSION, TS_SPLIT, TsRepoInstance
from commit0.harness.docker_build import build_repo_images
from commit0.harness.health_check_ts import run_ts_health_checks
from commit0.harness.spec_ts import Commit0TsSpec, make_ts_spec
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


def _filter_by_split(
    example: Union[TsRepoInstance, RepoInstance, dict],
    split: str,
) -> bool:
    """Filter a dataset entry by TS split."""
    if split == "all" or split == "all_ts":
        return True
    if isinstance(example, dict):
        repo_full = example.get("repo", "")
    else:
        repo_full = example.repo
    repo_name = repo_full.split("/")[-1]
    if split in TS_SPLIT:
        split_repos = TS_SPLIT[split]
        if not split_repos:
            return True  # empty = all
        return repo_name in split_repos
    return repo_name.replace("-", "_") == split.replace("-", "_")


def main(
    dataset_name: str,
    dataset_split: str,
    split: str,
    num_workers: int,
    verbose: int,
) -> None:
    """Build Docker images for TypeScript repos using polymorphic Spec reuse."""
    dataset = load_dataset_from_config(dataset_name, split=dataset_split)

    specs: list[Commit0TsSpec] = []
    for example in dataset:
        if not _filter_by_split(example, split):
            continue
        specs.append(make_ts_spec(example, absolute=True))

    if not specs:
        logger.warning("No TS repos matched split '%s'. Nothing to build.", split)
        return

    import docker

    logger.info("Building %d TS repo image(s) for split '%s'", len(specs), split)
    client = docker.from_env()

    try:
        # Polymorphic reuse: get_specs_from_dataset() at spec.py:248 detects
        # isinstance(dataset[0], Spec) and returns specs directly — no re-routing.
        successful, failed = build_repo_images(
            client, specs, "commit0", num_workers, verbose
        )

        # Non-blocking health checks
        for spec in specs:
            image_key = spec.repo_image_key
            if image_key in failed:
                continue
            setup = spec._get_setup_dict()
            results = run_ts_health_checks(
                client,
                image_key,
                node_version=setup.get("node", DEFAULT_NODE_VERSION),
                packages=setup.get("packages"),
            )
            for passed, check_name, detail in results:
                if not passed:
                    logger.warning(
                        "Health check FAILED [%s] for %s: %s",
                        check_name,
                        image_key,
                        detail,
                    )
                else:
                    logger.info(
                        "Health check passed [%s] for %s: %s",
                        check_name,
                        image_key,
                        detail,
                    )
    finally:
        client.close()

    if failed:
        logger.error("Failed to build %d image(s): %s", len(failed), list(failed))
        sys.exit(1)
