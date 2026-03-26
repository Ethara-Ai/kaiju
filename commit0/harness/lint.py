import shutil
import subprocess
import sys
import os
from pathlib import Path
from typing import Iterator, Union, List

from commit0.harness.constants import (
    RepoInstance,
)
from commit0.harness.utils import load_dataset_from_config


config = """repos:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.3.0
  hooks:
  - id: check-case-conflict
  - id: mixed-line-ending

- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.6.1
  hooks:
    - id: ruff
      args: [ --fix ]
    - id: ruff-format
"""


def main(
    dataset_name: str,
    dataset_split: str,
    repo_or_repo_dir: str,
    files: Union[List[Path], None],
    base_dir: str,
) -> None:
    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    example = None
    repo_name = None
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_or_repo_dir.endswith("/"):
            repo_or_repo_dir = repo_or_repo_dir[:-1]
        if repo_name in os.path.basename(repo_or_repo_dir):
            break
    assert example is not None, "No example available"
    assert repo_name is not None, "No repo available"

    if files is None:
        repo_dir = os.path.join(base_dir, repo_name)
        if os.path.isdir(repo_or_repo_dir):
            repo = repo_or_repo_dir
        elif os.path.isdir(repo_dir):
            repo = repo_dir
        else:
            raise Exception(
                f"Neither {repo_dir} nor {repo_or_repo_dir} is a valid path.\nUsage: commit0 lint {{repo_or_repo_dir}}"
            )

        files = []
        repo = os.path.join(repo, example["src_dir"])
        for root, dirs, fs in os.walk(repo):
            for file in fs:
                if file.endswith(".py"):
                    files.append(Path(os.path.join(root, file)))

    config_file = Path(".commit0.pre-commit-config.yaml")
    if not config_file.is_file():
        config_file.write_text(config)
    # Find pre-commit executable: prefer venv, then PATH
    pre_commit_bin = os.path.join(os.path.dirname(sys.executable), "pre-commit")
    if not os.path.isfile(pre_commit_bin):
        pre_commit_bin = shutil.which("pre-commit")
    if not pre_commit_bin:
        raise FileNotFoundError(
            "Error: pre-commit command not found. "
            "Ensure it is installed in the active virtual environment."
        )
    command = [pre_commit_bin, "run", "--config", str(config_file), "--files"] + [
        str(f) for f in files
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        print(result.stdout)
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as e:
        print(e.output)
        sys.exit(e.returncode)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Error running pre-commit: {e}") from e
    except Exception as e:
        raise Exception(f"An unexpected error occurred: {e}")


__all__ = []
