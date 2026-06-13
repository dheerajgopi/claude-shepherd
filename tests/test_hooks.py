"""Exhaustive tests for tdd_hooks — the mechanical path-policy boundary (§9/§10)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

tdd_hooks = pytest.importorskip("tdd_hooks")  # parallel track (T1-HOOKS)

from tdd_contracts import PathPolicyMode, WRITE_TOOLS  # noqa: E402
from tdd_hooks import PathPolicy, is_path_allowed, make_pretooluse_hook  # noqa: E402

REQUIREMENTS = ".shepherd/features/user-auth/requirements"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for d in ("tests", "src", "tests-extra", REQUIREMENTS):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def allow_policy(repo: Path) -> PathPolicy:
    return PathPolicy(
        mode=PathPolicyMode.ALLOW_ONLY, paths=["tests", REQUIREMENTS], repo_root=repo
    )


@pytest.fixture
def deny_policy(repo: Path) -> PathPolicy:
    return PathPolicy(
        mode=PathPolicyMode.DENY_UNDER, paths=["tests", REQUIREMENTS], repo_root=repo
    )


def _denied_flag(event) -> bool:
    if isinstance(event, dict):
        return bool(event["denied"])
    return bool(event.denied)


class TestAllowOnly:
    @pytest.mark.parametrize(
        "rel", ["tests/test_login.py", "tests/sub/test_deep.py", REQUIREMENTS + "/auth.md"]
    )
    def test_allows_under_each_listed_path(self, repo, allow_policy, rel) -> None:
        allowed, reason = is_path_allowed(
            "Write", {"file_path": str(repo / rel)}, allow_policy
        )
        assert allowed, reason

    @pytest.mark.parametrize("rel", ["src/x.py", "README.md", "setup.py"])
    def test_denies_outside(self, repo, allow_policy, rel) -> None:
        allowed, reason = is_path_allowed(
            "Write", {"file_path": str(repo / rel)}, allow_policy
        )
        assert not allowed
        assert reason  # non-empty
        name = Path(rel).name
        assert name in reason or rel in reason or str(repo / rel) in reason

    def test_denies_dotdot_escape(self, repo, allow_policy) -> None:
        # Lexically under tests/, resolves outside it.
        allowed, reason = is_path_allowed(
            "Write", {"file_path": "tests/../src/x.py"}, allow_policy
        )
        assert not allowed
        assert reason

    def test_denies_absolute_dotdot_escape(self, repo, allow_policy) -> None:
        allowed, _ = is_path_allowed(
            "Write", {"file_path": str(repo / "tests" / ".." / "src" / "x.py")},
            allow_policy,
        )
        assert not allowed

    def test_relative_and_absolute_equivalent(self, repo, allow_policy) -> None:
        rel_ok, _ = is_path_allowed(
            "Write", {"file_path": "tests/test_a.py"}, allow_policy
        )
        abs_ok, _ = is_path_allowed(
            "Write", {"file_path": str(repo / "tests" / "test_a.py")}, allow_policy
        )
        assert rel_ok is True and abs_ok is True

        rel_bad, _ = is_path_allowed(
            "Write", {"file_path": "src/x.py"}, allow_policy
        )
        abs_bad, _ = is_path_allowed(
            "Write", {"file_path": str(repo / "src" / "x.py")}, allow_policy
        )
        assert rel_bad is False and abs_bad is False

    def test_segment_aware_prefix(self, repo, allow_policy) -> None:
        # "tests-extra" must NOT match policy path "tests".
        allowed, _ = is_path_allowed(
            "Write", {"file_path": str(repo / "tests-extra" / "x.py")}, allow_policy
        )
        assert not allowed

    @pytest.mark.parametrize("tool", [t for t in WRITE_TOOLS if t != "NotebookEdit"])
    def test_all_write_tools_enforced(self, repo, allow_policy, tool) -> None:
        allowed, _ = is_path_allowed(
            tool, {"file_path": str(repo / "src" / "x.py")}, allow_policy
        )
        assert not allowed

    def test_notebook_edit_denied_outside(self, repo, allow_policy) -> None:
        allowed, _ = is_path_allowed(
            "NotebookEdit",
            {"notebook_path": str(repo / "src" / "x.ipynb")},
            allow_policy,
        )
        assert not allowed

    def test_missing_path_key_denied(self, allow_policy) -> None:
        allowed, reason = is_path_allowed("Write", {}, allow_policy)
        assert not allowed
        assert reason


class TestDenyUnder:
    @pytest.mark.parametrize("rel", ["src/x.py", "README.md", "lib/util.py"])
    def test_allows_outside_listed_paths(self, repo, deny_policy, rel) -> None:
        allowed, reason = is_path_allowed(
            "Write", {"file_path": str(repo / rel)}, deny_policy
        )
        assert allowed, reason

    @pytest.mark.parametrize(
        "rel", ["tests/test_login.py", "tests/sub/x.py", REQUIREMENTS + "/auth.md"]
    )
    def test_denies_under_each_listed_path(self, repo, deny_policy, rel) -> None:
        allowed, reason = is_path_allowed(
            "Write", {"file_path": str(repo / rel)}, deny_policy
        )
        assert not allowed
        assert reason
        name = Path(rel).name
        assert name in reason or rel in reason or str(repo / rel) in reason

    def test_denies_dotdot_sneak_into_tests(self, repo, deny_policy) -> None:
        # Lexically under src/, resolves into tests/.
        allowed, _ = is_path_allowed(
            "Write", {"file_path": "src/../tests/test_x.py"}, deny_policy
        )
        assert not allowed

    def test_segment_aware_sibling_allowed(self, repo, deny_policy) -> None:
        allowed, _ = is_path_allowed(
            "Write", {"file_path": str(repo / "tests-extra" / "x.py")}, deny_policy
        )
        assert allowed


class TestGlobClassifier:
    """Co-located test layouts (Go/JS) the directory model can't express."""

    @pytest.fixture
    def go_repo(self, tmp_path: Path) -> Path:
        for d in ("pkg", "cmd"):
            (tmp_path / d).mkdir(parents=True)
        return tmp_path

    def test_allow_only_permits_colocated_go_test(self, go_repo) -> None:
        policy = PathPolicy(
            mode=PathPolicyMode.ALLOW_ONLY, paths=["**/*_test.go"], repo_root=go_repo
        )
        ok, _ = is_path_allowed(
            "Write", {"file_path": str(go_repo / "pkg" / "svc_test.go")}, policy
        )
        assert ok

    def test_allow_only_denies_go_source(self, go_repo) -> None:
        policy = PathPolicy(
            mode=PathPolicyMode.ALLOW_ONLY, paths=["**/*_test.go"], repo_root=go_repo
        )
        ok, reason = is_path_allowed(
            "Write", {"file_path": str(go_repo / "pkg" / "svc.go")}, policy
        )
        assert not ok and reason

    def test_deny_under_protects_go_test_but_allows_source(self, go_repo) -> None:
        policy = PathPolicy(
            mode=PathPolicyMode.DENY_UNDER, paths=["**/*_test.go"], repo_root=go_repo
        )
        test_ok, _ = is_path_allowed(
            "Write", {"file_path": str(go_repo / "pkg" / "svc_test.go")}, policy
        )
        src_ok, _ = is_path_allowed(
            "Write", {"file_path": str(go_repo / "pkg" / "svc.go")}, policy
        )
        assert not test_ok and src_ok

    def test_js_suffix_globs(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        policy = PathPolicy(
            mode=PathPolicyMode.ALLOW_ONLY,
            paths=["**/*.test.*", "**/__tests__/**"],
            repo_root=tmp_path,
        )
        ok_test, _ = is_path_allowed(
            "Write", {"file_path": str(tmp_path / "src" / "a.test.ts")}, policy
        )
        ok_src, _ = is_path_allowed(
            "Write", {"file_path": str(tmp_path / "src" / "a.ts")}, policy
        )
        assert ok_test and not ok_src


class TestNonWriteTools:
    @pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "Bash", "TodoWrite"])
    def test_always_allowed_under_both_policies(
        self, repo, allow_policy, deny_policy, tool
    ) -> None:
        tool_input = {"file_path": str(repo / "src" / "x.py"), "command": "ls"}
        for policy in (allow_policy, deny_policy):
            allowed, _ = is_path_allowed(tool, tool_input, policy)
            assert allowed


