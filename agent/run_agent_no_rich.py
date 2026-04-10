import os
import time
import yaml
import multiprocessing
from tqdm import tqdm
from git import Repo
from agent.agent_utils import (
    create_branch,
    get_message,
    get_target_edit_files,
    get_changed_files_from_commits,
    update_message_with_dependencies,
    get_lint_cmd,
    read_yaml_config,
)
import subprocess
import sys
import json
from agent.agents import AiderAgents
from typing import cast
from agent.class_types import AgentConfig
from agent.thinking_capture import ThinkingCapture
from commit0.harness.constants import SPLIT
from commit0.harness.get_pytest_ids import main as get_tests
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.utils import load_dataset_from_config
from commit0.cli import read_commit0_config_file
from pathlib import Path
from datetime import datetime
from agent.run_agent import DirContext, run_eval_after_each_commit
import logging

logger = logging.getLogger(__name__)


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ".done").touch()


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    """Return a stable experiment log directory that persists across retries."""
    stable_dir = Path(log_dir) / repo_name / branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


def run_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run Aider for a given repository."""
    # get repo info
    commit0_config = read_commit0_config_file(commit0_config_file)

    assert "commit0" in commit0_config["dataset_name"] or commit0_config[
        "dataset_name"
    ].endswith(".json")
    _, repo_name = example["repo"].split("/")

    # repo_name = repo_name.lower()
    # repo_name = repo_name.replace(".", "-")

    repo_path = os.path.join(repo_base_dir, repo_name)
    repo_path = os.path.abspath(repo_path)

    try:
        local_repo = Repo(repo_path)
    except Exception:
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        )

    if agent_config.agent_name == "aider":
        agent = AiderAgents(
            agent_config.max_iteration,
            agent_config.model_name,
            agent_config.cache_prompts,
        )
    else:
        raise NotImplementedError(
            f"{agent_config.agent_name} is not implemented; please add your implementations in baselines/agents.py."
        )

    thinking_capture = (
        ThinkingCapture() if getattr(agent_config, "capture_thinking", False) else None
    )

    # Check if there are changes in the current branch
    if local_repo.is_dirty():
        # Stage all changes
        local_repo.git.add(A=True)
        # Commit changes with the message "left from last change"
        local_repo.index.commit("left from last change")

    # # if branch_name is not provided, create a new branch name based on agent_config
    # if branch is None:
    #     branch = args2string(agent_config)
    create_branch(local_repo, branch, example["base_commit"])

    # in cases where the latest commit of branch is not commit 0
    # set it back to commit 0
    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
        local_repo.git.reset("--hard", example["base_commit"])

    # get target files to edit and test files to run
    target_edit_files, import_dependencies = get_target_edit_files(
        local_repo,
        example["src_dir"],
        example["test"]["test_dir"],
        branch,
        example["reference_commit"],
        agent_config.use_topo_sort_dependencies,
    )

    lint_files = get_changed_files_from_commits(
        local_repo, "HEAD", example["base_commit"]
    )
    # Call the commit0 get-tests command to retrieve test files
    test_files_str = [xx for x in get_tests(repo_name, verbose=0) for xx in x]
    test_files = sorted(
        list(set([i.split(":")[0] for i in test_files_str if i.strip()]))
    )

    # prepare the log dir — stable across retries (no timestamp)
    experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)
    eval_results = {}

    # write agent_config to .agent.yaml in the log_dir for record
    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    with open(agent_config_log_file, "w") as agent_config_file:
        yaml.dump(agent_config, agent_config_file)

    message = ""

    stage_start_time = time.monotonic()

    with DirContext(repo_path):
        if agent_config is None:
            raise ValueError("Invalid input")

        if agent_config.run_tests:
            for test_file in test_files:
                test_file_name = test_file.replace(".py", "").replace("/", "__")
                test_log_dir = experiment_log_dir / test_file_name

                if _is_module_done(test_log_dir):
                    logger.info(
                        f"Skipping already-completed test module: {test_file_name}"
                    )
                    continue

                test_cmd = f"{sys.executable} -m commit0 test {repo_path} {test_file} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout 100"
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                message = get_message(agent_config, repo_path, test_files=[test_file])

                _ = agent.run(
                    "",
                    test_cmd,
                    lint_cmd,
                    target_edit_files,
                    test_log_dir,
                    test_first=True,
                    thinking_capture=thinking_capture,
                    current_stage="test",
                    current_module=test_file_name,
                )
                _mark_module_done(test_log_dir)

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
        elif agent_config.run_entire_dir_lint:
            message = get_message(agent_config, repo_path, test_files=test_files)
            for lint_file in lint_files:
                lint_file_name = lint_file.replace(".py", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / lint_file_name

                if _is_module_done(lint_log_dir):
                    logger.info(f"Skipping already-linted file: {lint_file_name}")
                    continue

                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )

                _ = agent.run(
                    "",
                    "",
                    lint_cmd,
                    [lint_file],
                    lint_log_dir,
                    lint_first=True,
                    thinking_capture=thinking_capture,
                    current_stage="lint",
                    current_module=lint_file_name,
                )
                _mark_module_done(lint_log_dir)

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
        else:
            message = get_message(agent_config, repo_path, test_files=test_files)

            for f in target_edit_files:
                file_name = f.replace(".py", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name

                if _is_module_done(file_log_dir):
                    logger.info(f"Skipping already-drafted file: {file_name}")
                    continue

                if agent_config.add_import_module_to_context:
                    dependencies = import_dependencies.get(f, [])
                    message = update_message_with_dependencies(message, dependencies)

                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                _ = agent.run(
                    message,
                    "",
                    lint_cmd,
                    [f],
                    file_log_dir,
                    thinking_capture=thinking_capture,
                    current_stage="draft",
                    current_module=file_name,
                )
                _mark_module_done(file_log_dir)

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
    if agent_config.record_test_for_each_commit:
        with open(experiment_log_dir / "eval_results.json", "w") as f:
            json.dump(eval_results, f)

    if thinking_capture is not None:
        try:
            from agent.output_writer import (
                extract_git_patch,
                build_metadata,
            )
            from agent.openhands_formatter import write_openhands_jsonl
            from agent.trajectory_writer import write_trajectory_md

            commit0_config = read_commit0_config_file(commit0_config_file)
            git_patch = extract_git_patch(repo_path, example["base_commit"])

            instance_id = (
                example["instance_id"]
                if "instance_id" in example.keys()
                else f"commit-0/{repo_name}"
            )
            metadata = build_metadata(
                model_name=agent_config.model_name,
                dataset_path=commit0_config.get("dataset_name", ""),
                max_iterations=agent_config.max_iteration,
            )

            stage_runtime = time.monotonic() - stage_start_time

            write_openhands_jsonl(
                output_path=str(experiment_log_dir / "output.jsonl"),
                turns=thinking_capture.turns,
                instance_id=instance_id,
                git_patch=git_patch,
                instruction=message if message else "",
                metadata=metadata,
                metrics=thinking_capture.get_metrics(),
                stage_runtime_seconds=stage_runtime,
            )

            if getattr(agent_config, "trajectory_md", True):
                write_trajectory_md(
                    output_path=experiment_log_dir / "trajectory.md",
                    repo_name=repo_name,
                    turns=thinking_capture.turns,
                )

            logger.info(
                f"Wrote thinking capture: {len(thinking_capture.turns)} turns, "
                f"{thinking_capture.get_metrics()['total_thinking_tokens']} thinking tokens"
            )
        except Exception as e:
            logger.warning(f"Failed to write thinking capture output: {e}")


def run_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Main function to run Aider for a given repository.

    Will run in parallel for each repo.
    """
    config = read_yaml_config(agent_config_file)

    agent_config = AgentConfig(**config)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config["repo_split"]
    if repo_split == "all":
        filtered_dataset = list(dataset)
    elif repo_split in SPLIT:
        filtered_dataset = [
            example
            for example in dataset
            if isinstance(example, dict)
            and "repo" in example
            and isinstance(example["repo"], str)
            and example["repo"].split("/")[-1] in SPLIT[repo_split]
        ]
    else:
        # Custom split not in SPLIT dict — include all entries from the dataset.
        filtered_dataset = list(dataset)
    assert len(filtered_dataset) > 0, (
        f"No examples available for repo_split={repo_split!r}. "
        f"If using a custom dataset, ensure the JSON file is non-empty."
    )

    # if len(filtered_dataset) > 1:
    #     sys.stdout = open(os.devnull, "w")
    if agent_config.add_import_module_to_context:
        # Install Chrome for Playwright for browser-based agents
        try:
            subprocess.run(["playwright", "install", "chromium"], check=True)
            print("Chrome installed successfully for Playwright")
        except subprocess.CalledProcessError as e:
            print(f"Error installing Chrome for Playwright: {e}")
        except FileNotFoundError:
            print("Playwright not found. Make sure it's installed and in your PATH.")

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running Aider for repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []

            # Use apply_async to submit jobs and add progress bar updates
            for example in filtered_dataset:
                result = pool.apply_async(
                    run_agent_for_repo,
                    args=(
                        commit0_config["base_dir"],
                        agent_config,
                        cast(RepoInstance, example),
                        branch,
                        override_previous_changes,
                        backend,
                        log_dir,
                        commit0_config_file,
                    ),
                    callback=lambda _: pbar.update(
                        1
                    ),  # Update progress bar on task completion
                )
                results.append(result)

            for result in results:
                result.get()
