"""Hostile / boundary tests for commit0.harness.dockerfiles_ts.

Pins down the pre_install safe-prefix warning path, adversarial
pre_install content, and apt-install parsing corner cases.
"""

from __future__ import annotations

import logging

import pytest

from commit0.harness.dockerfiles_ts import get_dockerfile_repo_ts


class TestPreInstallSafePrefixWarning:
    """Any ``pre_install`` command that does not start with a known-safe
    prefix logs a WARNING and is still emitted verbatim as ``RUN <cmd>``.
    This is a trust-on-write: the dataset is considered trusted, but the
    warning must fire so an operator notices supply-chain drift.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /var/lib/apt/lists/*",  # rm not in allowlist
            "sudo apt-get install foo",  # sudo prefix blocks allowlist match
            "./scripts/setup.sh",
            "bash -c 'echo hello'",
            "/usr/local/bin/setup",
            "; ls",
        ],
    )
    def test_unknown_prefix_emits_warning_and_run_line(self, cmd: str, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="commit0.harness.dockerfiles_ts"):
            dockerfile = get_dockerfile_repo_ts(
                base_image="commit0.base.node20:latest", pre_install=[cmd]
            )
        assert f"RUN {cmd}" in dockerfile
        assert any(
            "pre_install command does not match known-safe prefixes" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            "apt-get update",
            "apt install foo",
            "npm install -g lerna",
            "yarn global add typescript",
            "pnpm add -g npm",
            "pip install requests",
            "pip3 install wheel",
            "curl -fsSL https://example.com/install.sh",
            "wget https://example.com",
            "chmod +x /root/setup.sh",
            "mkdir -p /var/cache/foo",
            "ln -s /usr/bin/python3 /usr/local/bin/python",
            "echo 'hi'",
            "export PATH=/usr/local/bin:$PATH",
        ],
    )
    def test_known_safe_prefixes_do_not_warn(self, cmd: str, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="commit0.harness.dockerfiles_ts"):
            get_dockerfile_repo_ts(
                base_image="commit0.base.node20:latest", pre_install=[cmd]
            )
        assert not any(
            "does not match known-safe prefixes" in rec.message
            for rec in caplog.records
        ), caplog.text


class TestAptInstallParsing:
    """Packages from ``apt-get install`` / ``apt install`` are extracted and
    merged into a single consolidated RUN apt-get install line.
    """

    def test_single_apt_get_install(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b",
            pre_install=["apt-get install -y libfoo libbar"],
        )
        # Consolidated line exists and lists both packages
        assert "libfoo" in dockerfile
        assert "libbar" in dockerfile
        # User's -y flag is stripped before merge (harness emits its own -y).
        # So -y appears at most once, on the harness-authored install line.
        apt_install_lines = [
            line for line in dockerfile.splitlines() if "apt-get install" in line
        ]
        # Expect exactly one consolidated apt-get install line
        assert len(apt_install_lines) == 1
        assert "no-install-recommends" in apt_install_lines[0]

    def test_multiple_apt_install_lines_merged(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b",
            pre_install=[
                "apt-get install -y libfoo",
                "apt install libbar libqux",
            ],
        )
        assert "libfoo" in dockerfile
        assert "libbar" in dockerfile
        assert "libqux" in dockerfile

    def test_apt_install_with_only_flags_produces_no_packages(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b", pre_install=["apt-get install -y --fix-missing"]
        )
        # No apt-get update block should appear for zero packages
        assert "apt-get install -y --no-install-recommends" not in dockerfile

    def test_dash_prefixed_tokens_filtered(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b",
            pre_install=["apt-get install -y --no-install-recommends libreal"],
        )
        assert "libreal" in dockerfile
        # The --no-install-recommends token from user input must not be
        # emitted as its own package
        apt_lines = [
            line for line in dockerfile.splitlines() if "apt-get install" in line
        ]
        # The harness-emitted line already has the flag; user's copy is
        # filtered out (dash-prefix filter)
        assert len(apt_lines) == 1


class TestPackagesDeduplication:
    def test_same_native_dep_from_multiple_packages_deduped(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b",
            packages=["sqlite3", "better-sqlite3"],
        )
        # libsqlite3-dev appears exactly once
        assert dockerfile.count("libsqlite3-dev") == 1

    def test_pre_install_apt_and_packages_merged_and_sorted(self) -> None:
        dockerfile = get_dockerfile_repo_ts(
            base_image="b",
            pre_install=["apt-get install -y libfoo"],
            packages=["sharp"],
        )
        # Consolidated line contains both sources
        assert "libfoo" in dockerfile
        assert "libvips-dev" in dockerfile


class TestDockerfileHeaderInvariants:
    """Lightweight smoke tests: the emitted Dockerfile must always contain
    the FROM line, the proxy ARG block, the setup.sh entrypoint, and the
    node_modules sanity check.
    """

    def test_minimum_structure_present(self) -> None:
        dockerfile = get_dockerfile_repo_ts(base_image="commit0.base.node20:latest")
        lines = dockerfile.splitlines()
        assert lines[0] == "FROM commit0.base.node20:latest"
        assert any(line.startswith("ARG http_proxy=") for line in lines)
        assert any("COPY ./setup.sh /root/" in line for line in lines)
        assert any(
            "chmod +x /root/setup.sh && /bin/bash /root/setup.sh" in line
            for line in lines
        )
        assert any("WORKDIR /testbed/" in line for line in lines)
        assert any("node_modules OK" in line for line in lines)
        assert any(".dep-manifest.txt" in line for line in lines)

    @pytest.mark.parametrize(
        "arg_name, default",
        [
            ("http_proxy", '""'),
            ("https_proxy", '""'),
            ("HTTP_PROXY", '""'),
            ("HTTPS_PROXY", '""'),
            ("no_proxy", '"localhost,127.0.0.1,::1"'),
            ("NO_PROXY", '"localhost,127.0.0.1,::1"'),
        ],
    )
    def test_proxy_args_defaults(self, arg_name: str, default: str) -> None:
        dockerfile = get_dockerfile_repo_ts(base_image="b")
        assert f"ARG {arg_name}={default}" in dockerfile
