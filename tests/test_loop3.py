"""Tests for spec_implement_loop3 — implementation + escalation channel (§10, §15, §16).

World-building mirrors test_loop2: conftest fixtures + direct state edits;
the agent seam is FakeAgentRunner with scripted runs. Loops 1/2 are stubbed
via sys.modules injection for the approve path. Fake-script artifacts live
OUTSIDE the repo (the green commit sweeps `.` — nothing stray may dirty it).

The "test command" trick: config test.command exits 0 iff a PASS marker file
exists at the repo root, so scripted implementer runs flip red→green by
writing PASS (allowed: DENY_UNDER only protects tests/ and requirements/).
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import pytest

import spec_implement_loop3
from spec_implement_contracts import (
    COMMIT_GREEN,
    COMMIT_RED_AMENDED,
    ExitCode,
    LoopOutcome,
    LoopStatus,
    PathPolicyMode,
    Phase,
)
from spec_implement_fake_runner import FakeAgentRunner
from spec_implement_state import StateStore, resolve_feature

IMPLEMENT_MODEL = "claude-sonnet-4-6"  # conftest shepherd_repo config defaults
VERIFIER_MODEL = "claude-haiku-4-5"

PASS_COMMAND = (
    'python3 -c "import sys,pathlib; '
    "sys.exit(0 if pathlib.Path('PASS').exists() else 1)\""
)

REQUIREMENT_ID = "user_auth:REQ-001"

SPEC_TEXT = """# User auth

Rationale: pin the login behavior.

## REQ-001: Login succeeds

