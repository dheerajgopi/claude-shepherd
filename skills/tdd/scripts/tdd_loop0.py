"""Loop 0 — design sketch (§8 precursor).

`run_loop0` drives the draft → human review → approve/revise cycle on the
active feature before any EARS requirement exists: the design model explores
the repository read-only (Write is mechanically scoped to the feature's
design/ folder via the ALLOW_ONLY path policy), drafts a rough design sketch
(.md) — classes, functions, responsibilities, optional mermaid flowcharts —
and the loop checkpoints with exit 15 until the human approves, at which point
the dispatcher advances to Loop 1 (the sketch lives only in the machine-local,
gitignored .shepherd/ workspace).

This loop mirrors Loop 1's shape exactly (draft/revise/approve, byte-stable
system prompt, loop-local budget guard). It has no escalation re-entry: Loop 3
test-change escalations amend requirements (Loop 1), never the design.

All shared vocabulary comes from tdd_contracts; state mutations go through
ctx.store; prompt assembly through tdd_agent.build_prompt with sections
ordered stable → volatile (§12).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tdd_agent import build_prompt
from tdd_contracts import (
    DECISION_APPROVE,
    DESIGN_FILE_GLOB,
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
from tdd_state import FeatureContext, utc_now_iso

#: Loop 0 tool surface: read-only exploration + Write scoped by policy.
LOOP0_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Write"]

_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "design_prompt.md"
)
_system_prompt_cache: Optional[str] = None


def _system_prompt() -> str:
    """The Loop 0 system prompt, read once from references/design_prompt.md.

    Byte-stable: never modified or suffixed with anything run-dependent, so
    the prompt-cache prefix survives every iteration of the loop (§12).
    """

    global _system_prompt_cache
    if _system_prompt_cache is None:
        _system_prompt_cache = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt_cache


# ---------------------------------------------------------------------------
# Budget guard (loop-local by design; Loops 1/2/3 own their own copies)
# ---------------------------------------------------------------------------


def _budget_report(ctx: FeatureContext) -> Optional[str]:
    """Pre-run budget check; returns a status report when a limit is exceeded.

    Checks cumulative cost, Loop 0 turns, and wall clock (anchoring
    budgets_spent.started_at on the first-ever run). Returns None when all
    budgets have headroom. The phase is never transitioned, so a human can
    raise the budgets and re-run.
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
    if spent.turns_loop0 >= budgets.max_turns_per_loop:
        exceeded.append(
            f"loop0 turns {spent.turns_loop0} >= max {budgets.max_turns_per_loop}"
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
        "loop0 budget exceeded: "
        + "; ".join(exceeded)
        + f". Spent so far: ${spent.cost_usd:.2f}, "
        f"{spent.turns_loop0} loop0 turn(s), started {spent.started_at}."
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


def _design_rel(ctx: FeatureContext) -> str:
    """The feature's design dir relative to repo_root (the ALLOW_ONLY path)."""

    return ctx.design_dir.relative_to(ctx.repo_root).as_posix()


def _make_spec(ctx: FeatureContext, prompt: str) -> RunSpec:
    """One Loop 0 turn-batch: design model, Write scoped to design/."""

    remaining = max(
        ctx.config.budgets.max_cost_usd - ctx.state.budgets_spent.cost_usd, 0.0
    )
    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.design,
        system_prompt=_system_prompt(),
        session_id=ctx.state.session_ids.get("loop0"),
        allowed_tools=list(LOOP0_ALLOWED_TOOLS),
        path_policy_mode=PathPolicyMode.ALLOW_ONLY,
        path_policy_paths=[_design_rel(ctx)],
        max_turns=ctx.config.budgets.max_turns_per_loop,
        max_budget_usd=remaining,
        cwd=str(ctx.repo_root),
    )


def _persist_run(ctx: FeatureContext, result: RunResult) -> None:
    """After EVERY run: persist session id and accumulate spent budgets."""

    ctx.state.session_ids["loop0"] = result.session_id
    ctx.state.budgets_spent.cost_usd += result.cost_usd
    ctx.state.budgets_spent.turns_loop0 += result.num_turns
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


