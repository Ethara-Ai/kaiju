"""Hostile / boundary tests for small helpers in the TS run pipeline.

These hit behaviour not covered by the existing line-coverage suites:

* ``commit0.harness.run_ts_tests._inject_test_ids`` — append semantics,
  multiple matching lines, newline-bearing test IDs, shell metachars.
* ``commit0.harness.build_ts._filter_by_split`` — case sensitivity,
  underscore/hyphen normalisation, unknown splits, non-dict examples.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from commit0.harness.build_ts import _filter_by_split
from commit0.harness.run_ts_tests import _inject_test_ids


# ---------------------------------------------------------------------------
# _inject_test_ids
# ---------------------------------------------------------------------------


class TestInjectTestIdsBoundary:
    def test_empty_test_ids_returns_identical_string(self) -> None:
        script = "#!/bin/bash\nnpx jest --forceExit\n"
        out = _inject_test_ids(script, "")
        assert out == script

    @pytest.mark.parametrize(
        "test_id",
        [
            "src/foo.test.ts",
            "path with space.test.ts",
            "src/dir/bar.spec.ts",
            # Unicode
            "テスト.test.ts",
        ],
    )
    def test_appends_to_forceexit_line(self, test_id: str) -> None:
        script = "#!/bin/bash\nnpx jest --forceExit\n"
        out = _inject_test_ids(script, test_id)
        assert f"npx jest --forceExit {test_id}" in out

    def test_appends_to_vitest_line(self) -> None:
        script = "#!/bin/bash\nnpx vitest run\n"
        out = _inject_test_ids(script, "src/foo.test.ts")
        assert "npx vitest run src/foo.test.ts" in out

    def test_appends_to_every_matching_line(self) -> None:
        """If two lines match, both are modified. Matches real Jest fallback
        scripts that double-invoke the runner.
        """
        script = "#!/bin/bash\nnpx jest --forceExit\nnpx vitest run\n"
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit t.ts" in out
        assert "npx vitest run t.ts" in out

    def test_no_matching_line_returns_unchanged(self) -> None:
        script = "#!/bin/bash\necho hello\n"
        out = _inject_test_ids(script, "test.ts")
        assert out == script

    def test_preserves_non_matching_lines_verbatim(self) -> None:
        script = "#!/bin/bash\necho hi\nnpx jest --forceExit\necho bye\n"
        out = _inject_test_ids(script, "t.ts")
        lines = out.splitlines()
        assert lines[0] == "#!/bin/bash"
        assert lines[1] == "echo hi"
        assert lines[2] == "npx jest --forceExit t.ts"
        assert lines[3] == "echo bye"

    def test_rstrips_existing_trailing_whitespace_on_matching_line(self) -> None:
        """Jest line with trailing whitespace keeps a single separator space."""
        script = "npx jest --forceExit   \n"
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit t.ts" in out
        # No double-space
        assert "jest --forceExit  t.ts" not in out

    @pytest.mark.parametrize(
        "test_ids",
        [
            "a.test.ts b.test.ts",
            "a.test.ts\tb.test.ts",
            # Attacker-controlled test ID — the injector does NOT quote; the
            # invariant is only that it is appended verbatim. If the test ID
            # contains shell metachars they reach bash verbatim. This is
            # intentional because test IDs originate from the harness's own
            # generate_test_ids_ts, which sanitises them.
            "a.test.ts;echo pwned",
        ],
    )
    def test_appends_multiple_or_metachar_ids_verbatim(self, test_ids: str) -> None:
        script = "npx jest --forceExit\n"
        out = _inject_test_ids(script, test_ids)
        assert f"npx jest --forceExit {test_ids}" in out


def test_newline_in_test_ids_does_not_inject_extra_line() -> None:
    script = "npx jest --forceExit\n"
    out = _inject_test_ids(script, "a.test.ts\necho pwned")
    # Desired behaviour: newlines in test_ids are sanitised to spaces so no
    # bare `echo pwned` line exists in the emitted eval script.
    for line in out.splitlines():
        assert line.strip() != "echo pwned"


# ---------------------------------------------------------------------------
# _filter_by_split
# ---------------------------------------------------------------------------


class TestFilterBySplitBoundary:
    """_filter_by_split(example, split) returns True if example belongs to
    the requested split. Behaviour:

    * 'all' / 'all_ts' → True always
    * split in TS_SPLIT → repo_name in that list (empty list = all)
    * otherwise → normalize by replacing '-' with '_' and compare names
    """

    @pytest.mark.parametrize(
        "split",
        ["all", "all_ts"],
    )
    def test_all_splits_accept_anything(self, split: str) -> None:
        assert _filter_by_split({"repo": "random/weird-name"}, split) is True
        # Also a non-dict example
        ns = SimpleNamespace(repo="x/y")
        assert _filter_by_split(ns, split) is True

    def test_named_split_in_ts_split_but_empty_list_means_all(
        self, monkeypatch
    ) -> None:
        from commit0.harness import build_ts

        monkeypatch.setitem(build_ts.TS_SPLIT, "__test_empty__", [])
        assert _filter_by_split({"repo": "o/anything"}, "__test_empty__") is True

    def test_named_split_with_non_matching_repo_rejected(self, monkeypatch) -> None:
        from commit0.harness import build_ts

        monkeypatch.setitem(build_ts.TS_SPLIT, "__test_fixed__", ["zod"])
        assert _filter_by_split({"repo": "o/hono"}, "__test_fixed__") is False
        assert _filter_by_split({"repo": "o/zod"}, "__test_fixed__") is True

    @pytest.mark.parametrize(
        "split, repo, expected",
        [
            # exact normalized match
            ("my-repo", "org/my-repo", True),
            ("my_repo", "org/my-repo", True),
            ("my-repo", "org/my_repo", True),
            ("my_repo", "org/my_repo", True),
            # case sensitivity: implementation lowercase? No — normalisation
            # only replaces hyphens/underscores. Mixed case must NOT match.
            ("My-Repo", "org/my-repo", False),
            ("my-repo", "org/My-Repo", False),
            # boundary: no match against a different name
            ("zod", "org/hono", False),
            # empty split string never matches a real name
            ("", "org/my-repo", False),
            # Unicode repo name against normalised ASCII split
            ("emoji-repo", "org/emoji-repo", True),
        ],
    )
    def test_fallthrough_normalization(
        self, split: str, repo: str, expected: bool
    ) -> None:
        assert _filter_by_split({"repo": repo}, split) is expected

    def test_non_dict_example_with_repo_attribute(self) -> None:
        ns = SimpleNamespace(repo="org/zod")
        assert _filter_by_split(ns, "zod") is True

    def test_non_dict_example_no_match(self) -> None:
        ns = SimpleNamespace(repo="org/nothing-like-that")
        assert _filter_by_split(ns, "zod") is False

    def test_dict_without_repo_falls_through_to_empty_name(self) -> None:
        # repo_full='' → repo_name=''; '' == split normalised only if split==''
        assert _filter_by_split({}, "") is True
        assert _filter_by_split({}, "zod") is False

    def test_repo_name_is_last_slash_segment(self) -> None:
        # Multi-segment repo path: only the last segment matches the split
        assert _filter_by_split({"repo": "owner/sub/group/zod"}, "zod") is True

    def test_whitespace_in_split_does_not_match(self) -> None:
        assert _filter_by_split({"repo": "org/zod"}, "  zod  ") is False