WHEN a registered user submits valid credentials, THE SYSTEM SHALL log them in.
"""

TEST_FILE = "tests/test_feature.py"
TEST_CONTENT = (
    "# requirement: user_auth:REQ-001\n"
    "import pathlib\n"
    "\n"
    "def test_x():\n"
    "    assert pathlib.Path('PASS').exists()\n"
)

MINOR_DIFF = (
    "--- a/tests/test_feature.py\n"
    "+++ b/tests/test_feature.py\n"
    "@@ -1,2 +1,2 @@\n"
    " # requirement: user_auth:REQ-001\n"
    "-import pathlib\n"
    "+import pathlib  # stdlib\n"
)

BROKEN_DIFF = (
    "--- a/tests/test_feature.py\n"
    "+++ b/tests/test_feature.py\n"
    "@@ -1,2 +1,2 @@\n"
    " # requirement: THIS CONTEXT DOES NOT EXIST\n"
    "-import nonexistent\n"
    "+import pathlib\n"
)


def _proposal(diff: str = MINOR_DIFF, reason: str = "fix import") -> dict:
    return {
        "tool_name": "propose_test_change",
        "tool_input": {
            "test_file": TEST_FILE,
            "related_requirement": REQUIREMENT_ID,
            "reason": reason,
            "proposed_diff": diff,
        },
    }


def _verdict(v: str) -> str:
    return json.dumps({"verdict": v, "rationale": f"triage says {v}"})


def _blocker(
    question: str = "JWT or session cookies?",
    context: str = "the requirement does not pin down the auth mechanism",
    options: str = "JWT | session cookies",
) -> dict:
    return {
        "tool_name": "request_human_input",
        "tool_input": {
            "question": question,
            "context": context,
            "suggested_options": options,
        },
    }


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _trace_json(tests: list[str]) -> str:
    return json.dumps(
        {
            "slug": "user-auth",
            "requirements": [
                {
                    "requirement_id": REQUIREMENT_ID,
                    "spec_file": "user_auth.md",
                    "revision": 1,
                    "tests": tests,
                    "status": "covered",
                    "notes": None,
                }
            ],
            "revisions": [],
            "schema_version": 1,
        }
    )


def _setup_loop3(
    feature: SimpleNamespace,
    *,
    phase: Phase = Phase.RED_COMMITTED,
    trace_tests: Optional[list[str]] = None,
    state_mut: Optional[Callable] = None,
):
    """Loop-3 entry world at RED_COMMITTED with a clean, committed tree."""

    (feature.requirements_dir / "user_auth.md").write_text(SPEC_TEXT)
    (feature.repo / ".gitignore").write_text(".shepherd/\nfake_script.json*\n")
    (feature.repo / TEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (feature.repo / TEST_FILE).write_text(TEST_CONTENT)
    (feature.feature_dir / ".spec-implement").mkdir(parents=True, exist_ok=True)
    (feature.feature_dir / ".spec-implement" / "traceability.json").write_text(
        _trace_json(trace_tests if trace_tests is not None else [f"{TEST_FILE}::test_x"])
    )

    cfg = feature.repo / ".shepherd" / "config.yaml"
    import yaml

    data = yaml.safe_load(cfg.read_text())
    data["test"]["command"] = PASS_COMMAND
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))

    _git(feature.repo, "add", "-A")
    _git(feature.repo, "commit", "-m", "loop3 world")

    store = StateStore(feature.feature_dir)
    state = store.load()
    state.phase = phase.value
    state.red_commit_count = 1
    if state_mut is not None:
        state_mut(state)
    store.save(state)

    return resolve_feature(feature.repo, feature.slug, force=False)


def _runner(feature: SimpleNamespace, runs: list[dict]) -> FakeAgentRunner:
    # Script artifacts OUTSIDE the repo: the green commit sweeps `.`.
    outside = feature.repo.parent / "fake_loop3_script.json"
    return FakeAgentRunner(runs, outside, feature.repo)


def _stub_sibling_loops(
    monkeypatch,
    *,
    amended_ids: list[str],
    resync_outcome: LoopOutcome,
    calls: dict,
):
    """Inject stub spec_implement_loop1/spec_implement_loop2 so lazy imports in loop3 hit them."""

    loop1 = types.ModuleType("spec_implement_loop1")

    def amend_requirements(ctx, runner, proposal):
        calls["amend"] = proposal
        return amended_ids

    loop1.amend_requirements = amend_requirements

    loop2 = types.ModuleType("spec_implement_loop2")

    def resync_tests(ctx, runner, requirement_ids):
        calls["resync"] = requirement_ids
        return resync_outcome

    loop2.resync_tests = resync_tests

    monkeypatch.setitem(sys.modules, "spec_implement_loop1", loop1)
    monkeypatch.setitem(sys.modules, "spec_implement_loop2", loop2)


class TestHappyPath:
    def test_red_to_green(self, feature) -> None:
        ctx = _setup_loop3(feature)
        before = _commit_count(feature.repo)
        runner = _runner(
            feature,
            [
                {
                    "text": "implemented",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                }
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.ADVANCE
        assert "green" in outcome.detail

        spec = runner.received[0]
        assert spec.model == IMPLEMENT_MODEL
        assert spec.session_id is None  # first-ever implementer turn
        assert spec.path_policy_mode is PathPolicyMode.DENY_UNDER
        assert spec.path_policy_paths == [
            "tests",
            ".shepherd/features/user-auth/requirements",
        ]
        assert spec.expose_propose_test_change is True
        assert "## Context" in spec.prompt
        assert spec.prompt.rstrip().rindex("## Test output") > spec.prompt.index("## Context")

        subject = _git(feature.repo, "log", "-1", "--format=%s").strip()
        assert subject == COMMIT_GREEN.format(slug="user-auth")
        assert _commit_count(feature.repo) == before + 1

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.DONE.value
        assert state.session_ids["loop3"] == "impl-sess-1"
        assert state.budgets_spent.cost_usd > 0

    def test_resumed_turn_carries_only_test_output(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {"text": "thinking", "session_id": "impl-sess-1"},
                {
                    "text": "fixed",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                },
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.ADVANCE
        second = runner.received[1]
        assert second.session_id == "impl-sess-1"  # resumed (§12)
        assert "## Context" not in second.prompt
        assert second.prompt.startswith("## Test output")


class TestTraceabilityGate:
    def test_green_with_broken_traceability_fails(self, feature) -> None:
        # Matrix maps a function name absent from the test file.
        ctx = _setup_loop3(feature, trace_tests=[f"{TEST_FILE}::test_ghost"])
        (feature.repo / "PASS").write_text("1")  # green immediately
        before = _commit_count(feature.repo)
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "traceability" in outcome.detail
        assert (ctx.reports_dir / "traceability_violation.md").is_file()
        assert _commit_count(feature.repo) == before  # NO green commit
        assert runner.received == []  # no agent runs at all


class TestProposals:
    def test_minor_auto_applied_and_cycle_continues(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {  # implementer proposes a minor change
                    "text": "blocked on import",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_proposal()],
                },
                {"text": _verdict("minor")},  # triage
                {  # cycle continues: next implementer run goes green
                    "text": "done",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                },
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.ADVANCE
        content = (feature.repo / TEST_FILE).read_text()
        assert "import pathlib  # stdlib" in content  # diff applied on disk

        from spec_implement_trace import load_matrix

        matrix = load_matrix(ctx.feature_dir)
        assert matrix.requirements[0].revision == 2
        assert matrix.revisions[-1].kind == "auto_applied_minor"

        triage_spec = runner.received[1]
        assert triage_spec.model == VERIFIER_MODEL
        assert triage_spec.allowed_tools == []
        assert "## Proposal" in triage_spec.prompt
        assert "Login succeeds" in triage_spec.prompt  # requirement located

    def test_significant_escalates(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "tests look wrong",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_proposal(reason="weaken assertion")],
                },
                {"text": _verdict("significant")},
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.ESCALATED
        assert (ctx.reports_dir / "escalation_1.md").is_file()
        assert (ctx.reports_dir / "escalation_1.json").is_file()
        report = json.loads((ctx.reports_dir / "escalation_1.json").read_text())
        assert report["proposal"]["test_file"] == TEST_FILE
        assert report["verdict"] == "significant"
        assert StateStore(ctx.feature_dir).load().phase == Phase.ESCALATED.value

    def test_garbage_triage_escalates_as_unsure(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "x",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_proposal()],
                },
                {"text": "I think this is probably fine to change?"},
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.exit_code is ExitCode.ESCALATED
        report = json.loads((ctx.reports_dir / "escalation_1.json").read_text())
        assert report["verdict"] == "unsure"

    def test_minor_verdict_with_broken_diff_escalates(self, feature) -> None:
        ctx = _setup_loop3(feature)
        original = (feature.repo / TEST_FILE).read_text()
        runner = _runner(
            feature,
            [
                {
                    "text": "x",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_proposal(diff=BROKEN_DIFF)],
                },
                {"text": _verdict("minor")},
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.exit_code is ExitCode.ESCALATED
        assert (feature.repo / TEST_FILE).read_text() == original  # untouched
        report = json.loads((ctx.reports_dir / "escalation_1.json").read_text())
        assert "did not apply cleanly" in report["rationale"]


class TestBlockerChannel:
    def _blocked(self, feature):
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "I cannot proceed without a decision",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_blocker()],
                }
            ],
        )
        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)
        assert outcome.exit_code is ExitCode.NEEDS_INPUT
        return ctx

    def test_request_human_input_checkpoints(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "blocked",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_blocker()],
                }
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.NEEDS_INPUT
        assert runner.received[0].expose_request_human_input is True
        assert (ctx.reports_dir / "blocker_1.md").is_file()
        data = json.loads((ctx.reports_dir / "blocker_1.json").read_text())
        assert data["question"] == "JWT or session cookies?"
        assert data["suggested_options"] == "JWT | session cookies"

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.BLOCKED.value
        assert state.session_ids["loop3"] == "impl-sess-1"

    def test_answer_resumes_session_and_reaches_green(self, feature) -> None:
        ctx = self._blocked(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "thanks — using JWT",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                }
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, "Use JWT")

        assert outcome.status is LoopStatus.ADVANCE  # PASS written → green
        answer_spec = runner.received[0]
        assert "## Human answer" in answer_spec.prompt
        assert "Use JWT" in answer_spec.prompt
        assert answer_spec.session_id == "impl-sess-1"  # resumed (§12)
        assert StateStore(ctx.feature_dir).load().phase == Phase.DONE.value

    def test_no_feedback_reexits_without_runs(self, feature) -> None:
        ctx = self._blocked(feature)
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.NEEDS_INPUT
        assert "awaiting answer" in outcome.detail
        assert runner.received == []

    def test_blocker_takes_precedence_over_proposal(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "stuck and the test looks wrong too",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_blocker(), _proposal()],
                }
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.exit_code is ExitCode.NEEDS_INPUT  # not ESCALATED
        assert (ctx.reports_dir / "blocker_1.md").is_file()
        assert not (ctx.reports_dir / "escalation_1.json").exists()
        assert len(runner.received) == 1  # no triage turn ran


class TestEscalationResolution:
    def _escalated(self, feature, monkeypatch=None) -> tuple:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature,
            [
                {
                    "text": "x",
                    "session_id": "impl-sess-1",
                    "tool_calls": [_proposal(reason="weaken")],
                },
                {"text": _verdict("significant")},
            ],
        )
        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)
        assert outcome.exit_code is ExitCode.ESCALATED
        return ctx, outcome

    def test_no_decision_reexits_without_runs(self, feature) -> None:
        ctx, _ = self._escalated(feature)
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.ESCALATED
        assert "awaiting decision" in outcome.detail
        assert runner.received == []

    def test_reject_resumes_with_rejection_section(self, feature) -> None:
        ctx, _ = self._escalated(feature)
        runner = _runner(
            feature,
            [
                {  # the rejection turn
                    "text": "understood",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                },
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, "reject", "test is correct")

        assert outcome.status is LoopStatus.ADVANCE  # PASS written → green
        rejection_spec = runner.received[0]
        assert "## Escalation rejected" in rejection_spec.prompt
        assert "test is correct" in rejection_spec.prompt
        assert rejection_spec.session_id == "impl-sess-1"  # resumed

    def test_approve_runs_amend_pipeline_to_green(self, feature, monkeypatch) -> None:
        ctx, _ = self._escalated(feature)
        calls: dict = {}
        _stub_sibling_loops(
            monkeypatch,
            amended_ids=[REQUIREMENT_ID],
            resync_outcome=LoopOutcome(LoopStatus.ADVANCE, detail="resynced"),
            calls=calls,
        )
        # Tests will be green when the main cycle resumes.
        (feature.repo / "PASS").write_text("1")
        # The resync normally leaves changed tests behind; simulate one so
        # red(2) has content to commit.
        (feature.repo / TEST_FILE).write_text(TEST_CONTENT + "# amended\n")
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.ADVANCE
        assert calls["amend"]["test_file"] == TEST_FILE
        assert calls["resync"] == [REQUIREMENT_ID]

        subjects = _git(feature.repo, "log", "--format=%s").splitlines()
        assert subjects[0] == COMMIT_GREEN.format(slug="user-auth")
        assert subjects[1] == COMMIT_RED_AMENDED.format(slug="user-auth", n=2)

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.DONE.value
        assert state.red_commit_count == 2

    def test_approve_with_failed_resync_stays_amending(
        self, feature, monkeypatch
    ) -> None:
        ctx, _ = self._escalated(feature)
        calls: dict = {}
        gap = LoopOutcome(
            LoopStatus.CHECKPOINT, ExitCode.COVERAGE_GAP, "resync gap"
        )
        _stub_sibling_loops(
            monkeypatch, amended_ids=[REQUIREMENT_ID], resync_outcome=gap, calls=calls
        )
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, "approve", None)

        assert outcome.exit_code is ExitCode.COVERAGE_GAP
        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.AMENDING_REQUIREMENTS.value  # recovery anchor


class TestGuards:
    def test_budget_exceeded_before_any_run(self, feature) -> None:
        def exhaust(state):
            state.budgets_spent.cost_usd = 99.0

        ctx = _setup_loop3(feature, state_mut=exhaust)
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.BUDGET_EXCEEDED
        assert runner.received == []
        # Phase untouched: IMPLEMENTING (transitioned on entry from RED_COMMITTED).
        assert StateStore(ctx.feature_dir).load().phase == Phase.IMPLEMENTING.value

    def test_timeout_treated_as_failing_run(self, feature, monkeypatch) -> None:
        ctx = _setup_loop3(feature)

        real_run = subprocess.run
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            if kwargs.get("shell"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise subprocess.TimeoutExpired(cmd=args[0], timeout=900)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(spec_implement_loop3.subprocess, "run", fake_run)
        runner = _runner(
            feature,
            [
                {
                    "text": "ok",
                    "session_id": "impl-sess-1",
                    "files": [{"path": "PASS", "content": "1"}],
                }
            ],
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.ADVANCE
        assert "timed out" in runner.received[0].prompt

    def test_runner_error_fails(self, feature) -> None:
        ctx = _setup_loop3(feature)
        runner = _runner(
            feature, [{"text": "", "is_error": True, "session_id": "s"}]
        )

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR

    def test_defensive_on_wrong_phase(self, feature) -> None:
        ctx = _setup_loop3(feature, phase=Phase.DRAFTING_REQUIREMENTS)
        runner = _runner(feature, [])

        outcome = spec_implement_loop3.run_loop3(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert "cannot run from phase" in outcome.detail


class TestDiffApplier:
    def test_pure_insertion_rejected(self, feature) -> None:
        ctx = _setup_loop3(feature)
        diff = (
            "--- a/tests/test_feature.py\n"
            "+++ b/tests/test_feature.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+import sys\n"
        )
        assert not spec_implement_loop3._apply_unified_diff(ctx.repo_root, TEST_FILE, diff)

    def test_wrong_file_header_rejected(self, feature) -> None:
        ctx = _setup_loop3(feature)
        diff = MINOR_DIFF.replace("tests/test_feature.py", "src/other.py")
        assert not spec_implement_loop3._apply_unified_diff(ctx.repo_root, TEST_FILE, diff)

    def test_fenced_diff_applies(self, feature) -> None:
        ctx = _setup_loop3(feature)
        fenced = "```diff\n" + MINOR_DIFF + "```"
        assert spec_implement_loop3._apply_unified_diff(ctx.repo_root, TEST_FILE, fenced)
        assert "# stdlib" in (ctx.repo_root / TEST_FILE).read_text()

    def test_ambiguous_context_rejected(self, tmp_path) -> None:
        target = tmp_path / "t.py"
        target.write_text("x = 1\nx = 1\n")
        diff = "@@ -5,1 +5,1 @@\n-x = 1\n+x = 2\n"  # wrong hint, 2 matches
        assert not spec_implement_loop3._apply_unified_diff(tmp_path, "t.py", diff)
