from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_utils_java import (
    _chunk_text,
    _count_tokens,
    _summarize_single_java,
    summarize_specification_java,
)
from agent.thinking_capture import SummarizerCost


def _make_mock_response(content: str | None = "summary") -> MagicMock:
    usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return MagicMock(
        choices=[MagicMock(message=MagicMock(content=content))],
        usage=usage,
    )


def _mock_litellm_module(content: str | None = "summary") -> MagicMock:
    mock = MagicMock()
    mock.completion.return_value = _make_mock_response(content)
    return mock


class TestChunkText:
    def test_single_chunk(self) -> None:
        assert _chunk_text("short text", chunk_size=1000) == ["short text"]

    def test_splits_at_newline(self) -> None:
        text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd\n"
        chunks = _chunk_text(text, chunk_size=25)
        assert len(chunks) >= 2
        for chunk in chunks[:-1]:
            assert chunk.endswith("\n")

    def test_exact_boundary(self) -> None:
        text = "a" * 100
        chunks = _chunk_text(text, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_empty_string(self) -> None:
        assert _chunk_text("", chunk_size=100) == []

    def test_no_newlines(self) -> None:
        text = "a" * 100
        chunks = _chunk_text(text, chunk_size=30)
        assert len(chunks) >= 3
        assert "".join(chunks) == text


class TestCountTokens:
    def test_uses_counter(self) -> None:
        mock_litellm = MagicMock()
        mock_litellm.token_counter.return_value = 42
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            result = _count_tokens("hello world", "test-model")
        assert result == 42

    def test_fallback_on_error(self) -> None:
        mock_litellm = MagicMock()
        mock_litellm.token_counter.side_effect = RuntimeError("no tokenizer")
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            result = _count_tokens("a" * 100, "test-model")
        assert result == 25


class TestSummarizeSingleJava:
    def test_returns_summary(self) -> None:
        llm = _mock_litellm_module("  summary text  ")
        result, cost = _summarize_single_java("spec", "model", 4000, 10000, llm)
        assert result == "summary text"
        assert isinstance(cost, SummarizerCost)

    def test_handles_api_error(self) -> None:
        llm = _mock_litellm_module(None)
        result, cost = _summarize_single_java("spec", "model", 4000, 10000, llm)
        assert result is None
        assert isinstance(cost, SummarizerCost)

    def test_respects_budget(self) -> None:
        llm = _mock_litellm_module("ok")
        _summarize_single_java("spec", "model", 4000, 5000, llm)
        system_msg = llm.completion.call_args.kwargs["messages"][0]["content"]
        assert "5000" in system_msg
        assert "tokens" in system_msg

    def test_empty_text(self) -> None:
        llm = _mock_litellm_module("ok")
        result, cost = _summarize_single_java("", "model", 4000, 10000, llm)
        user_msg = llm.completion.call_args.kwargs["messages"][1]["content"]
        assert "Summarize this specification" in user_msg
        assert result == "ok"


@pytest.fixture()
def mock_litellm() -> MagicMock:
    mock = _mock_litellm_module()
    mock.token_counter = MagicMock(return_value=100)
    mock.completion_cost = MagicMock(return_value=0.001)
    with patch.dict(sys.modules, {"litellm": mock}):
        yield mock


class TestSummarizeSpecificationJava:
    def test_short_single_pass(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = _make_mock_response("condensed")
        result, costs = summarize_specification_java(
            spec_text="A" * 1000, model="m", max_tokens=4000, max_char_length=500
        )
        assert result == "condensed"
        assert mock_litellm.completion.call_count == 1
        assert len(costs) >= 1
        assert all(isinstance(c, SummarizerCost) for c in costs)

    def test_long_chunked(self, mock_litellm: MagicMock) -> None:
        call_idx = {"n": 0}

        def fake(**kwargs: object) -> MagicMock:
            call_idx["n"] += 1
            return _make_mock_response(f"chunk {call_idx['n']}")

        mock_litellm.completion.side_effect = fake
        mock_litellm.token_counter.return_value = 200_000
        large_spec = "word " * 120_000
        result, costs = summarize_specification_java(
            spec_text=large_spec, model="m", max_tokens=4000, max_char_length=10000
        )
        assert mock_litellm.completion.call_count >= 2
        assert len(result) > 0
        assert len(costs) >= 2

    def test_cache_hit(self, mock_litellm: MagicMock, tmp_path: Path) -> None:
        spec = "cached spec"
        cache_key = hashlib.sha256((spec + "m" + "50").encode()).hexdigest()
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "hash": cache_key,
                    "model": "m",
                    "max_char_length": 50,
                    "summary": "cached_result",
                }
            )
        )
        result, costs = summarize_specification_java(
            spec_text=spec,
            model="m",
            max_tokens=4000,
            max_char_length=50,
            cache_path=cache_file,
        )
        assert result == "cached_result"
        assert mock_litellm.completion.call_count == 0
        assert costs == []

    def test_cache_miss_writes(self, mock_litellm: MagicMock, tmp_path: Path) -> None:
        mock_litellm.completion.return_value = _make_mock_response("new_summary")
        cache_file = tmp_path / "cache.json"
        summarize_specification_java(
            spec_text="spec",
            model="m",
            max_tokens=4000,
            max_char_length=50,
            cache_path=cache_file,
        )
        assert cache_file.exists() is True
        cached = json.loads(cache_file.read_text())
        assert cached["summary"] == "new_summary"
        assert "hash" in cached

    def test_fallback_on_error(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.side_effect = RuntimeError("API down")
        spec = "Y" * 5000
        result, costs = summarize_specification_java(
            spec_text=spec, model="m", max_tokens=4000, max_char_length=1000
        )
        assert result == spec[:1000]

    def test_empty_spec(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = _make_mock_response("ok")
        mock_litellm.token_counter.return_value = 0
        result, costs = summarize_specification_java(
            spec_text="", model="m", max_tokens=4000, max_char_length=50
        )
        assert isinstance(result, str)

    def test_consolidation_pass(self, mock_litellm: MagicMock) -> None:
        call_idx = {"n": 0}

        def fake(**kwargs: object) -> MagicMock:
            call_idx["n"] += 1
            return _make_mock_response("chunk_result " * 200)

        mock_litellm.completion.side_effect = fake

        def variable_tokens(**kwargs: object) -> int:
            text = kwargs.get("text", "")
            if isinstance(text, str) and len(text) > 400_000:
                return 200_000
            if isinstance(text, str) and len(text) <= 100:
                return 25
            return 50_000

        mock_litellm.token_counter.side_effect = variable_tokens
        large_spec = "w " * 600_001
        result, costs = summarize_specification_java(
            spec_text=large_spec, model="m", max_tokens=4000, max_char_length=100
        )
        assert len(result) > 0
        assert len(costs) >= 3

    def test_parallel_chunks(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = _make_mock_response("chunk_sum")
        mock_litellm.token_counter.return_value = 200_000
        spec = "w " * 600_001
        result, costs = summarize_specification_java(
            spec_text=spec, model="m", max_tokens=4000, max_char_length=100_000
        )
        assert "chunk_sum" in result
        assert mock_litellm.completion.call_count >= 2

    def test_respects_max_char(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = _make_mock_response("")
        spec = "X" * 2000
        result, costs = summarize_specification_java(
            spec_text=spec, model="m", max_tokens=4000, max_char_length=500
        )
        assert result == spec[:500]
