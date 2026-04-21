"""Go agent runner for commit0.

Mirrors run_agent.py but uses Go-specific splits, test IDs, utilities,
and agent configuration. Orchestrates parallel agent execution across
Go repositories.
"""

import logging
import multiprocessing
import os
import queue
import subprocess
import sys
import time
import json
import yaml
from pathlib import Path
from typing import Optional, cast
from types import TracebackType
from datetime import datetime

import git

from agent.agents_go import AiderGoAgents
from agent.agent_utils_go import (
    collect_go_test_files,
    create_branch,
    get_go_lint_cmd,
    get_go_message,
    get_target_edit_files,
    load_agent_config,
)
from agent.class_types import AgentConfig
from agent.display import TerminalDisplay
from commit0.harness.constants import RepoInstance
from commit0.harness.constants_go import (
    GO_SPLIT,
    GO_SPLIT_ALL,
    RUN_GO_TEST_LOG_DIR,
)
from commit0.harness.get_go_test_ids import main as get_go_test_ids
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)

RUN_AGENT_LOG_DIR = Path("logs/agent_go")


def _read_commit0_go_config(config_file: str) -> dict:
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class DirContext:
    def __init__(self, d: str):
        self.dir = d
        self.cwd = os.getcwd()

    def __enter__(self):
        os.chdir(self.dir)

    def __exit__(
        self,
        exctype: Optional[type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        os.chdir(self.cwd)


def run_eval_after_each_commit(
    branch: str, backend: str, commit0_config_file: str
) -> str:
    eval_cmd = f"{sys.executable} commit0/cli_go.py evaluate --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout 100"
    try:
        result = subprocess.run(
            eval_cmd.split(), capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error("Error running eval command: %s", e, exc_info=True)
        return e.stdout if e.stdout else str(e)


def run_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: dict,
    branch: str,
    update_queue: multiprocessing.Queue,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    _, repo_name = example["repo"].split("/")

    update_queue.put(("start_repo", (repo_name, 0)))

    repo_path = os.path.join(repo_base_dir, repo_name)
    repo_path = os.path.abspath(repo_path)

    try:
        local_repo = git.Repo(repo_path)
    except Exception:
        logger.error(
            "Failed to open repo at %s: not a git repo", repo_path, exc_info=True
        )
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        ) from None

    agent = AiderGoAgents(
        agent_config.max_iteration,
        agent_config.model_name,
        agent_config.cache_prompts,
    )

    if local_repo.is_dirty():
        logger.warning("Auto-committing uncommitted changes in %s", repo_path)
        local_repo.git.add(A=True)
        local_repo.index.commit("left from last change")

    create_branch(local_repo, branch, override=override_previous_changes)

    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
        logger.warning(
            "Resetting %s to base commit %s (override_previous_changes=True)",
            repo_name,
            example["base_commit"],
        )
        local_repo.git.reset("--hard", example["base_commit"])

    src_dir = example.get("src_dir", ".")
    reference_commit = example.get("reference_commit", "HEAD")

    target_edit_files = get_target_edit_files(
        repo_path, src_dir, branch, reference_commit
    )
    test_files = collect_go_test_files(repo_path)
    logger.info("Found %d target edit files for %s", len(target_edit_files), repo_name)

    test_files_str = [xx for x in get_go_test_ids(repo_name, verbose=0) for xx in x]

    experiment_log_dir = (
        Path(log_dir)
        / repo_name
        / branch
        / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    )
    experiment_log_dir.mkdir(parents=True, exist_ok=True)

    eval_results = {}

    agent_config_log_file = experiment_log_dir / ".agent.go.yaml"
    try:
        with open(agent_config_log_file, "w") as acf:
            yaml.dump(agent_config, acf)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    with DirContext(repo_path):
        if agent_config.run_tests:
            update_queue.put(("start_repo", (repo_name, len(test_files_str))))
            for test_id in test_files_str:
                if not test_id.strip():
                    continue
                update_queue.put(("set_current_file", (repo_name, test_id)))
                test_cmd = f"{sys.executable} commit0/cli_go.py test {repo_path} {test_id} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout 100"
                test_id_safe = test_id.replace("/", "__").replace(".", "_")
                test_log_dir = experiment_log_dir / test_id_safe
                lint_cmd = (
                    get_go_lint_cmd(
                        _read_commit0_go_config(commit0_config_file).get(
                            "dataset_name", ""
                        ),
                        _read_commit0_go_config(commit0_config_file).get(
                            "dataset_split", "test"
                        ),
                        repo_name,
                        repo_base_dir,
                    )
                    if agent_config.use_lint_info
                    else ""
                )
                message = get_go_message(
                    agent_config,
                    repo_path,
                    test_files,
                )

                agent_return = agent.run(
                    message,
                    test_cmd,
                    lint_cmd,
                    target_edit_files,
                    str(test_log_dir),
                    test_first=True,
                )
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, test_id, agent_return.last_cost),
                    )
                )
        elif agent_config.run_entire_dir_lint:
            lint_cmd = get_go_lint_cmd(
                _read_commit0_go_config(commit0_config_file).get("dataset_name", ""),
                _read_commit0_go_config(commit0_config_file).get(
                    "dataset_split", "test"
                ),
                repo_name,
                repo_base_dir,
            )
            update_queue.put(("start_repo", (repo_name, len(target_edit_files))))
            for edit_file in target_edit_files:
                update_queue.put(("set_current_file", (repo_name, edit_file)))
                file_name = edit_file.replace(".go", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / file_name

                agent_return = agent.run(
                    "",
                    "",
                    lint_cmd,
                    [edit_file],
                    str(lint_log_dir),
                    lint_first=True,
                )
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, edit_file, agent_return.last_cost),
                    )
                )
        else:
            message = get_go_message(
                agent_config,
                repo_path,
                test_files,
            )

            update_queue.put(("start_repo", (repo_name, len(target_edit_files))))
            for f in target_edit_files:
                update_queue.put(("set_current_file", (repo_name, f)))
                file_name = f.replace(".go", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name
                lint_cmd = (
                    get_go_lint_cmd(
                        _read_commit0_go_config(commit0_config_file).get(
                            "dataset_name", ""
                        ),
                        _read_commit0_go_config(commit0_config_file).get(
                            "dataset_split", "test"
                        ),
                        repo_name,
                        repo_base_dir,
                    )
                    if agent_config.use_lint_info
                    else ""
                )
                agent_return = agent.run(message, "", lint_cmd, [f], str(file_log_dir))
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, f, agent_return.last_cost),
                    )
                )

    if agent_config.record_test_for_each_commit:
        try:
            with open(experiment_log_dir / "eval_results.json", "w") as f:
                json.dump(eval_results, f)
        except OSError as e:
            logger.error(
                "Failed to write eval results to %s: %s",
                experiment_log_dir / "eval_results.json",
                e,
            )
            raise

    update_queue.put(("finish_repo", repo_name))


