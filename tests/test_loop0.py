"""Tests for spec_implement_loop0 — Loop 0 (design sketch).

Drives run_loop0 in-process against the conftest fixtures (feature) and a
scripted FakeAgentRunner. Script files live OUTSIDE the fixture repo
(tmp_path_factory) so the fake's bookkeeping never dirties the working tree.

The `feature` fixture seeds phase DRAFTING_REQUIREMENTS; these tests reset the
phase directly (bypassing transition validation, like test_loop1) to exercise
Loop 0's own phases.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_implement_contracts import (
    ExitCode,
    LoopStatus,
    PathPolicyMode,
    Phase,
)
from spec_implement_fake_runner import FakeAgentRunner
from spec_implement_loop0 import run_loop0
from spec_implement_state import StateStore, resolve_feature

DESIGN_REL = ".shepherd/features/user-auth/design"
DESIGN_PROMPT = (
    Path(__file__).resolve().parent.parent
    / "skills" / "spec-implement" / "references" / "design_prompt.md"
)

#: A scripted draft run: one design file inside design/ plus one write OUTSIDE
#: the policy paths (must be denied by the REAL ALLOW_ONLY policy).
DRAFT_RUN = {
    "text": "Sketched.\n- LoginService: validates credentials",
    "session_id": "sess-l0",
    "cost_usd": 0.30,
    "num_turns": 4,
    "files": [
        {
            "path": f"{DESIGN_REL}/design.md",
            "content": (
                "# User auth design\n\n"
                "## Components\n\n- LoginService.login(creds) -> Session\n"
            ),
        },
        {"path": "src/evil.py", "content": "# must never land\n"},
    ],
}


@pytest.fixture
def script_dir(tmp_path_factory) -> Path:
    """A directory for fake-runner scripts, outside the fixture repo."""

    return tmp_path_factory.mktemp("fake-scripts")


def _runner(
    script_dir: Path, repo: Path, runs: list[dict], name: str = "script.json"
) -> FakeAgentRunner:
    path = script_dir / name
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    return FakeAgentRunner.from_script(str(path), repo)


def _ctx(feature: SimpleNamespace, phase: Phase = Phase.SKETCHING_DESIGN):
    ctx = resolve_feature(feature.repo, feature.slug, False)
    ctx.state.phase = phase.value
    ctx.store.save(ctx.state)
    return ctx


def _reload(ctx):
    return StateStore(ctx.feature_dir).load()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


class TestFreshDraft:
    def test_draft_checkpoints_awaiting_design_approval(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        outcome = run_loop0(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_DESIGN_APPROVAL
        assert int(outcome.exit_code) == 15
        assert f"{DESIGN_REL}/design.md" in outcome.detail
        assert "--decision approve" in outcome.detail

        state = _reload(ctx)
        assert state.phase == Phase.AWAITING_DESIGN_APPROVAL.value
        assert state.session_ids["loop0"] == "sess-l0"
        assert state.budgets_spent.cost_usd == pytest.approx(0.30)
        assert state.budgets_spent.turns_loop0 == 4
        assert state.budgets_spent.started_at is not None
        assert state.history[-1].phase == Phase.AWAITING_DESIGN_APPROVAL.value
        assert state.history[-1].session_id == "sess-l0"

    def test_policy_gates_scripted_writes(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop0(ctx, runner, None, None)

        assert (feature.design_dir / "design.md").is_file()
        # the write outside design/ was denied by the real ALLOW_ONLY policy
        assert not (feature.repo / "src" / "evil.py").exists()

    def test_runspec_fields(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop0(ctx, runner, None, None)

        (spec,) = runner.received
        assert spec.session_id is None  # first-ever run starts a new session
        assert spec.model == ctx.config.models.design
        assert spec.system_prompt == DESIGN_PROMPT.read_text(encoding="utf-8")
        assert spec.allowed_tools == ["Read", "Glob", "Grep", "Write"]
        assert spec.path_policy_mode is PathPolicyMode.ALLOW_ONLY
        assert spec.path_policy_paths == [DESIGN_REL]
        assert spec.max_turns == ctx.config.budgets.max_turns_per_loop
        assert spec.max_budget_usd == pytest.approx(ctx.config.budgets.max_cost_usd)
        assert spec.cwd == str(ctx.repo_root)

    def test_prompt_sections_stable_to_volatile(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop0(ctx, runner, None, None)

        prompt = runner.received[0].prompt
        task_text = (feature.feature_dir / "task.md").read_text()
        assert prompt.startswith("## Task\n\n" + task_text)
        assert "## Feature" in prompt
        assert f"Slug: {feature.slug}" in prompt
        assert str(feature.design_dir) in prompt

    def test_runner_error_fails_internal_error(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [{"text": "", "session_id": "sess-err", "cost_usd": 0.05,
              "num_turns": 1, "is_error": True}],
        )

        outcome = run_loop0(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        state = _reload(ctx)
        # run accounting persisted even on error; phase NOT transitioned
        assert state.phase == Phase.SKETCHING_DESIGN.value
        assert state.session_ids["loop0"] == "sess-err"
        assert state.budgets_spent.cost_usd == pytest.approx(0.05)


class TestApprove:
    def test_approve_without_design_files_fails(self, feature, script_dir):
        ctx = _ctx(feature, Phase.AWAITING_DESIGN_APPROVAL)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop0(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "nothing to approve" in outcome.detail
        assert runner.received == []  # no agent run for an approve
        assert _reload(ctx).phase == Phase.AWAITING_DESIGN_APPROVAL.value

    def test_full_path_draft_then_approve(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        draft = run_loop0(ctx, runner, None, None)
        assert draft.exit_code is ExitCode.AWAITING_DESIGN_APPROVAL
        commits_before = _git(feature.repo, "rev-list", "--count", "HEAD").strip()

        outcome = run_loop0(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.ADVANCE
        assert outcome.detail == "design approved"
        # advances to DESIGN_APPROVED — Loop 1's entry phase
        assert _reload(ctx).phase == Phase.DESIGN_APPROVED.value

        # No commit: .shepherd/ is gitignored, the design stays machine-local.
        commits_after = _git(feature.repo, "rev-list", "--count", "HEAD").strip()
        assert commits_after == commits_before
        assert (feature.design_dir / "design.md").is_file()


class TestFeedback:
    def test_feedback_cycles_phase_and_resumes_session(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [DRAFT_RUN, {"text": "Revised.", "cost_usd": 0.1, "num_turns": 2}],
        )
        run_loop0(ctx, runner, None, None)

        outcome = run_loop0(ctx, runner, "reject", "Add a session-expiry component")

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_DESIGN_APPROVAL
        assert len(runner.received) == 2  # second scripted run was consumed

        second = runner.received[1]
        assert second.session_id == "sess-l0"  # resumed the persisted session
        assert "## Reviewer feedback" in second.prompt
        assert "Add a session-expiry component" in second.prompt

        state = _reload(ctx)
        assert state.phase == Phase.AWAITING_DESIGN_APPROVAL.value
        assert state.budgets_spent.cost_usd == pytest.approx(0.40)
        assert state.budgets_spent.turns_loop0 == 6
        # phase cycled AWAITING_DESIGN_APPROVAL -> SKETCHING_DESIGN -> AWAITING_DESIGN_APPROVAL
        phases = [h.phase for h in state.history]
        assert phases[-2:] == [
            Phase.SKETCHING_DESIGN.value,
            Phase.AWAITING_DESIGN_APPROVAL.value,
        ]

    def test_awaiting_with_no_input_is_idempotent(self, feature, script_dir):
        ctx = _ctx(feature, Phase.AWAITING_DESIGN_APPROVAL)
        (feature.design_dir / "design.md").write_text("# Design\n")
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop0(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_DESIGN_APPROVAL
        assert "awaiting review" in outcome.detail.lower()
        assert f"{DESIGN_REL}/design.md" in outcome.detail
        assert runner.received == []  # no agent run consumed
        assert _reload(ctx).phase == Phase.AWAITING_DESIGN_APPROVAL.value


class TestBudgetGuard:
    def test_cost_budget_exceeded_checkpoints_without_running(self, feature, script_dir):
        ctx = _ctx(feature)
        ctx.state.budgets_spent.cost_usd = ctx.config.budgets.max_cost_usd
        ctx.store.save(ctx.state)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop0(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.BUDGET_EXCEEDED
        assert int(outcome.exit_code) == 13
        assert "cost" in outcome.detail
        assert runner.received == []  # NO run consumed
        # phase untouched so a human can raise budgets and re-run
        assert _reload(ctx).phase == Phase.SKETCHING_DESIGN.value


class TestWrongPhase:
    def test_other_phase_fails_defensively(self, feature, script_dir):
        ctx = _ctx(feature, Phase.IMPLEMENTING)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop0(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "loop0 called in phase IMPLEMENTING" in outcome.detail
