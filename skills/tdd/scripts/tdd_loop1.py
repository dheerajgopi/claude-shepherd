"""Loop 1 — Gherkin specification (§8, §10, §12).

`run_loop1` drives the draft → human review → approve/revise cycle on the
active feature: the gherkin model explores the repository read-only (Write is
mechanically scoped to the feature's gherkin/ folder via the ALLOW_ONLY path
policy), drafts `.feature` files, and the loop checkpoints with exit 10 until
the human approves, at which point the spec commit is created and the
dispatcher advances to Loop 2.

`amend_scenarios` is the §10 escalation re-entry: Loop 3 calls it after an
approved test-change proposal; it resumes the Loop 1 session, amends only the
affected scenarios, and returns their scenario ids. Phase transitions around
amendment are owned by Loop 3 — this function never transitions.

All shared vocabulary comes from tdd_contracts; state mutations go through
ctx.store; git through tdd_git; prompt assembly through
tdd_agent.build_prompt with sections ordered stable → volatile (§12).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tdd_git
from tdd_agent import build_prompt
from tdd_contracts import (
    COMMIT_SPEC,
    DECISION_APPROVE,
    TASK_FILE,
    AgentRunner,
    ExitCode,
    LoopOutcome,
    LoopStatus,
    PathPolicyMode,
    Phase,
    RunResult,
    RunSpec,
)
from tdd_state import FeatureContext, HarnessError, utc_now_iso

#: Loop 1 tool surface (§8): read-only exploration + Write scoped by policy.
LOOP1_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Write"]

_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "gherkin_prompt.md"
)
_system_prompt_cache: Optional[str] = None


def _system_prompt() -> str:
    """The Loop 1 system prompt, read once from references/gherkin_prompt.md.

    Byte-stable: never modified or suffixed with anything run-dependent, so
    the prompt-cache prefix survives every iteration of the loop (§12).
    """

    global _system_prompt_cache
    if _system_prompt_cache is None:
        _system_prompt_cache = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt_cache


# ---------------------------------------------------------------------------
# Budget guard (loop-local by design; Loops 2/3 own their own copies)
# ---------------------------------------------------------------------------


def _budget_report(ctx: FeatureContext) -> Optional[str]:
    """Pre-run budget check; returns a status report when a limit is exceeded.

    Checks cumulative cost, Loop 1 turns, and wall clock (anchoring
    budgets_spent.started_at on the first-ever run). Returns None when all
    budgets have headroom. The caller decides how to surface an exceeded
    budget (checkpoint outcome vs. raised HarnessError); the phase is never
    transitioned, so a human can raise the budgets and re-run.
    """

    budgets = ctx.config.budgets
    spent = ctx.state.budgets_spent
    if spent.started_at is None:
        spent.started_at = utc_now_iso()
        ctx.store.save(ctx.state)

    exceeded: list[str] = []
    if spent.cost_usd >= budgets.max_cost_usd:
        exceeded.append(
            f"cost ${spent.cost_usd:.2f} >= max ${budgets.max_cost_usd:.2f}"
        )
    if spent.turns_loop1 >= budgets.max_turns_per_loop:
        exceeded.append(
            f"loop1 turns {spent.turns_loop1} >= max {budgets.max_turns_per_loop}"
        )
    started = datetime.fromisoformat(spent.started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed_minutes = (
        datetime.now(timezone.utc) - started
    ).total_seconds() / 60.0
    if elapsed_minutes > budgets.max_wall_clock_minutes:
        exceeded.append(
            f"wall clock {elapsed_minutes:.1f}m > max "
            f"{budgets.max_wall_clock_minutes}m"
        )
    if not exceeded:
        return None
    return (
        "loop1 budget exceeded: "
        + "; ".join(exceeded)
        + f". Spent so far: ${spent.cost_usd:.2f}, "
        f"{spent.turns_loop1} loop1 turn(s), started {spent.started_at}."
    )


def _budget_checkpoint(ctx: FeatureContext) -> Optional[LoopOutcome]:
    """LoopOutcome(CHECKPOINT, BUDGET_EXCEEDED) if over budget, else None."""

    report = _budget_report(ctx)
    if report is None:
        return None
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.BUDGET_EXCEEDED,
        detail=report,
    )


# ---------------------------------------------------------------------------
# RunSpec construction and post-run persistence
# ---------------------------------------------------------------------------


def _gherkin_rel(ctx: FeatureContext) -> str:
    """The feature's gherkin dir relative to repo_root (the ALLOW_ONLY path)."""

    return ctx.gherkin_dir.relative_to(ctx.repo_root).as_posix()


