from __future__ import annotations

import json
from unittest.mock import MagicMock


import docker.errors

from commit0.harness.health_check_ts import (
    check_node_modules,
    check_node_version,
    check_require,
    run_ts_health_checks,
)


def _mock_client(
    output: bytes = b"", side_effect: Exception | None = None
) -> MagicMock:
    client = MagicMock(spec=["containers"])
    if side_effect:
        client.containers.run.side_effect = side_effect
    else:
        client.containers.run.return_value = output
    return client


class TestCheckNodeModules:
    def test_positive_count(self) -> None:
        client = _mock_client(json.dumps({"count": 42}).encode())
        ok, detail = check_node_modules(client, "img:v1")
        assert ok is True
        assert "42" in detail

    def test_zero_count(self) -> None:
        client = _mock_client(json.dumps({"count": 0}).encode())
        ok, detail = check_node_modules(client, "img:v1")
        assert ok is False
        assert "empty" in detail

    def test_negative_count(self) -> None:
        client = _mock_client(json.dumps({"count": -1, "error": "ENOENT"}).encode())
        ok, detail = check_node_modules(client, "img:v1")
        assert ok is False
        assert "missing" in detail.lower() or "ENOENT" in detail

    def test_exception_returns_false(self) -> None:
        client = _mock_client(side_effect=RuntimeError("boom"))
        ok, detail = check_node_modules(client, "img:v1")
        assert ok is False
        assert "error" in detail.lower()


class TestCheckNodeVersion:
    def test_matching_version(self) -> None:
        client = _mock_client(b"20\n")
        ok, detail = check_node_version(client, "img:v1", "20")
        assert ok is True
        assert "20" in detail

    def test_mismatched_version(self) -> None:
        client = _mock_client(b"18\n")
        ok, detail = check_node_version(client, "img:v1", "20")
        assert ok is False
        assert "Expected" in detail

    def test_exception_returns_false(self) -> None:
        client = _mock_client(side_effect=RuntimeError("connection lost"))
        ok, detail = check_node_version(client, "img:v1", "20")
        assert ok is False
        assert "error" in detail.lower()


class TestCheckRequire:
    def test_successful_require(self) -> None:
        client = _mock_client(b"")
        ok, detail = check_require(client, "img:v1", "express")
        assert ok is True
        assert "OK" in detail

    def test_types_package_skipped(self) -> None:
        client = _mock_client()
        ok, detail = check_require(client, "img:v1", "@types/node")
        assert ok is True
        assert "Skipped" in detail
        client.containers.run.assert_not_called()

    def test_container_error_returns_false(self) -> None:
        err = docker.errors.ContainerError(
            container="c", exit_status=1, command="cmd", image="img", stderr=b"err"
        )
        client = _mock_client(side_effect=err)
        ok, detail = check_require(client, "img:v1", "nonexistent")
        assert ok is False
        assert "failed" in detail

    def test_generic_exception_returns_false(self) -> None:
        client = _mock_client(side_effect=RuntimeError("docker gone"))
        ok, detail = check_require(client, "img:v1", "express")
        assert ok is False
        assert "error" in detail.lower()


class TestRunTsHealthChecks:
    def test_node_modules_always_checked(self) -> None:
        client = _mock_client(json.dumps({"count": 10}).encode())
        results = run_ts_health_checks(client, "img:v1")
        assert len(results) >= 1
        assert results[0][1] == "node_modules"

    def test_node_version_checked_when_provided(self) -> None:
        client = _mock_client(json.dumps({"count": 10}).encode())
        client.containers.run.side_effect = [
            json.dumps({"count": 10}).encode(),
            b"20\n",
        ]
        results = run_ts_health_checks(client, "img:v1", node_version="20")
        names = [r[1] for r in results]
        assert "node_version" in names

    def test_packages_checked(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.side_effect = [
            json.dumps({"count": 10}).encode(),
            b"",
            b"",
        ]
        results = run_ts_health_checks(client, "img:v1", packages=["express", "lodash"])
        names = [r[1] for r in results]
        assert "require:express" in names
        assert "require:lodash" in names

    def test_types_packages_skipped_in_require(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.side_effect = [
            json.dumps({"count": 5}).encode(),
        ]
        results = run_ts_health_checks(client, "img:v1", packages=["@types/node"])
        names = [r[1] for r in results]
        assert not any("@types" in n for n in names)

    def test_no_optional_args_returns_only_node_modules(self) -> None:
        client = _mock_client(json.dumps({"count": 3}).encode())
        results = run_ts_health_checks(client, "img:v1")
        assert len(results) == 1
        assert results[0][1] == "node_modules"