def _call_hook(hook, tool_name: str, tool_input: dict, repo: Path):
    input_data = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "sess-test",
        "cwd": str(repo),
    }
    return asyncio.run(hook(input_data, "toolu_01", None))


class TestMakePretooluseHook:
    def test_allow_returns_empty_dict(self, repo, allow_policy) -> None:
        hook = make_pretooluse_hook(allow_policy)
        result = _call_hook(
            hook, "Write", {"file_path": str(repo / "tests" / "t.py")}, repo
        )
        assert result == {}

    def test_deny_returns_exact_verified_shape(self, repo, allow_policy) -> None:
        # Shape verified live against the SDK (docs/sdk-notes.md §2).
        hook = make_pretooluse_hook(allow_policy)
        result = _call_hook(
            hook, "Write", {"file_path": str(repo / "src" / "x.py")}, repo
        )

        assert set(result.keys()) == {"hookSpecificOutput"}
        out = result["hookSpecificOutput"]
        assert set(out.keys()) == {
            "hookEventName",
            "permissionDecision",
            "permissionDecisionReason",
        }
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny"
        assert isinstance(out["permissionDecisionReason"], str)
        assert out["permissionDecisionReason"]

    def test_non_write_tool_allowed(self, repo, allow_policy) -> None:
        hook = make_pretooluse_hook(allow_policy)
        result = _call_hook(hook, "Read", {"file_path": str(repo / "src" / "x.py")}, repo)
        assert result == {}

    def test_events_record_denied_flags(self, repo, allow_policy) -> None:
        events: list = []
        hook = make_pretooluse_hook(allow_policy, events=events)

        _call_hook(hook, "Write", {"file_path": str(repo / "tests" / "ok.py")}, repo)
        _call_hook(hook, "Write", {"file_path": str(repo / "src" / "bad.py")}, repo)

        assert len(events) == 2
        assert [_denied_flag(e) for e in events] == [False, True]