def _make_spec(ctx: FeatureContext, prompt: str) -> RunSpec:
    """One Loop 1 turn-batch: gherkin model, Write scoped to gherkin/ (§8)."""

    remaining = max(
        ctx.config.budgets.max_cost_usd - ctx.state.budgets_spent.cost_usd, 0.0
    )
    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.gherkin,
        system_prompt=_system_prompt(),
        session_id=ctx.state.session_ids.get("loop1"),
        allowed_tools=list(LOOP1_ALLOWED_TOOLS),
        path_policy_mode=PathPolicyMode.ALLOW_ONLY,
        path_policy_paths=[_gherkin_rel(ctx)],
        max_turns=ctx.config.budgets.max_turns_per_loop,
        max_budget_usd=remaining,
        cwd=str(ctx.repo_root),
    )


def _persist_run(ctx: FeatureContext, result: RunResult) -> None:
    """After EVERY run: persist session id and accumulate spent budgets."""

    ctx.state.session_ids["loop1"] = result.session_id
    ctx.state.budgets_spent.cost_usd += result.cost_usd
    ctx.state.budgets_spent.turns_loop1 += result.num_turns
    ctx.store.save(ctx.state)


def _run_error(result: RunResult) -> LoopOutcome:
    return LoopOutcome(
        status=LoopStatus.FAILED,
        exit_code=ExitCode.INTERNAL_ERROR,
        detail=result.error or "agent run failed without an error message",
    )


# ---------------------------------------------------------------------------
# Detail helpers
# ---------------------------------------------------------------------------


def _feature_files(ctx: FeatureContext) -> list[str]:
    """Repo-relative paths of the .feature files currently in gherkin/."""

    if not ctx.gherkin_dir.is_dir():
        return []
    return sorted(
        p.relative_to(ctx.repo_root).as_posix()
        for p in ctx.gherkin_dir.glob("*.feature")
    )


def _listing(ctx: FeatureContext) -> str:
    files = _feature_files(ctx)
    return ", ".join(files) if files else "(none)"


def _review_detail(ctx: FeatureContext, verb: str) -> str:
    return (
        f"Gherkin {verb}: {_listing(ctx)}. Review and re-invoke with "
        "--decision approve or --feedback"
    )


# ---------------------------------------------------------------------------
# run_loop1 — phase dispatch
# ---------------------------------------------------------------------------


def run_loop1(
    ctx: FeatureContext,
    runner: AgentRunner,
    decision: Optional[str],
    feedback: Optional[str],
) -> LoopOutcome:
    """Drive Loop 1 from the feature's current phase (§8).

    DRAFTING_GHERKIN drafts (fresh feature, or re-entry after a crash);
    AWAITING_APPROVAL consumes the human's decision/feedback; any other phase
    is a dispatcher bug and fails defensively.
    """

    phase = Phase(ctx.state.phase)

    if phase is Phase.DRAFTING_GHERKIN:
        return _draft(ctx, runner)

    if phase is Phase.AWAITING_APPROVAL:
        if decision == DECISION_APPROVE:
            return _approve(ctx)
        if feedback:
            return _revise(ctx, runner, feedback)
        return LoopOutcome(
            status=LoopStatus.CHECKPOINT,
            exit_code=ExitCode.AWAITING_APPROVAL,
            detail=(
                f"Gherkin awaiting review: {_listing(ctx)}. Re-invoke with "
                "--decision approve or --feedback"
            ),
        )

    return LoopOutcome(
        status=LoopStatus.FAILED,
        exit_code=ExitCode.INTERNAL_ERROR,
        detail=f"loop1 called in phase {phase.value}",
    )


def _draft(ctx: FeatureContext, runner: AgentRunner) -> LoopOutcome:
    """First draft: task statement + feature pointers, no volatile content."""

    over = _budget_checkpoint(ctx)
    if over is not None:
        return over

    task_text = (ctx.feature_dir / TASK_FILE).read_text(encoding="utf-8")
    prompt = build_prompt(
        [
            ("Task", task_text),
            (
                "Feature",
                f"Slug: {ctx.slug}\n"
                f"Gherkin directory (absolute): {ctx.gherkin_dir}\n"
                "Write every .feature file into that directory; writes "
                "anywhere else are mechanically denied.",
            ),
        ]
    )
    result = runner.run(_make_spec(ctx, prompt))
    _persist_run(ctx, result)
    if result.is_error:
        return _run_error(result)

    ctx.store.transition(
        ctx.state, Phase.AWAITING_APPROVAL, session_id=result.session_id
    )
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.AWAITING_APPROVAL,
        detail=_review_detail(ctx, "drafted"),
    )


