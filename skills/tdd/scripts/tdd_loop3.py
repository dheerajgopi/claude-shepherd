"""Loop 3 — implementation + escalation channel (§10, §12, §15, §16).

Runs the configured test command, feeds failures to the `implement` model
(main code only — the DENY_UNDER path policy makes test/requirements edits
mechanically impossible), and repeats until green. All test-modification
pressure funnels through the `propose_test_change` custom tool: the cheap
`verifier` model triages each proposal; minor/mechanical changes are applied
by THIS script (never an agent turn — the "agent cannot edit tests" invariant
holds), significant or unsure ones escalate with exit 12.

On an approved escalation, control returns to Loop 1 incrementally
(`tdd_loop1.amend_requirements`), Loop 2 re-syncs only the affected tests
(`tdd_loop2.resync_tests`), a new `red(n)` commit marks the renegotiation,
and implementation resumes.

Completion (§10) requires the test command to exit 0 AND the traceability
matrix to still validate — a deleted test cannot fake a green run.

Prompt-cache discipline (§12): the implementer session is created once and
resumed every iteration; the first prompt carries the stable context, later
turns append ONLY the volatile test output. Triage is a stateless one-shot.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import tdd_git
import tdd_wsl
from tdd_agent import build_prompt
from tdd_scan import format_convention_docs, read_convention_docs
from tdd_contracts import (
    COMMIT_GREEN,
    COMMIT_RED_AMENDED,
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
from tdd_trace import (
    _extract_first_json_object,
    bump_revisions,
    load_matrix,
    matrix_validates,
    save_matrix,
)

#: System prompts live in references/, byte-stable for prompt caching (§12).
_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
IMPL_PROMPT_FILE = _REFERENCES_DIR / "impl_prompt.md"
TRIAGE_PROMPT_FILE = _REFERENCES_DIR / "verifier_triage_prompt.md"

_OUTPUT_TAIL = 8_000          # chars of test output fed to the implementer
_TEST_FILE_CAP = 20_000       # chars of test content in the triage prompt
_TEST_COMMAND_TIMEOUT = 900   # seconds

_PROPOSAL_TOOL_NAMES = ("propose_test_change", "mcp__tdd__propose_test_change")
_BLOCKER_TOOL_NAMES = ("request_human_input", "mcp__tdd__request_human_input")
_VALID_VERDICTS = ("minor", "significant", "unsure")

_REJECT_DEFAULT = "Proposal rejected; the test stands as written."


# ---------------------------------------------------------------------------
# Budgets and run accounting (loop-3 buckets; style mirrors tdd_loop2)
# ---------------------------------------------------------------------------


def _budget_guard(ctx: FeatureContext) -> Optional[LoopOutcome]:
    """Check budgets (§10): cost, loop-3 turns, wall clock.

    Sets `budgets_spent.started_at` on first use. Returns a CHECKPOINT /
    BUDGET_EXCEEDED outcome with a short report when any limit is hit; never
    transitions phase (the human can raise budgets and re-run).
    """

    from datetime import datetime, timezone

    spent = ctx.state.budgets_spent
    if spent.started_at is None:
        spent.started_at = utc_now_iso()
        ctx.store.save(ctx.state)

    budgets = ctx.config.budgets
    report: Optional[str] = None
    if spent.cost_usd >= budgets.max_cost_usd:
        report = (
            f"cost budget exhausted: ${spent.cost_usd:.2f} spent of "
            f"${budgets.max_cost_usd:.2f} max"
        )
    elif spent.turns_loop3 >= budgets.max_turns_per_loop:
        report = (
            f"turn budget exhausted for loop 3: {spent.turns_loop3} of "
            f"{budgets.max_turns_per_loop} max"
        )
    else:
        started = datetime.fromisoformat(spent.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed_min = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        if elapsed_min >= budgets.max_wall_clock_minutes:
            report = (
                f"wall-clock budget exhausted: {elapsed_min:.1f} of "
                f"{budgets.max_wall_clock_minutes} minutes"
            )
    if report is None:
        return None
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.BUDGET_EXCEEDED,
        f"budget exceeded in loop 3 — {report} (phase: {ctx.state.phase})",
    )


def _record_run(
    ctx: FeatureContext, result: RunResult, persist_session: bool
) -> Optional[LoopOutcome]:
    """Post-run bookkeeping: session (implementer only), cost, turns, save."""

    if persist_session and result.session_id:
        ctx.state.session_ids["loop3"] = result.session_id
    ctx.state.budgets_spent.cost_usd += result.cost_usd
    ctx.state.budgets_spent.turns_loop3 += result.num_turns
    ctx.store.save(ctx.state)
    if result.is_error:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            result.error or "agent run failed without an error message",
        )
    return None


# ---------------------------------------------------------------------------
# RunSpec builders
# ---------------------------------------------------------------------------


def _requirements_rel(ctx: FeatureContext) -> str:
    return ctx.requirements_dir.relative_to(ctx.repo_root).as_posix()


def _implementer_spec(ctx: FeatureContext, prompt: str) -> RunSpec:
    """Implementer: resumed loop3 session, DENY_UNDER tests + requirements (§10)."""

    remaining = max(
        0.0, ctx.config.budgets.max_cost_usd - ctx.state.budgets_spent.cost_usd
    )
    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.implement,
        system_prompt=IMPL_PROMPT_FILE.read_text(encoding="utf-8"),
        session_id=ctx.state.session_ids.get("loop3"),
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit"],
        path_policy_mode=PathPolicyMode.DENY_UNDER,
        path_policy_paths=[*ctx.config.test.paths, _requirements_rel(ctx)],
        expose_propose_test_change=True,
        expose_request_human_input=True,
        max_turns=ctx.config.budgets.max_turns_per_loop,
        max_budget_usd=remaining or None,
        cwd=str(ctx.repo_root),
    )


def _triage_spec(ctx: FeatureContext, prompt: str) -> RunSpec:
    """Triage verifier: stateless one-shot, no tools, no path policy."""

    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.verifier,
        system_prompt=TRIAGE_PROMPT_FILE.read_text(encoding="utf-8"),
        session_id=None,
        allowed_tools=[],
        cwd=str(ctx.repo_root),
    )


# ---------------------------------------------------------------------------
# Test command
# ---------------------------------------------------------------------------


def _run_test_command(ctx: FeatureContext) -> tuple[int, str]:
    """Run the configured test command; (returncode, combined output tail).

    On a Windows host driving a WSL-filesystem repo the command is routed
    through ``wsl.exe`` (see tdd_wsl) so it runs in a login shell with the
    project's PATH; otherwise it runs natively via the local shell.
    """

    target = tdd_wsl.wsl_target(ctx.repo_root)
    if target is None:
        args, kwargs = ctx.config.test.command, {"shell": True, "cwd": ctx.repo_root}
    else:
        distro, linux_path = target
        args = tdd_wsl.shell_argv(ctx.config.test.command, distro, linux_path)
        kwargs = {}  # cwd is carried by the `cd` inside WSL; a UNC cwd is unusable
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TEST_COMMAND_TIMEOUT,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return 1, f"test command timed out after {_TEST_COMMAND_TIMEOUT}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output[-_OUTPUT_TAIL:]


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def _requirement_text(ctx: FeatureContext, requirement_id: str) -> str:
    """The related requirement's text, located by `<stem>:REQ-<nnn>` convention.

    Extracts the `## REQ-<nnn>` heading block (up to the next `##` heading)
    from the spec file. Falls back to the whole spec file, then to a
    not-found note — triage still proceeds (the verifier is told what's
    missing).
    """

    stem, _, req_id = requirement_id.partition(":")
    path = ctx.requirements_dir / f"{stem}.md"
    if not path.is_file():
        candidates = list(ctx.requirements_dir.rglob("*.md"))
        if len(candidates) == 1:
            path = candidates[0]
        else:
            return f"(requirement {requirement_id!r}: spec file not found)"
    content = path.read_text(encoding="utf-8", errors="replace")
    if req_id:
        pattern = re.compile(
            rf"^##\s+{re.escape(req_id.strip())}\b.*$", re.MULTILINE
        )
        match = pattern.search(content)
        if match:
            rest = content[match.start():]
            nxt = re.search(r"^##\s+", rest[match.end() - match.start():], re.MULTILINE)
            block = rest if nxt is None else rest[: match.end() - match.start() + nxt.start()]
            return block.rstrip()
    return content


def _triage_proposal(
    ctx: FeatureContext, runner: AgentRunner, proposal: dict[str, Any]
) -> tuple[str, str, Optional[LoopOutcome]]:
    """Triage one proposal (§10): (verdict, rationale, hard-stop outcome).

    Any parse failure or invalid verdict collapses to "unsure" — unsure
    escalates; the channel fails safe.
    """

    test_file = str(proposal.get("test_file", ""))
    test_path = ctx.repo_root / test_file
    test_content = (
        test_path.read_text(encoding="utf-8", errors="replace")[:_TEST_FILE_CAP]
        if test_path.is_file()
        else f"({test_file}: file not found)"
    )
    sections = [
        (
            "Proposal",
            f"test_file: {proposal.get('test_file', '?')}\n"
            f"related_requirement: {proposal.get('related_requirement', '?')}\n"
            f"reason: {proposal.get('reason', '?')}\n\n"
            f"```diff\n{proposal.get('proposed_diff', '')}\n```",
        ),
        (
            "Requirement",
            _requirement_text(ctx, str(proposal.get("related_requirement", ""))),
        ),
        ("Current test content", test_content),
    ]

    guard = _budget_guard(ctx)
    if guard is not None:
        return "unsure", "budget guard tripped before triage", guard
    result = runner.run(_triage_spec(ctx, build_prompt(sections)))
    failure = _record_run(ctx, result, persist_session=False)
    if failure is not None:
        return "unsure", "triage run failed", failure

    try:
        data = json.loads(_extract_first_json_object(result.text))
        verdict = str(data.get("verdict", "")).lower()
        rationale = str(data.get("rationale", ""))
    except (ValueError, AttributeError):
        return "unsure", "triage output unparseable; escalating per §10", None
    if verdict not in _VALID_VERDICTS:
        return "unsure", f"invalid triage verdict {verdict!r}; escalating", None
    return verdict, rationale, None


# ---------------------------------------------------------------------------
# Mechanical unified-diff application (minor proposals only)
# ---------------------------------------------------------------------------


def _parse_hunks(diff: str, test_file: str) -> Optional[list[tuple[int, list[str], list[str]]]]:
    """Parse a unified diff into [(old_start_line, old_lines, new_lines)].

    Returns None on ANY ambiguity: fenced noise that isn't a diff, headers
    naming a different file (or /dev/null), unknown line prefixes, malformed
    @@ headers, or a hunk with no anchor (pure insertion).
    """

    lines = diff.splitlines()
    # Tolerate a markdown fence around the diff body.
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]

    def _names_target(header_path: str) -> bool:
        p = header_path.strip()
        for prefix in ("a/", "b/"):
            if p.startswith(prefix):
                p = p[len(prefix):]
        return p == test_file

    hunks: list[tuple[int, list[str], list[str]]] = []
    old: list[str] = []
    new: list[str] = []
    old_start = 0
    in_hunk = False
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")

    def _flush() -> bool:
        if not in_hunk:
            return True
        if not old:  # pure insertion: no exact anchor to locate (§ reject rule)
            return False
        hunks.append((old_start, list(old), list(new)))
        return True

    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            target = line[4:].split("\t")[0]
            if target.strip() == "/dev/null" or not _names_target(target):
                return None
            continue
        if line.startswith("diff ") or line.startswith("index "):
            continue
        m = hunk_re.match(line)
        if m:
            if not _flush():
                return None
            old, new = [], []
            old_start = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            if line.strip():  # prose outside hunks → not a clean diff
                return None
            continue
        if line.startswith(" ") or line == "":
            old.append(line[1:])
            new.append(line[1:])
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
        elif line.startswith("\\"):  # "\ No newline at end of file"
            continue
        else:
            return None
    if not _flush():
        return None
    return hunks or None


def _apply_unified_diff(repo_root: Path, test_file: str, diff: str) -> bool:
    """Apply a unified diff to exactly `test_file`, only if every hunk
    applies cleanly. Accept/reject rules:

    - the file must exist; headers (if present) must name only `test_file`
      (a/ b/ stripped; /dev/null rejected);
    - every hunk needs >=1 context/removed line (pure insertions rejected);
    - each hunk's old block must match at the @@ line number, or at exactly
      ONE location file-wide; hunks apply in order without overlap;
    - any no-match / multiple-match / malformed input -> False, file untouched.
    """

    path = repo_root / test_file
    if not path.is_file():
        return False
    hunks = _parse_hunks(diff, test_file)
    if hunks is None:
        return False

    file_lines = path.read_text(encoding="utf-8").splitlines()
    edits: list[tuple[int, int, list[str]]] = []  # (start, end, replacement)
    floor = 0  # hunks must land in order, without overlap

    for old_start, old, new in hunks:
        hint = old_start - 1

        def _matches_at(i: int) -> bool:
            return 0 <= i and i + len(old) <= len(file_lines) and file_lines[i : i + len(old)] == old

        if _matches_at(hint) and hint >= floor:
            start = hint
        else:
            found = [
                i
                for i in range(floor, len(file_lines) - len(old) + 1)
                if _matches_at(i)
            ]
            if len(found) != 1:
                return False
            start = found[0]
        edits.append((start, start + len(old), list(new)))
        floor = start + len(old)

    for start, end, replacement in reversed(edits):
        file_lines[start:end] = replacement
    path.write_text("\n".join(file_lines) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Escalation reports
# ---------------------------------------------------------------------------


def _escalation_paths(ctx: FeatureContext) -> list[Path]:
    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    return sorted(ctx.reports_dir.glob("escalation_*.json"))


def _write_escalation(
    ctx: FeatureContext,
    proposal: dict[str, Any],
    verdict: str,
    rationale: str,
    also_proposed: list[dict[str, Any]],
    note: str = "",
) -> Path:
    """Write escalation_<n>.{md,json} to reports/; return the .md path."""

    n = len(_escalation_paths(ctx)) + 1
    md_path = ctx.reports_dir / f"escalation_{n}.md"
    json_path = ctx.reports_dir / f"escalation_{n}.json"

    body = [
        f"# Escalation {n} — proposed test change",
        "",
        f"- timestamp: {utc_now_iso()}",
        f"- test_file: {proposal.get('test_file', '?')}",
        f"- related_requirement: {proposal.get('related_requirement', '?')}",
        f"- reason: {proposal.get('reason', '?')}",
        f"- triage verdict: **{verdict}** — {rationale}",
    ]
    if note:
        body.append(f"- note: {note}")
    body += ["", "```diff", str(proposal.get("proposed_diff", "")), "```"]
    if also_proposed:
        body += ["", "## Also proposed in the same run", ""]
        for extra in also_proposed:
            body.append(
                f"- {extra.get('test_file', '?')} "
                f"({extra.get('related_requirement', '?')}): {extra.get('reason', '?')}"
            )
    md_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {"proposal": proposal, "verdict": verdict, "rationale": rationale,
             "note": note, "also_proposed": also_proposed},
            indent=2,
        ),
        encoding="utf-8",
    )
    return md_path


def _latest_escalation(ctx: FeatureContext) -> Optional[dict[str, Any]]:
    paths = _escalation_paths(ctx)
    if not paths:
        return None
    data = json.loads(paths[-1].read_text(encoding="utf-8"))
    return data.get("proposal")


# ---------------------------------------------------------------------------
# Blocker reports (the request_human_input channel)
# ---------------------------------------------------------------------------


def _blocker_paths(ctx: FeatureContext) -> list[Path]:
    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    return sorted(ctx.reports_dir.glob("blocker_*.json"))


def _write_blocker(
    ctx: FeatureContext,
    blocker: dict[str, Any],
    also_asked: list[dict[str, Any]],
) -> Path:
    """Write blocker_<n>.{md,json} to reports/; return the .md path."""

    n = len(_blocker_paths(ctx)) + 1
    md_path = ctx.reports_dir / f"blocker_{n}.md"
    json_path = ctx.reports_dir / f"blocker_{n}.json"

    body = [
        f"# Blocker {n} — implementer needs human input",
        "",
        f"- timestamp: {utc_now_iso()}",
        "",
        "## Question",
        "",
        str(blocker.get("question", "?")),
        "",
        "## Context",
        "",
        str(blocker.get("context", "?")),
    ]
    options = str(blocker.get("suggested_options", "")).strip()
    if options:
        body += ["", "## Suggested options", "", options]
    if also_asked:
        body += ["", "## Also asked in the same run", ""]
        for extra in also_asked:
            body.append(f"- {extra.get('question', '?')}")
    md_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "question": blocker.get("question", ""),
                "context": blocker.get("context", ""),
                "suggested_options": blocker.get("suggested_options", ""),
                "also_asked": also_asked,
                "timestamp": utc_now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return md_path


# ---------------------------------------------------------------------------
# Commits (tolerant on crash re-entry: skip when nothing changed)
# ---------------------------------------------------------------------------


def _commit_if_changes(repo: Path, message: str, paths: list[str]) -> bool:
    """commit_paths, but a no-op when the pathspecs hold no changes.

    Crash re-entry can land after a commit but before the phase transition;
    re-committing nothing would error. An empty pathspec is a no-op too —
    never fall through to `git add -A --` (which would stage everything).
    """

    if not paths:
        return False
    status = tdd_git.git(["status", "--porcelain", "--", *paths], repo)
    if not status.strip():
        return False
    tdd_git.commit_paths(repo, message, paths)
    return True


# ---------------------------------------------------------------------------
# Proposal processing
# ---------------------------------------------------------------------------


def _proposals_from(result: RunResult) -> list[dict[str, Any]]:
    return [
        dict(e.tool_input)
        for e in result.tool_events
        if e.tool_name in _PROPOSAL_TOOL_NAMES
    ]


def _blockers_from(result: RunResult) -> list[dict[str, Any]]:
    return [
        dict(e.tool_input)
        for e in result.tool_events
        if e.tool_name in _BLOCKER_TOOL_NAMES
    ]


def _checkpoint_blocker(
    ctx: FeatureContext, blockers: list[dict[str, Any]]
) -> LoopOutcome:
    """Write the blocker report, transition to BLOCKED, checkpoint NEEDS_INPUT.

    An explicit "I'm stuck" is a hard stop: it takes precedence over any test-
    change proposals raised in the same turn.
    """

    report = _write_blocker(ctx, blockers[0], also_asked=blockers[1:])
    ctx.store.transition(
        ctx.state,
        Phase.BLOCKED,
        session_id=ctx.state.session_ids.get("loop3"),
        reason=str(blockers[0].get("question", "")),
    )
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.NEEDS_INPUT,
        f"{report}: {blockers[0].get('question', 'human input requested')}",
    )


def _process_proposals(
    ctx: FeatureContext,
    runner: AgentRunner,
    proposals: list[dict[str, Any]],
) -> tuple[Optional[LoopOutcome], list[str]]:
    """Triage every proposal in order (§10).

    Minor ones are applied mechanically (with the matrix revision note);
    the first significant/unsure one escalates — (outcome, notes) returned.
    """

    notes: list[str] = []
    for idx, proposal in enumerate(proposals):
        verdict, rationale, hard_stop = _triage_proposal(ctx, runner, proposal)
        if hard_stop is not None:
            return hard_stop, notes

        if verdict == "minor":
            test_file = str(proposal.get("test_file", ""))
            applied = _apply_unified_diff(
                ctx.repo_root, test_file, str(proposal.get("proposed_diff", ""))
            )
            if applied:
                try:
                    matrix = load_matrix(ctx.feature_dir)
                except ValueError:
                    matrix = None
                if matrix is not None:
                    bump_revisions(
                        matrix,
                        [str(proposal.get("related_requirement", ""))],
                        kind="auto_applied_minor",
                        description=f"{test_file}: {proposal.get('reason', '')}",
                    )
                    save_matrix(ctx.feature_dir, matrix)
                notes.append(f"auto-applied minor test change to {test_file}")
                continue
            verdict, rationale = "unsure", (
                f"triage said minor but the diff did not apply cleanly to "
                f"{test_file}; escalating"
            )

        # significant / unsure → escalate (§10: only significant escalates,
        # and unsure is treated as significant).
        report = _write_escalation(
            ctx,
            proposal,
            verdict,
            rationale,
            also_proposed=proposals[idx + 1 :],
            note="" if verdict != "unsure" else "escalated as unsure",
        )
        ctx.store.transition(
            ctx.state,
            Phase.ESCALATED,
            session_id=ctx.state.session_ids.get("loop3"),
            reason=str(proposal.get("test_file", "")),
        )
        return (
            LoopOutcome(
                LoopStatus.CHECKPOINT,
                ExitCode.ESCALATED,
                f"{report}: {verdict} test-change proposal — {rationale}",
            ),
            notes,
        )
    return None, notes


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def _finalize_green(ctx: FeatureContext) -> LoopOutcome:
    """Green gate (§10): traceability must validate, then commit + DONE."""

    matrix = None
    try:
        matrix = load_matrix(ctx.feature_dir)
    except ValueError:
        pass
    if matrix is None:
        return LoopOutcome(
            LoopStatus.FAILED, ExitCode.INTERNAL_ERROR, "traceability matrix missing"
        )
    ok, det = matrix_validates(ctx.repo_root, matrix)
    if not ok:
        ctx.reports_dir.mkdir(parents=True, exist_ok=True)
        violation = ctx.reports_dir / "traceability_violation.md"
        violation.write_text(
            "# Traceability violation at green\n\n"
            f"- timestamp: {utc_now_iso()}\n\n{det}\n",
            encoding="utf-8",
        )
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            "tests green but traceability broken (a deleted/renamed test "
            f"cannot fake a green run): {det}",
        )

    _commit_if_changes(ctx.repo_root, COMMIT_GREEN.format(slug=ctx.slug), ["."])
    if Phase(ctx.state.phase) is Phase.IMPLEMENTING:
        ctx.store.transition(
            ctx.state, Phase.GREEN, session_id=ctx.state.session_ids.get("loop3")
        )
    ctx.store.transition(
        ctx.state, Phase.DONE, session_id=ctx.state.session_ids.get("loop3")
    )
    return LoopOutcome(
        LoopStatus.ADVANCE, detail="green: all tests pass, traceability intact"
    )


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------


def _main_cycle(
    ctx: FeatureContext,
    runner: AgentRunner,
    pending_sections: Optional[list[tuple[str, str]]] = None,
) -> LoopOutcome:
    """Test → (green gate | implement) → triage proposals → repeat (§15).

    `pending_sections` carries a one-off extra prompt section (the escalation
    rejection) into the next implementer turn.
    """

    notes: list[str] = []
    while True:
        guard = _budget_guard(ctx)
        if guard is not None:
            return guard

        code, output = _run_test_command(ctx)
        if code == 0:
            return _finalize_green(ctx)

        guard = _budget_guard(ctx)
        if guard is not None:
            return guard

        if ctx.state.session_ids.get("loop3") is None:
            # First-ever turn: stable context first, volatile output LAST (§12).
            sections = [
                (
                    "Context",
                    "Implement until the test suite passes. Tests and "
                    "requirements are read-only contracts. Test command: "
                    f"{ctx.config.test.command}",
                ),
            ]
            docs = format_convention_docs(read_convention_docs(ctx.repo_root))
            if docs:
                sections.append(("Repo conventions", docs))
            sections.append(("Test output", output))
        else:
            sections = [*(pending_sections or []), ("Test output", output)]
        pending_sections = None

        result = runner.run(_implementer_spec(ctx, build_prompt(sections)))
        failure = _record_run(ctx, result, persist_session=True)
        if failure is not None:
            return failure

        blockers = _blockers_from(result)
        if blockers:
            return _checkpoint_blocker(ctx, blockers)

        outcome, new_notes = _process_proposals(ctx, runner, _proposals_from(result))
        notes.extend(new_notes)
        if outcome is not None:
            if notes and outcome.exit_code is ExitCode.ESCALATED:
                outcome.detail += f" (earlier this run: {'; '.join(notes)})"
            return outcome


# ---------------------------------------------------------------------------
# Escalation resolution
# ---------------------------------------------------------------------------


def _amend_pipeline(ctx: FeatureContext, runner: AgentRunner) -> LoopOutcome:
    """Approved escalation (§10): Loop 1 amend → Loop 2 resync → red(n) →
    back to implementation. Entered from ESCALATED (after the transition to
    AMENDING_REQUIREMENTS) and from AMENDING_REQUIREMENTS crash recovery.
    """

    import tdd_loop1
    import tdd_loop2

    proposal = _latest_escalation(ctx)
    if proposal is None:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            "approval received but no escalation proposal found in reports/",
        )

    # (a) Loop 1 amends only the affected requirements. ShepherdError
    #     (e.g. BUDGET_EXCEEDED) propagates to tdd.py's top-level handler.
    amended_ids = tdd_loop1.amend_requirements(ctx, runner, proposal)

    # (b) Loop 2 re-syncs only the mapped tests.
    outcome = tdd_loop2.resync_tests(ctx, runner, amended_ids)
    if outcome.status is not LoopStatus.ADVANCE:
        return outcome  # phase stays AMENDING_REQUIREMENTS; re-run recovers here

    # (c) The renegotiation is visible in history: red(n) (§16). Only
    #     test-classified files are committed; the amended spec stays in
    #     gitignored .shepherd/.
    ctx.state.red_commit_count += 1
    n = ctx.state.red_commit_count
    ctx.store.save(ctx.state)
    _commit_if_changes(
        ctx.repo_root,
        COMMIT_RED_AMENDED.format(slug=ctx.slug, n=n),
        tdd_git.changed_files_matching(ctx.repo_root, ctx.config.test.paths),
    )

    # (d) Back to implementation.
    ctx.store.transition(ctx.state, Phase.RED_COMMITTED)
    ctx.store.transition(ctx.state, Phase.IMPLEMENTING)
    return _main_cycle(ctx, runner)


def _resolve_escalation(
    ctx: FeatureContext,
    runner: AgentRunner,
    decision: Optional[str],
    feedback: Optional[str],
) -> LoopOutcome:
    if decision == "approve":
        ctx.store.transition(ctx.state, Phase.AMENDING_REQUIREMENTS)
        return _amend_pipeline(ctx, runner)

    if decision == "reject":
        ctx.store.transition(ctx.state, Phase.IMPLEMENTING)
        rejection = ("Escalation rejected", feedback or _REJECT_DEFAULT)
        guard = _budget_guard(ctx)
        if guard is not None:
            return guard
        result = runner.run(
            _implementer_spec(ctx, build_prompt([rejection]))
        )
        failure = _record_run(ctx, result, persist_session=True)
        if failure is not None:
            return failure
        outcome, _ = _process_proposals(ctx, runner, _proposals_from(result))
        if outcome is not None:
            return outcome
        return _main_cycle(ctx, runner)

    paths = _escalation_paths(ctx)
    latest_md = (
        str(paths[-1]).replace(".json", ".md") if paths else "(no report found)"
    )
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.ESCALATED,
        f"{latest_md}: awaiting decision (--decision approve|reject)",
    )


# ---------------------------------------------------------------------------
# Blocker resolution (the request_human_input channel)
# ---------------------------------------------------------------------------


def _resolve_blocker(
    ctx: FeatureContext, runner: AgentRunner, feedback: Optional[str]
) -> LoopOutcome:
    """Resume implementation with the human's answer injected (§ blocker).

    The answer rides in as a one-off prompt section on the resumed loop3
    session, exactly like the escalation-rejection feedback; the main cycle
    then runs the test command and continues toward green.
    """

    if feedback:
        ctx.store.transition(ctx.state, Phase.IMPLEMENTING)
        return _main_cycle(ctx, runner, pending_sections=[("Human answer", feedback)])

    paths = _blocker_paths(ctx)
    latest_md = (
        str(paths[-1]).replace(".json", ".md") if paths else "(no report found)"
    )
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.NEEDS_INPUT,
        f'{latest_md}: awaiting answer (--feedback "<answer>")',
    )


# ---------------------------------------------------------------------------
# Loop 3 entry point
# ---------------------------------------------------------------------------


def run_loop3(
    ctx: FeatureContext,
    runner: AgentRunner,
    decision: Optional[str],
    feedback: Optional[str],
) -> LoopOutcome:
    """Implement until green, escalating test-change pressure (§10)."""

    try:
        phase = Phase(ctx.state.phase)
    except ValueError:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            f"unknown phase {ctx.state.phase!r}",
        )

    if phase is Phase.RED_COMMITTED:
        ctx.store.transition(ctx.state, Phase.IMPLEMENTING)
        return _main_cycle(ctx, runner)
    if phase is Phase.IMPLEMENTING:
        return _main_cycle(ctx, runner)
    if phase is Phase.ESCALATED:
        return _resolve_escalation(ctx, runner, decision, feedback)
    if phase is Phase.BLOCKED:
        return _resolve_blocker(ctx, runner, feedback)
    if phase is Phase.AMENDING_REQUIREMENTS:
        # Crash recovery mid-amendment: re-run the pipeline from the saved
        # proposal. amend_requirements may re-run against already-amended
        # requirements; the instruction is idempotent-leaning ("amend ONLY
        # what the proposal requires") and the resync re-verifies coverage.
        return _amend_pipeline(ctx, runner)
    if phase is Phase.GREEN:
        # Crash between the green commit and the DONE transition.
        ctx.store.transition(ctx.state, Phase.DONE)
        return LoopOutcome(
            LoopStatus.ADVANCE, detail="green already committed; feature done"
        )
    return LoopOutcome(
        LoopStatus.FAILED,
        ExitCode.INTERNAL_ERROR,
        f"loop 3 cannot run from phase {phase.value}",
    )
