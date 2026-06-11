"""Tests for tdd_loop2 — test generation + coverage verification (§9, §12).

World-building uses the conftest fixtures (tmp_repo/harness_repo/feature) plus
direct state edits; the agent seam is FakeAgentRunner with per-test scripted
runs. Loop 1 is never called: loop2 entry is set up by writing a .feature file
and flipping state.phase to GHERKIN_APPROVED directly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import tdd_loop2
from tdd_contracts import (
    COMMIT_RED,
    ExitCode,
    LoopStatus,
    PathPolicyMode,
    Phase,
    ScenarioTrace,
    TraceabilityMatrix,
)
from tdd_fake_runner import FakeAgentRunner
from tdd_state import StateStore, resolve_feature
from tdd_trace import load_matrix, save_matrix

TESTGEN_MODEL = "claude-sonnet-4-6"   # conftest harness_repo config defaults
VERIFIER_MODEL = "claude-haiku-4-5"

ONE_SCENARIO_FEATURE = """Feature: User auth

  Scenario: Successful login
    Given a registered user
    When they submit valid credentials
    Then they are logged in
"""

TWO_SCENARIO_FEATURE = ONE_SCENARIO_FEATURE + """
  Scenario: Lockout after failures
    Given a registered user
    When they submit wrong credentials five times
    Then the account is locked