def _approve(ctx: FeatureContext) -> LoopOutcome:
    """Human approved: commit the spec (§16) and advance to Loop 2."""

    if not _feature_files(ctx):
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail=(
                f"nothing to approve: no .feature files in {_gherkin_rel(ctx)}"
            ),
        )
    # Commits task.md + gherkin/ (state.json under .tdd/ is gitignored).
    feature_rel = str(ctx.feature_dir.relative_to(ctx.repo_root))
    tdd_git.commit_paths(
        ctx.repo_root, COMMIT_SPEC.format(slug=ctx.slug), [feature_rel]
    )
    ctx.store.transition(
        ctx.state,
        Phase.GHERKIN_APPROVED,
        session_id=ctx.state.session_ids.get("loop1"),
    )
    return LoopOutcome(status=LoopStatus.ADVANCE, detail="spec committed")


def _revise(
    ctx: FeatureContext, runner: AgentRunner, feedback: str
) -> LoopOutcome:
    """Reviewer corrections: resume the session with the feedback turn (§12)."""

    over = _budget_checkpoint(ctx)
    if over is not None:
        return over

    ctx.store.transition(
        ctx.state,
        Phase.DRAFTING_GHERKIN,
        session_id=ctx.state.session_ids.get("loop1"),
    )
    prompt = build_prompt([("Reviewer feedback", feedback)])
    result = runner.run(_make_spec(ctx, prompt))
    _persist_run(ctx, result)
    if result.is_error:
        return _run_error(result)

    ctx.store.transition(
        ctx.state, Phase.AWAITING_APPROVAL, session_id=result.session_id
    )
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.AWAITING_APPROVAL,
        detail=_review_detail(ctx, "revised"),
    )


# ---------------------------------------------------------------------------
# amend_scenarios — §10 escalation re-entry (called by Loop 3)
# ---------------------------------------------------------------------------

_AMEND_INSTRUCTION = (
    "Amend ONLY the scenario(s) affected by this approved proposal; preserve "
    "all other scenario wording byte-for-byte. After editing, end your reply "
    "with a single line: AMENDED: <scenario_id>[, <scenario_id>...] using "
    "the <feature-file-stem>:<scenario name> format."
)


def amend_scenarios(
    ctx: FeatureContext, runner: AgentRunner, proposal: dict
) -> list[str]:
    """Resume the Loop 1 session to amend scenarios for an approved proposal.

    `proposal` carries test_file, related_scenario, reason, proposed_diff.
    Returns the amended scenario ids parsed from the agent's final
    "AMENDED:" line (last occurrence wins); falls back to — and always
    includes — proposal["related_scenario"] (deduped, order-preserving).

    Phase transitions around amendment are owned by Loop 3 — none happen
    here. Because this is a nested call inside Loop 3's escalation handling,
    budget exhaustion raises HarnessError(BUDGET_EXCEEDED) with the status
    report (Loop 3 lets it propagate to the CLI), and a failed run raises
    HarnessError(INTERNAL_ERROR).
    """

    report = _budget_report(ctx)
    if report is not None:
        raise HarnessError(ExitCode.BUDGET_EXCEEDED, report)

    formatted = (
        f"test_file: {proposal['test_file']}\n"
        f"related_scenario: {proposal['related_scenario']}\n"
        f"reason: {proposal['reason']}\n"
        "proposed_diff:\n"
        "```diff\n"
        + str(proposal["proposed_diff"]).rstrip("\n")
        + "\n```"
    )
    prompt = build_prompt(
        [
            ("Approved test-change proposal", formatted),
            ("Instruction", _AMEND_INSTRUCTION),
        ]
    )
    result = runner.run(_make_spec(ctx, prompt))
    _persist_run(ctx, result)
    if result.is_error:
        raise HarnessError(
            ExitCode.INTERNAL_ERROR,
            "loop1 amendment run failed: "
            + (result.error or "no error message"),
        )

    parsed: list[str] = []
    for line in result.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("AMENDED:"):
            parsed = [
                s.strip()
                for s in stripped[len("AMENDED:"):].split(",")
                if s.strip()
            ]

    amended: list[str] = []
    for scenario_id in [*parsed, proposal["related_scenario"]]:
        if scenario_id not in amended:
            amended.append(scenario_id)
    return amended