def run_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
    display_repo_progress_num: int,
) -> None:
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    config = _read_commit0_go_config(commit0_config_file)

    dataset = load_dataset_from_config(
        config["dataset_name"], split=config["dataset_split"]
    )
    repo_split = config["repo_split"]
    if repo_split == "all":
        filtered_dataset = list(dataset)
    elif repo_split in GO_SPLIT:
        filtered_dataset = [
            example
            for example in dataset
            if isinstance(example, dict)
            and "repo" in example
            and isinstance(example["repo"], str)
            and example["repo"].split("/")[-1] in GO_SPLIT[repo_split]
        ]
    else:
        filtered_dataset = [
            example
            for example in dataset
            if isinstance(example, dict)
            and "repo" in example
            and isinstance(example["repo"], str)
            and example["repo"].split("/")[-1].replace("-", "_")
            == repo_split.replace("-", "_")
        ]
        if not filtered_dataset:
            filtered_dataset = list(dataset)
    assert len(filtered_dataset) > 0, (
        f"No examples available for repo_split={repo_split!r}. "
        f"If using a custom dataset, ensure the JSON file is non-empty."
    )

    with TerminalDisplay(len(filtered_dataset)) as display:
        not_started_repos = [
            example["repo"].split("/")[-1] for example in filtered_dataset
        ]
        display.set_not_started_repos(not_started_repos)

        start_time = time.time()

        display.update_repo_progress_num(
            min(display_repo_progress_num, max_parallel_repos)
        )
        display.update_backend_display(backend)
        display.update_log_dir_display(log_dir)
        display.update_agent_display(
            agent_config.agent_name,
            agent_config.model_name,
            agent_config.run_tests,
            agent_config.use_topo_sort_dependencies,
            agent_config.use_repo_info,
            agent_config.use_unit_tests_info,
            agent_config.use_spec_info,
            agent_config.use_lint_info,
        )
        display.update_branch_display(branch)

        with multiprocessing.Manager() as manager:
            update_queue = manager.Queue()
            with multiprocessing.Pool(processes=max_parallel_repos) as pool:
                results = []

                for example in filtered_dataset:
                    result = pool.apply_async(
                        run_agent_for_repo,
                        args=(
                            config["base_dir"],
                            agent_config,
                            example,
                            branch,
                            update_queue,
                            override_previous_changes,
                            backend,
                            log_dir,
                            commit0_config_file,
                        ),
                    )
                    results.append(result)

                last_time_update = 0.0
                while any(not r.ready() for r in results):
                    try:
                        while not update_queue.empty():
                            action, data = update_queue.get_nowait()
                            if action == "start_repo":
                                repo_name, total_files = data
                                display.start_repo(repo_name, total_files)
                            elif action == "finish_repo":
                                repo_name = data
                                display.finish_repo(repo_name)
                            elif action == "set_current_file":
                                repo_name, file_name = data
                                display.set_current_file(repo_name, file_name)
                            elif action == "update_money_display":
                                repo_name, file_name, money_spent = data
                                display.update_money_display(
                                    repo_name, file_name, money_spent
                                )
                    except queue.Empty:
                        logger.debug("Queue empty, waiting for worker updates")

                    current_time = time.time()
                    if current_time - last_time_update >= 1:
                        elapsed_time = int(current_time - start_time)
                        display.update_time_display(elapsed_time)
                        last_time_update = current_time

                    time.sleep(0.1)

                while not update_queue.empty():
                    action, data = update_queue.get()
                    if action == "start_repo":
                        repo_name, total_files = data
                        display.start_repo(repo_name, total_files)
                    elif action == "finish_repo":
                        repo_name = data
                        display.finish_repo(repo_name)
                    elif action == "set_current_file":
                        repo_name, file_name = data
                        display.set_current_file(repo_name, file_name)
                    elif action == "update_money_display":
                        repo_name, file_name, money_spent = data
                        display.update_money_display(repo_name, file_name, money_spent)

                elapsed_time = int(time.time() - start_time)
                display.update_time_display(elapsed_time)

                for result in results:
                    result.get()
                logger.info("All %d agent workers completed", len(results))