def _design_files(ctx: FeatureContext) -> list[str]:
    """Repo-relative paths of the design sketch files currently in design/."""

    if not ctx.design_dir.is_dir():
        return []
    return sorted(
        p.relative_to(ctx.repo_root).as_posix()
        for p in ctx.design_dir.glob(DESIGN_FILE_GLOB)
    )


def _listing(ctx: FeatureContext) -> str:
    files = _design_files(ctx)
    return ", ".join(files) if files else "(none)"


def _review_detail(ctx: FeatureContext, verb: str) -> str:
    return (
        f"Design {verb}: {_listing(ctx)}. Review and re-invoke with "
        "--decision approve or --feedback"
    )


# ---------------------------------------------------------------------------
# run_loop0 — phase dispatch
# ---------------------------------------------------------------------------


def run_loop0(
    ctx: FeatureContext,
    runner: AgentRunner,
    decision: Optional[str],
    feedback: Optional[str],
) -> LoopOutcome:
    """Drive Loop 0 from the feature's current phase.

    SKETCHING_DESIGN drafts (fresh feature, or re-entry after a crash);
    AWAITING_DESIGN_APPROVAL consumes the human's decision/feedback; any other
    phase is a dispatcher bug and fails defensively.
    """

    phase = Phase(ctx.state.phase)

    if phase is Phase.SKETCHING_DESIGN:
        return _draft(ctx, runner)

    if phase is Phase.AWAITING_DESIGN_APPROVAL:
        if decision == DECISION_APPROVE:
            return _approve(ctx)
        if feedback:
            return _revise(ctx, runner, feedback)
        return LoopOutcome(
            status=LoopStatus.CHECKPOINT,
            exit_code=ExitCode.AWAITING_DESIGN_APPROVAL,
            detail=(
                f"Design awaiting review: {_listing(ctx)}. Re-invoke with "
                "--decision approve or --feedback"
            ),
        )

    return LoopOutcome(
        status=LoopStatus.FAILED,
        exit_code=ExitCode.INTERNAL_ERROR,
        detail=f"loop0 called in phase {phase.value}",
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
                f"Design directory (absolute): {ctx.design_dir}\n"
                "Write every design sketch (.md) file into that directory; "
                "writes anywhere else are mechanically denied.",
            ),
        ]
    )
    result = runner.run(_make_spec(ctx, prompt))
    _persist_run(ctx, result)
    if result.is_error:
        return _run_error(result)

    ctx.store.transition(
        ctx.state, Phase.AWAITING_DESIGN_APPROVAL, session_id=result.session_id
    )
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.AWAITING_DESIGN_APPROVAL,
        detail=_review_detail(ctx, "drafted"),
    )


def _approve(ctx: FeatureContext) -> LoopOutcome:
    """Human approved: advance to Loop 1 (.shepherd/ is gitignored — no commit)."""

    if not _design_files(ctx):
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail=f"nothing to approve: no design files in {_design_rel(ctx)}",
        )
    ctx.store.transition(
        ctx.state,
        Phase.DESIGN_APPROVED,
        session_id=ctx.state.session_ids.get("loop0"),
    )
    return LoopOutcome(status=LoopStatus.ADVANCE, detail="design approved")


def _revise(
    ctx: FeatureContext, runner: AgentRunner, feedback: str
) -> LoopOutcome:
    """Reviewer corrections: resume the session with the feedback turn (§12)."""

    over = _budget_checkpoint(ctx)
    if over is not None:
        return over

    ctx.store.transition(
        ctx.state,
        Phase.SKETCHING_DESIGN,
        session_id=ctx.state.session_ids.get("loop0"),
    )
    prompt = build_prompt([("Reviewer feedback", feedback)])
    result = runner.run(_make_spec(ctx, prompt))
    _persist_run(ctx, result)
    if result.is_error:
        return _run_error(result)

    ctx.store.transition(
        ctx.state, Phase.AWAITING_DESIGN_APPROVAL, session_id=result.session_id
    )
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.AWAITING_DESIGN_APPROVAL,
        detail=_review_detail(ctx, "revised"),
    )
