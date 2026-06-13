"""Subprocess-level CLI contract tests for tdd.py (§6 verbs, §13 exit codes).

These define the CLI contract. Tests that exercise loop internals are loose
here and tightened in T3-INTEG (marked inline). The SDK is never touched:
TDD_RUNNER=fake:<script.json> selects FakeAgentRunner for every `run`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import TDD_PY, run_cli  # noqa: F401  (run_cli also available as fixture)
from tdd_contracts import Phase

pytestmark = pytest.mark.skipif(
    not TDD_PY.exists(), reason="tdd.py not yet written (parallel track T1-CLI)"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


@pytest.fixture
def fake_env(tmp_path_factory) -> dict:
    """TDD_RUNNER env pointing at a generous fake script (outside any repo)."""

    scratch = tmp_path_factory.mktemp("fake-runner")
    script = scratch / "script.json"
    runs = [
        {"text": f"Feature: stub output for scripted run {i}\n"} for i in range(6)
    ]
    script.write_text(json.dumps({"runs": runs}))
    return {"TDD_RUNNER": f"fake:{script}"}


class TestUninitialized:
    def test_run_without_shepherd_exits_22(self, tmp_repo, fake_env) -> None:
        result = run_cli(["run"], tmp_repo, env_extra=fake_env)
        assert result.returncode == 22, result.stderr


class TestInit:
    def test_init_creates_config_with_detected_conventions(self, tmp_repo) -> None:
        result = run_cli(["init"], tmp_repo)
        assert result.returncode == 0, result.stderr

        config = tmp_repo / ".shepherd" / "config.yaml"
        assert config.exists()
        text = config.read_text()
        assert "pytest" in text       # detected from pyproject
        assert "tests" in text        # detected testpaths
        assert (tmp_repo / ".shepherd" / "features").is_dir()

    def test_init_twice_is_idempotent(self, tmp_repo) -> None:
        assert run_cli(["init"], tmp_repo).returncode == 0
        config = tmp_repo / ".shepherd" / "config.yaml"
        before = config.read_text()

        result = run_cli(["init"], tmp_repo)

        assert result.returncode == 0, result.stderr
        assert config.read_text() == before  # never overwritten without --force


class TestNew:
    def test_new_scaffolds_feature(self, tmp_repo) -> None:
        assert run_cli(["init"], tmp_repo).returncode == 0

        result = run_cli(["new", "Add user auth"], tmp_repo)
        assert result.returncode == 0, result.stderr

        assert _current_branch(tmp_repo) == "tdd/add-user-auth"

        feature_dir = tmp_repo / ".shepherd" / "features" / "add-user-auth"
        assert feature_dir.is_dir()
        # Without --task-file, the title is the task statement (§6).
        assert (feature_dir / "task.md").read_text().strip() == "Add user auth"

        state = json.loads((feature_dir / ".tdd" / "state.json").read_text())
        assert state["slug"] == "add-user-auth"
        assert state["branch"] == "tdd/add-user-auth"
        assert state["phase"] in {p.value for p in Phase}
        assert len(state["base_commit"]) == 40

    def test_new_task_stdin_becomes_task_md(self, tmp_repo) -> None:
        assert run_cli(["init"], tmp_repo).returncode == 0
        statement = (
            "List users with pagination.\n\n"
            "- Default page size is 20.\n- Max page size is 100.\n"
        )

        result = run_cli(
            ["new", "Paginated user list", "--task-stdin"],
            tmp_repo,
            stdin=statement,
        )
        assert result.returncode == 0, result.stderr

        # Slug/branch derive from the title; task.md holds the full statement.
        assert _current_branch(tmp_repo) == "tdd/paginated-user-list"
        task_md = (
            tmp_repo / ".shepherd" / "features" / "paginated-user-list" / "task.md"
        )
        assert task_md.read_text() == statement

    def test_new_rejects_empty_task_stdin(self, tmp_repo) -> None:
        assert run_cli(["init"], tmp_repo).returncode == 0

        result = run_cli(
            ["new", "Add user auth", "--task-stdin"], tmp_repo, stdin="  \n"
        )

        assert result.returncode != 0
        assert "empty" in result.stderr
        # Nothing scaffolded on refusal.
        assert not (tmp_repo / ".shepherd" / "features" / "add-user-auth").exists()

    def test_new_refuses_dirty_tree(self, tmp_repo) -> None:
        assert run_cli(["init"], tmp_repo).returncode == 0
        readme = tmp_repo / "README.md"
        readme.write_text(readme.read_text() + "\nuncommitted edit\n")

        result = run_cli(["new", "Add user auth"], tmp_repo)

        assert result.returncode != 0
        assert (result.stderr + result.stdout).strip()  # explains the refusal


def _init_and_new(tmp_repo) -> None:
    assert run_cli(["init"], tmp_repo).returncode == 0
    assert run_cli(["new", "Add user auth"], tmp_repo).returncode == 0


class TestRunResolution:
    def test_run_on_main_without_feature_exits_20(self, tmp_repo, fake_env) -> None:
        _init_and_new(tmp_repo)
        _git(tmp_repo, "checkout", "main")

        result = run_cli(["run"], tmp_repo, env_extra=fake_env)
        assert result.returncode == 20, result.stderr

    def test_run_with_feature_from_main_exits_21(self, tmp_repo, fake_env) -> None:
        _init_and_new(tmp_repo)
        _git(tmp_repo, "checkout", "main")

        result = run_cli(
            ["run", "--feature", "add-user-auth"], tmp_repo, env_extra=fake_env
        )
        assert result.returncode == 21, result.stderr

    def test_force_proceeds_past_branch_mismatch(self, tmp_repo, fake_env) -> None:
        # tightened in T3-INTEG
        _init_and_new(tmp_repo)
        _git(tmp_repo, "checkout", "main")

        result = run_cli(
            ["run", "--feature", "add-user-auth", "--force"],
            tmp_repo,
            env_extra=fake_env,
        )

        # Resolution must succeed; whatever happens next is a loop concern.
        # A fresh feature starts in Loop 0 (design), so the first checkpoint is
        # exit 15 (AWAITING_DESIGN_APPROVAL).
        assert result.returncode not in (20, 21, 22), (
            result.returncode,
            result.stderr,
        )
        combined = (result.stderr + result.stdout).lower()
        assert result.returncode in (1, 10, 15) or "loop" in combined, (
            result.returncode,
            result.stderr,
        )


class TestStatus:
    def test_status_json_includes_feature_and_phase(self, tmp_repo) -> None:
        _init_and_new(tmp_repo)

        result = run_cli(["status", "--json"], tmp_repo)

        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)  # must be valid JSON
        assert "add-user-auth" in result.stdout
        assert any(p.value in result.stdout for p in Phase)