"""

LOGIN_ID = "user_auth:Successful login"
LOCKOUT_ID = "user_auth:Lockout after failures"

ROW_LOGIN_COVERED = {
    "scenario_id": LOGIN_ID,
    "feature_file": "user_auth.feature",
    "tests": ["tests/test_user_auth.py::test_successful_login"],
    "status": "covered",
}
ROW_LOCKOUT_MISSING = {
    "scenario_id": LOCKOUT_ID,
    "feature_file": "user_auth.feature",
    "tests": [],
    "status": "missing",
    "notes": "no test exercises the lockout path",
}
ROW_LOCKOUT_COVERED = {
    "scenario_id": LOCKOUT_ID,
    "feature_file": "user_auth.feature",
    "tests": ["tests/test_user_auth.py::test_lockout"],
    "status": "covered",
}

TEST_FILE_CONTENT = (
    "# scenario: user_auth:Successful login\n"
    "def test_successful_login():\n"
    "    assert False\n"
)


def _matrix_json(*rows: dict) -> str:
    return json.dumps({"scenarios": list(rows)})


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _setup_loop2(
    feature: SimpleNamespace,
    *,
    feature_text: str = ONE_SCENARIO_FEATURE,
    phase: Phase = Phase.GHERKIN_APPROVED,
    state_mut: Optional[Callable] = None,
    max_iterations: Optional[int] = None,
):
    """Loop-2 entry world: .feature on disk, phase set by direct state edit."""

    (feature.gherkin_dir / "user_auth.feature").write_text(feature_text)
    # Production .gitignore policy (§5): state.json never lands in commits.
    (feature.repo / ".gitignore").write_text(
        ".harness/features/*/.tdd/state.json\n"
    )
    if max_iterations is not None:
        cfg = feature.repo / ".harness" / "config.yaml"
        cfg.write_text(
            cfg.read_text().replace(
                "max_coverage_iterations: 5",
                f"max_coverage_iterations: {max_iterations}",
            )
        )

    store = StateStore(feature.feature_dir)
    state = store.load()
    state.phase = phase.value
    if state_mut is not None:
        state_mut(state)
    store.save(state)

    return resolve_feature(feature.repo, feature.slug, force=False)


def _runner(repo: Path, runs: list[dict]) -> FakeAgentRunner:
    return FakeAgentRunner(runs, repo / "fake_script.json", repo)


class TestHappyPath:
    def test_full_coverage_produces_red_commit(self, feature) -> None:
        ctx = _setup_loop2(feature)
        runner = _runner(
            feature.repo,
            [
                {
                    "text": "tests written",
                    "session_id": "gen-sess-1",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED)},
            ],
        )

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.ADVANCE
        assert "red committed" in outcome.detail

        # ALLOW_ONLY tests/ permitted the scripted write.
        gen_spec = runner.received[0]
        assert gen_spec.model == TESTGEN_MODEL
        assert gen_spec.session_id is None  # first-ever generator turn
        assert gen_spec.path_policy_mode is PathPolicyMode.ALLOW_ONLY
        assert gen_spec.path_policy_paths == ["tests"]
        assert (feature.repo / "tests" / "test_user_auth.py").is_file()

        # Verifier is a stateless, tool-less one-shot.
        ver_spec = runner.received[1]
        assert ver_spec.model == VERIFIER_MODEL
        assert ver_spec.session_id is None
        assert ver_spec.allowed_tools == []
        assert ver_spec.path_policy_mode is None
        assert "## Gherkin" in ver_spec.prompt
        assert "test_successful_login" in ver_spec.prompt  # tests read from disk

        # Matrix persisted at .tdd/traceability.json with covered rows.
        matrix = load_matrix(ctx.feature_dir)
        assert matrix is not None
        assert [s.scenario_id for s in matrix.scenarios] == [LOGIN_ID]
        assert matrix.scenarios[0].status == "covered"
        assert matrix.scenarios[0].tests == [
            "tests/test_user_auth.py::test_successful_login"
        ]

        # Red commit: exact message, contains test file AND traceability.json.
        subject = _git(feature.repo, "log", "-1", "--format=%s").strip()
        assert subject == COMMIT_RED.format(slug="user-auth")
        shown = _git(feature.repo, "show", "--name-only", "HEAD")
        assert "tests/test_user_auth.py" in shown
        assert ".harness/features/user-auth/.tdd/traceability.json" in shown
        assert "state.json" not in shown  # gitignored, never committed

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.RED_COMMITTED.value
        assert state.red_commit_count == 1
        assert state.session_ids["loop2"] == "gen-sess-1"


class TestGapIteration:
    def test_gap_then_covered_resumes_session_with_gaps_only(self, feature) -> None:
        ctx = _setup_loop2(feature, feature_text=TWO_SCENARIO_FEATURE)
        runner = _runner(
            feature.repo,
            [
                {
                    "text": "first batch",
                    "session_id": "gen-sess",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED, ROW_LOCKOUT_MISSING)},
                {
                    "text": "lockout test added",
                    "files": [
                        {
                            "path": "tests/test_user_auth.py",
                            "content": TEST_FILE_CONTENT
                            + "\n# scenario: user_auth:Lockout after failures\n"
                            "def test_lockout():\n    assert False\n",
                        }
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED, ROW_LOCKOUT_COVERED)},
            ],
        )

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.ADVANCE
        assert len(runner.received) == 4  # gen, verify, gen, verify

        generator_specs = [s for s in runner.received if s.model == TESTGEN_MODEL]
        assert len(generator_specs) == 2  # exactly TWO generator runs

        second_gen = runner.received[2]
        assert second_gen.model == TESTGEN_MODEL
        # Resumed with the persisted session id (§12 cache target).
        assert second_gen.session_id == "gen-sess"
        # Cache discipline: ONLY the volatile gaps; no stable sections resent.
        assert "## Coverage gaps" in second_gen.prompt
        assert LOCKOUT_ID in second_gen.prompt
        assert "Project test conventions" not in second_gen.prompt
        assert "Approved Gherkin" not in second_gen.prompt

        matrix = load_matrix(ctx.feature_dir)
        assert {s.scenario_id: s.status for s in matrix.scenarios} == {
            LOGIN_ID: "covered",
            LOCKOUT_ID: "covered",
        }

    def test_gaps_exhausted_writes_report_and_checkpoints(self, feature) -> None:
        ctx = _setup_loop2(
            feature, feature_text=TWO_SCENARIO_FEATURE, max_iterations=1
        )
        runner = _runner(
            feature.repo,
            [
                {
                    "text": "partial batch",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED, ROW_LOCKOUT_MISSING)},
            ],
        )

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.COVERAGE_GAP  # exit 11
        report = ctx.reports_dir / "coverage_gap.md"
        assert report.is_file()
        assert LOCKOUT_ID in report.read_text()
        assert str(report) in outcome.detail

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.VERIFYING_COVERAGE.value

    def test_verifier_garbage_twice_counts_as_failed_iteration(self, feature) -> None:
        ctx = _setup_loop2(feature, max_iterations=1)
        runner = _runner(
            feature.repo,
            [
                {
                    "text": "tests written",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": "I could not build the matrix, sorry."},  # no JSON
                {"text": "still prose, no JSON here either"},      # retry garbage
            ],
        )

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.COVERAGE_GAP  # exit 11 with max 1
        assert len(runner.received) == 3  # gen + verifier + ONE retry

        # The retry carried the parse error back to the verifier.
        retry_spec = runner.received[2]
        assert retry_spec.model == VERIFIER_MODEL
        assert "## Parse error" in retry_spec.prompt

        report = ctx.reports_dir / "coverage_gap.md"
        assert report.is_file()
        assert "unparseable" in report.read_text()  # the recorded note

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.VERIFYING_COVERAGE.value


class TestMatrixMerge:
    def test_merge_preserves_existing_revision(self, feature) -> None:
        ctx = _setup_loop2(feature)
        seeded = TraceabilityMatrix(
            slug="user-auth",
            scenarios=[
                ScenarioTrace(
                    scenario_id=LOGIN_ID,
                    feature_file="user_auth.feature",
                    revision=3,
                    tests=[],
                    status="missing",
                )
            ],
        )
        save_matrix(ctx.feature_dir, seeded)

        runner = _runner(
            feature.repo,
            [
                {
                    "text": "tests written",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED)},
            ],
        )

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.ADVANCE
        matrix = load_matrix(ctx.feature_dir)
        (scenario,) = matrix.scenarios
        assert scenario.revision == 3  # kept across the verifier re-report
        assert scenario.status == "covered"
        assert scenario.tests == ["tests/test_user_auth.py::test_successful_login"]


class TestResync:
    def test_resync_updates_affected_tests_without_commit(self, feature) -> None:
        def seed_session(state):
            state.session_ids["loop2"] = "sess-loop2"

        ctx = _setup_loop2(
            feature, phase=Phase.AMENDING_GHERKIN, state_mut=seed_session
        )
        seeded = TraceabilityMatrix(
            slug="user-auth",
            scenarios=[
                ScenarioTrace(
                    scenario_id=LOGIN_ID,
                    feature_file="user_auth.feature",
                    revision=1,
                    tests=["tests/test_user_auth.py::test_successful_login"],
                    status="covered",
                )
            ],
        )
        save_matrix(ctx.feature_dir, seeded)
        commits_before = _commit_count(feature.repo)

        runner = _runner(
            feature.repo,
            [
                {
                    "text": "mapped tests updated",
                    "files": [
                        {"path": "tests/test_user_auth.py", "content": TEST_FILE_CONTENT}
                    ],
                },
                {"text": _matrix_json(ROW_LOGIN_COVERED)},
            ],
        )

        outcome = tdd_loop2.resync_tests(ctx, runner, [LOGIN_ID])

        assert outcome.status is LoopStatus.ADVANCE

        # Generator resumed the loop2 session with the scoped amendment turn.
        gen_spec = runner.received[0]
        assert gen_spec.model == TESTGEN_MODEL
        assert gen_spec.session_id == "sess-loop2"
        assert "## Amended scenarios" in gen_spec.prompt
        assert "## Affected tests" in gen_spec.prompt
        assert LOGIN_ID in gen_spec.prompt
        assert "Update ONLY their mapped tests" in gen_spec.prompt
        assert "Project test conventions" not in gen_spec.prompt

        # bump_revisions applied: revision incremented + resync audit entry.
        matrix = load_matrix(ctx.feature_dir)
        (scenario,) = matrix.scenarios
        assert scenario.revision == 2
        assert matrix.revisions[-1].kind == "resync"
        assert matrix.revisions[-1].scenario_ids == [LOGIN_ID]

        # NO commit and NO phase transition — Loop 3 owns both.
        assert _commit_count(feature.repo) == commits_before
        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.AMENDING_GHERKIN.value

    def test_resync_without_matrix_fails(self, feature) -> None:
        ctx = _setup_loop2(feature)
        runner = _runner(feature.repo, [])

        outcome = tdd_loop2.resync_tests(ctx, runner, [LOGIN_ID])

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert runner.received == []


class TestGuards:
    def test_budget_exceeded_checkpoints_before_any_run(self, feature) -> None:
        def blow_budget(state):
            state.budgets_spent.cost_usd = 999.0  # > max_cost_usd (10)

        ctx = _setup_loop2(feature, state_mut=blow_budget)
        runner = _runner(feature.repo, [])

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.BUDGET_EXCEEDED  # exit 13
        assert runner.received == []  # no run consumed
        assert "budget exceeded" in outcome.detail

    def test_runner_error_fails_loop(self, feature) -> None:
        ctx = _setup_loop2(feature)
        runner = _runner(feature.repo, [{"text": "", "is_error": True}])

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR

    def test_wrong_entry_phase_fails_defensively(self, feature) -> None:
        ctx = _setup_loop2(feature, phase=Phase.DRAFTING_GHERKIN)
        runner = _runner(feature.repo, [])

        outcome = tdd_loop2.run_loop2(ctx, runner)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert runner.received == []
