"""Tests for tdd_loop1 — Loop 1 (EARS requirements specification, §8/§10/§12).

Drives run_loop1/amend_requirements in-process against the conftest fixtures
(tmp_repo/sluice_repo/feature) and a scripted FakeAgentRunner. Script files
live OUTSIDE the fixture repo (tmp_path_factory) so the fake's bookkeeping
never dirties the working tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tdd_contracts import (
    GITIGNORE_ENTRIES,
    ExitCode,
    LoopStatus,
    PathPolicyMode,
    Phase,
)
from tdd_fake_runner import FakeAgentRunner
from tdd_loop1 import amend_requirements, run_loop1
from tdd_state import SluiceError, StateStore, resolve_feature

REQUIREMENTS_REL = ".sluice/features/user-auth/requirements"
REQUIREMENTS_PROMPT = (
    Path(__file__).resolve().parent.parent
    / "skills" / "tdd" / "references" / "requirements_prompt.md"
)

#: A scripted draft run: one spec file inside requirements/ plus one write
#: OUTSIDE the policy paths (must be denied by the REAL ALLOW_ONLY policy).
DRAFT_RUN = {
    "text": "Drafted.\n- REQ-001 Login succeeds: happy path login",
    "session_id": "sess-l1",
    "cost_usd": 0.42,
    "num_turns": 5,
    "files": [
        {
            "path": f"{REQUIREMENTS_REL}/user-auth.md",
            "content": (
                "# User auth\n\n"
                "## REQ-001: Login succeeds\n\n"
                "WHEN valid credentials are submitted, "
                "THE SYSTEM SHALL start a session.\n"
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


def _ctx(feature: SimpleNamespace):
    return resolve_feature(feature.repo, feature.slug, False)


def _set_phase(ctx, phase: Phase) -> None:
    ctx.state.phase = phase.value
    ctx.store.save(ctx.state)


def _reload(ctx):
    return StateStore(ctx.feature_dir).load()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


class TestFreshDraft:
    def test_draft_checkpoints_awaiting_approval(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        outcome = run_loop1(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_APPROVAL
        assert int(outcome.exit_code) == 10
        assert f"{REQUIREMENTS_REL}/user-auth.md" in outcome.detail
        assert "--decision approve" in outcome.detail

        state = _reload(ctx)
        assert state.phase == Phase.AWAITING_APPROVAL.value
        assert state.session_ids["loop1"] == "sess-l1"
        assert state.budgets_spent.cost_usd == pytest.approx(0.42)
        assert state.budgets_spent.turns_loop1 == 5
        assert state.budgets_spent.started_at is not None
        # last history entry records the transition with the session id
        assert state.history[-1].phase == Phase.AWAITING_APPROVAL.value
        assert state.history[-1].session_id == "sess-l1"

    def test_policy_gates_scripted_writes(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop1(ctx, runner, None, None)

        assert (feature.requirements_dir / "user-auth.md").is_file()
        # the write outside requirements/ was denied by the real ALLOW_ONLY policy
        assert not (feature.repo / "src" / "evil.py").exists()

    def test_runspec_fields(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop1(ctx, runner, None, None)

        (spec,) = runner.received
        assert spec.session_id is None  # first-ever run starts a new session
        assert spec.model == ctx.config.models.requirements
        assert spec.system_prompt == REQUIREMENTS_PROMPT.read_text(encoding="utf-8")
        assert spec.allowed_tools == ["Read", "Glob", "Grep", "Write"]
        assert spec.path_policy_mode is PathPolicyMode.ALLOW_ONLY
        assert spec.path_policy_paths == [REQUIREMENTS_REL]
        assert spec.max_turns == ctx.config.budgets.max_turns_per_loop
        assert spec.max_budget_usd == pytest.approx(
            ctx.config.budgets.max_cost_usd
        )  # nothing spent yet -> full remaining budget
        assert spec.cwd == str(ctx.repo_root)

    def test_prompt_sections_stable_to_volatile(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        run_loop1(ctx, runner, None, None)

        prompt = runner.received[0].prompt
        task_text = (feature.feature_dir / "task.md").read_text()
        assert prompt.startswith("## Task\n\n" + task_text)
        assert "## Feature" in prompt
        assert f"Slug: {feature.slug}" in prompt
        assert str(feature.requirements_dir) in prompt

    def test_runner_error_fails_internal_error(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [{"text": "", "session_id": "sess-err", "cost_usd": 0.05,
              "num_turns": 1, "is_error": True}],
        )

        outcome = run_loop1(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        state = _reload(ctx)
        # run accounting persisted even on error; phase NOT transitioned
        assert state.phase == Phase.DRAFTING_REQUIREMENTS.value
        assert state.session_ids["loop1"] == "sess-err"
        assert state.budgets_spent.cost_usd == pytest.approx(0.05)


class TestApprove:
    def test_approve_without_spec_files_fails(self, feature, script_dir):
        ctx = _ctx(feature)
        _set_phase(ctx, Phase.AWAITING_APPROVAL)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop1(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "nothing to approve" in outcome.detail
        assert runner.received == []  # no agent run for an approve
        assert _reload(ctx).phase == Phase.AWAITING_APPROVAL.value

    def test_full_path_draft_then_approve(self, feature, script_dir):
        # production .gitignore policy: the whole .sluice/ workspace is local
        (feature.repo / ".gitignore").write_text(
            "\n".join(GITIGNORE_ENTRIES) + "\n"
        )
        ctx = _ctx(feature)
        runner = _runner(script_dir, feature.repo, [DRAFT_RUN])

        draft = run_loop1(ctx, runner, None, None)
        assert draft.exit_code is ExitCode.AWAITING_APPROVAL
        commits_before = _git(feature.repo, "rev-list", "--count", "HEAD").strip()

        outcome = run_loop1(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.ADVANCE
        assert outcome.detail == "spec approved"
        assert _reload(ctx).phase == Phase.REQUIREMENTS_APPROVED.value

        # No spec commit: .sluice/ is gitignored, the spec stays machine-local.
        commits_after = _git(feature.repo, "rev-list", "--count", "HEAD").strip()
        assert commits_after == commits_before
        assert (feature.repo / f"{REQUIREMENTS_REL}/user-auth.md").is_file()


class TestFeedback:
    def test_feedback_cycles_phase_and_resumes_session(
        self, feature, script_dir
    ):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [DRAFT_RUN, {"text": "Revised.", "cost_usd": 0.1, "num_turns": 2}],
        )
        run_loop1(ctx, runner, None, None)

        outcome = run_loop1(ctx, runner, "reject", "Add a lockout requirement")

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_APPROVAL
        assert len(runner.received) == 2  # second scripted run was consumed

        second = runner.received[1]
        assert second.session_id == "sess-l1"  # resumed the persisted session
        assert "## Reviewer feedback" in second.prompt
        assert "Add a lockout requirement" in second.prompt

        state = _reload(ctx)
        assert state.phase == Phase.AWAITING_APPROVAL.value
        assert state.budgets_spent.cost_usd == pytest.approx(0.52)
        assert state.budgets_spent.turns_loop1 == 7
        # phase cycled AWAITING_APPROVAL -> DRAFTING_REQUIREMENTS -> AWAITING_APPROVAL
        phases = [h.phase for h in state.history]
        assert phases[-2:] == [
            Phase.DRAFTING_REQUIREMENTS.value,
            Phase.AWAITING_APPROVAL.value,
        ]

    def test_awaiting_with_no_input_is_idempotent(self, feature, script_dir):
        ctx = _ctx(feature)
        _set_phase(ctx, Phase.AWAITING_APPROVAL)
        (feature.requirements_dir / "user-auth.md").write_text(
            "## REQ-001: Login succeeds\n"
        )
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop1(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_APPROVAL
        assert "awaiting review" in outcome.detail.lower()
        assert f"{REQUIREMENTS_REL}/user-auth.md" in outcome.detail
        assert runner.received == []  # no agent run consumed
        assert _reload(ctx).phase == Phase.AWAITING_APPROVAL.value


class TestBudgetGuard:
    def test_cost_budget_exceeded_checkpoints_without_running(
        self, feature, script_dir
    ):
        ctx = _ctx(feature)
        ctx.state.budgets_spent.cost_usd = ctx.config.budgets.max_cost_usd
        ctx.store.save(ctx.state)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop1(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.BUDGET_EXCEEDED
        assert int(outcome.exit_code) == 13
        assert "cost" in outcome.detail
        assert runner.received == []  # NO run consumed
        # phase untouched so a human can raise budgets and re-run
        assert _reload(ctx).phase == Phase.DRAFTING_REQUIREMENTS.value

    def test_amend_raises_sluice_error_when_over_budget(
        self, feature, script_dir
    ):
        ctx = _ctx(feature)
        ctx.state.budgets_spent.cost_usd = ctx.config.budgets.max_cost_usd
        ctx.store.save(ctx.state)
        runner = _runner(script_dir, feature.repo, [])
        proposal = _proposal()

        with pytest.raises(SluiceError) as excinfo:
            amend_requirements(ctx, runner, proposal)

        assert excinfo.value.exit_code is ExitCode.BUDGET_EXCEEDED
        assert runner.received == []


class TestWrongPhase:
    def test_other_phase_fails_defensively(self, feature, script_dir):
        ctx = _ctx(feature)
        _set_phase(ctx, Phase.IMPLEMENTING)
        runner = _runner(script_dir, feature.repo, [])

        outcome = run_loop1(ctx, runner, None, None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "loop1 called in phase IMPLEMENTING" in outcome.detail


def _proposal() -> dict:
    return {
        "test_file": "tests/test_auth.py",
        "related_requirement": "user-auth:REQ-001",
        "reason": "assertion encodes the wrong lockout threshold",
        "proposed_diff": "- assert attempts == 3\n+ assert attempts == 5\n",
    }


class TestAmendRequirements:
    def test_parses_amended_line(self, feature, script_dir):
        ctx = _ctx(feature)
        ctx.state.session_ids["loop1"] = "sess-l1"
        ctx.store.save(ctx.state)
        runner = _runner(
            script_dir,
            feature.repo,
            [{
                "text": "Edited the requirement.\n"
                        "AMENDED: user-auth:REQ-001, user-auth:REQ-002",
                "session_id": "sess-l1",
                "cost_usd": 0.2,
                "num_turns": 3,
            }],
        )

        ids = amend_requirements(ctx, runner, _proposal())

        assert ids == ["user-auth:REQ-001", "user-auth:REQ-002"]
        (spec,) = runner.received
        assert spec.session_id == "sess-l1"  # resumed the Loop 1 session
        assert spec.path_policy_mode is PathPolicyMode.ALLOW_ONLY
        assert spec.path_policy_paths == [REQUIREMENTS_REL]
        assert "## Approved test-change proposal" in spec.prompt
        assert "```diff" in spec.prompt
        assert "AMENDED:" in spec.prompt  # the instruction names the marker

        state = _reload(ctx)
        assert state.budgets_spent.cost_usd == pytest.approx(0.2)
        assert state.budgets_spent.turns_loop1 == 3
        # phase transitions around amendment are owned by Loop 3
        assert state.phase == Phase.DRAFTING_REQUIREMENTS.value

    def test_falls_back_to_related_requirement(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [{"text": "Edited, forgot the marker line."}],
        )

        ids = amend_requirements(ctx, runner, _proposal())

        assert ids == ["user-auth:REQ-001"]

    def test_related_requirement_always_included(self, feature, script_dir):
        ctx = _ctx(feature)
        runner = _runner(
            script_dir,
            feature.repo,
            [{"text": "AMENDED: user-auth:REQ-009"}],
        )

        ids = amend_requirements(ctx, runner, _proposal())

        assert ids == ["user-auth:REQ-009", "user-auth:REQ-001"]
