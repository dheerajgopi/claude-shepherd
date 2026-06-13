"""Loop 2 — test generation + coverage verification (§9, §12).

Turns approved EARS requirements into executable tests using the `testgen`
model, with the mechanical ALLOW_ONLY path policy restricting writes to the
configured test paths, then verifies coverage with the cheap `verifier`
model, which emits the traceability matrix (requirement → tests). Generation
iterates on the gaps; full coverage produces the red commit (`tdd(<slug>):
red — failing tests`) — the recovery anchor created BEFORE Loop 3 begins.

Also owns `resync_tests` (§10): after an approved escalation amendment,
re-sync ONLY the tests mapped to the amended requirements, bumping their
matrix revisions. Loop 3 owns the phases and the red(n) commit on that path.

Prompt-cache discipline (§12): the generator session is created once and
resumed on every subsequent iteration; the first prompt carries the stable
sections (approved requirements, convention scan), later turns append ONLY
the volatile coverage gaps. The verifier is stateless — one fresh session per
pass. System prompts are loaded byte-stable from references/.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import tdd_git
import tdd_wsl
from tdd_agent import build_prompt
from tdd_contracts import (
    COMMIT_RED,
    COVERAGE_COVERED,
    DESIGN_FILE_GLOB,
    SPEC_FILE_GLOB,
    WRITE_TOOLS,
    AgentRunner,
    ExitCode,
    LoopOutcome,
    LoopStatus,
    PathPolicyMode,
    Phase,
    RequirementTrace,
    RunResult,
    RunSpec,
    TraceabilityMatrix,
)
from tdd_scan import _TEST_FILE_PATTERNS, format_convention_docs, scan_conventions
from tdd_state import FeatureContext, utc_now_iso
from tdd_trace import (
    bump_revisions,
    gap_report,
    load_matrix,
    matrix_fully_covered,
    parse_verifier_matrix,
    save_matrix,
)

#: System prompts live in references/, byte-stable for prompt caching (§12).
_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
TESTGEN_PROMPT_FILE = _REFERENCES_DIR / "testgen_prompt.md"
VERIFIER_PROMPT_FILE = _REFERENCES_DIR / "verifier_coverage_prompt.md"

_EXEMPLAR_CAP = 10_000      # chars of exemplar test file in the scan summary
_TEST_FILE_CAP = 20_000     # chars per test file in the verifier prompt
_GAP_REPORT_NAME = "coverage_gap.md"

_RESYNC_INSTRUCTION = (
    "The listed requirements were amended after an approved escalation. "
    "Update ONLY their mapped tests to match the amended expectations; do "
    "not touch other tests."
)


# ---------------------------------------------------------------------------
# Budgets and run accounting
# ---------------------------------------------------------------------------


def _budget_guard(ctx: FeatureContext) -> Optional[LoopOutcome]:
    """Check budgets before a run (§10): cost, loop-2 turns, wall clock.

    Sets `budgets_spent.started_at` on first use. Returns a CHECKPOINT /
    BUDGET_EXCEEDED outcome with a short report when any limit is hit; never
    transitions phase.
    """

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
    elif spent.turns_loop2 >= budgets.max_turns_per_loop:
        report = (
            f"turn budget exhausted for loop 2: {spent.turns_loop2} of "
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
        f"budget exceeded in loop 2 — {report} (phase: {ctx.state.phase})",
    )


def _record_run(
    ctx: FeatureContext, result: RunResult, persist_session: bool
) -> Optional[LoopOutcome]:
    """Post-run bookkeeping: session (generator only), cost, turns, save.

    Returns a FAILED outcome when the run itself errored.
    """

    if persist_session and result.session_id:
        ctx.state.session_ids["loop2"] = result.session_id
    ctx.state.budgets_spent.cost_usd += result.cost_usd
    ctx.state.budgets_spent.turns_loop2 += result.num_turns
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


def _generator_spec(ctx: FeatureContext, prompt: str, system_prompt: str) -> RunSpec:
    """Test-generator spec: resumes the loop2 session, ALLOW_ONLY test paths."""

    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.testgen,
        system_prompt=system_prompt,
        session_id=ctx.state.session_ids.get("loop2"),
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit"],
        path_policy_mode=PathPolicyMode.ALLOW_ONLY,
        path_policy_paths=list(ctx.config.test.paths),
        max_turns=ctx.config.budgets.max_turns_per_loop,
        cwd=str(ctx.repo_root),
    )


def _verifier_spec(ctx: FeatureContext, prompt: str, system_prompt: str) -> RunSpec:
    """Verifier spec: stateless one-shot, no tools, no path policy."""

    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.verifier,
        system_prompt=system_prompt,
        session_id=None,
        allowed_tools=[],
        cwd=str(ctx.repo_root),
    )


# ---------------------------------------------------------------------------
# Prompt content
# ---------------------------------------------------------------------------


def _requirements_content(ctx: FeatureContext) -> str:
    """All EARS spec files under requirements/, sorted, as one section."""

    parts = []
    for path in sorted(ctx.requirements_dir.rglob(SPEC_FILE_GLOB)):
        rel = path.relative_to(ctx.requirements_dir).as_posix()
        parts.append(f"### {rel}\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts) or "(no spec files found)"


def _design_content(ctx: FeatureContext) -> str:
    """The approved design sketch(es), or "" when none (older feature).

    The design names the concrete classes/functions to be built; feeding it to
    the generator lets it write unit tests bound to those real units, while the
    requirements drive WHICH behaviors must be covered.
    """

    if not ctx.design_dir.is_dir():
        return ""
    parts = []
    for path in sorted(ctx.design_dir.glob(DESIGN_FILE_GLOB)):
        parts.append(f"### {path.name}\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def _scan_summary(ctx: FeatureContext) -> str:
    """Deterministic convention pre-scan (§9), composed for the prompt."""

    scan = scan_conventions(ctx.repo_root)
    lines = [
        f"framework: {scan.framework or '(none detected)'}",
        f"test command: {ctx.config.test.command or scan.test_command or '(none detected)'}",
        "test paths (writes are mechanically restricted to these): "
        + (", ".join(ctx.config.test.paths) or "(none configured)"),
        "notes:",
        *(f"  - {note}" for note in scan.notes),
    ]
    if scan.exemplar_test is not None:
        exemplar_path = ctx.repo_root / scan.exemplar_test
        if exemplar_path.is_file():
            content = exemplar_path.read_text(encoding="utf-8", errors="replace")
            lines += [
                "",
                f"exemplar test file: {scan.exemplar_test}",
                "",
                "```",
                content[:_EXEMPLAR_CAP],
                "```",
            ]
    docs = format_convention_docs(scan.convention_docs)
    if docs:
        lines += ["", docs]
    return "\n".join(lines)


def _touched_paths(result: RunResult) -> set[Path]:
    """Paths the generator successfully wrote/edited during a run."""

    out: set[Path] = set()
    for event in result.tool_events:
        if event.tool_name in WRITE_TOOLS and not event.denied:
            raw = event.tool_input.get("file_path") or event.tool_input.get(
                "notebook_path"
            )
            if raw:
                out.add(Path(raw))
    return out


def _tests_section(ctx: FeatureContext, touched: Iterable[Path]) -> str:
    """Contents of every test file: generator-touched + pattern matches.

    Files are read from disk (the matrix must reflect what is actually on
    disk), each capped at 20000 chars.
    """

    repo = ctx.repo_root.resolve(strict=False)
    files: set[Path] = set()
    for raw in touched:
        p = Path(raw)
        if not p.is_absolute():
            p = repo / p
        p = p.resolve(strict=False)
        if p.is_file():
            files.add(p)
    for rel in ctx.config.test.paths:
        base = repo / rel
        if not base.is_dir():
            continue
        for pattern in _TEST_FILE_PATTERNS:
            files.update(q.resolve(strict=False) for q in base.rglob(pattern))

    parts = []
    for path in sorted(files):
        try:
            rel_name = path.relative_to(repo).as_posix()
        except ValueError:
            continue  # outside the repo; never feed it to the verifier
        content = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"### {rel_name}\n\n```\n{content[:_TEST_FILE_CAP]}\n```")
    return "\n\n".join(parts) or "(no test files found under the configured test paths)"


def _gap_text(matrix: TraceabilityMatrix) -> str:
    """Gap section for the resumed generator: non-covered requirements + notes."""

    lines = []
    for s in matrix.requirements:
        if s.status != COVERAGE_COVERED or not s.tests:
            line = f"- {s.requirement_id}: {s.status}"
            if s.notes:
                line += f" — {s.notes}"
            lines.append(line)
    return "\n".join(lines)


def _reentry_gap_text(ctx: FeatureContext) -> str:
    """Gap text for a crash re-entry mid-iteration (session exists, no gaps yet)."""

    try:
        matrix = load_matrix(ctx.feature_dir)
    except ValueError:
        matrix = None
    if matrix is not None:
        text = _gap_text(matrix)
        if text:
            return text
    return (
        "Resuming after an interruption. Re-check the generated tests and "
        "ensure every requirement is fully covered."
    )


# ---------------------------------------------------------------------------
# Verifier pass (one-shot, bounded JSON-parse retry)
# ---------------------------------------------------------------------------


def _verifier_pass(
    ctx: FeatureContext,
    runner: AgentRunner,
    system_prompt: str,
    sections: list[tuple[str, str]],
) -> tuple[Optional[list[RequirementTrace]], Optional[LoopOutcome], Optional[str]]:
    """One verifier pass with a single parse retry.

    Returns exactly one of: (parsed traces, None, None) on success,
    (None, outcome, None) on a hard stop (budget / run error), or
    (None, None, note) when the output stayed unparseable after the retry.
    """

    guard = _budget_guard(ctx)
    if guard is not None:
        return None, guard, None
    result = runner.run(_verifier_spec(ctx, build_prompt(sections), system_prompt))
    failure = _record_run(ctx, result, persist_session=False)
    if failure is not None:
        return None, failure, None
    try:
        return parse_verifier_matrix(result.text), None, None
    except ValueError as first_err:
        guard = _budget_guard(ctx)
        if guard is not None:
            return None, guard, None
        retry_sections = [*sections, ("Parse error", str(first_err))]
        result = runner.run(
            _verifier_spec(ctx, build_prompt(retry_sections), system_prompt)
        )
        failure = _record_run(ctx, result, persist_session=False)
        if failure is not None:
            return None, failure, None
        try:
            return parse_verifier_matrix(result.text), None, None
        except ValueError as second_err:
            return None, None, (
                f"verifier output unparseable after one retry: {second_err}"
            )


# ---------------------------------------------------------------------------
# Matrix merge
# ---------------------------------------------------------------------------


def _spec_file_exists(ctx: FeatureContext, rel: str) -> bool:
    return (ctx.requirements_dir / rel).is_file() or (ctx.repo_root / rel).is_file()


def _merge_into_matrix(
    ctx: FeatureContext, matrix: TraceabilityMatrix, parsed: list[RequirementTrace]
) -> None:
    """Merge a verifier pass into the persistent matrix, preserving revisions.

    Existing requirement_ids keep their `revision` (and the matrix keeps its
    revisions log) but take the freshly reported tests/status/notes/
    spec_file; new ids are appended. Requirements the verifier no longer
    reports are removed only when their spec file is gone, else kept with
    a note.
    """

    existing = {s.requirement_id: s for s in matrix.requirements}
    reported: set[str] = set()
    for trace in parsed:
        reported.add(trace.requirement_id)
        old = existing.get(trace.requirement_id)
        if old is not None:
            old.spec_file = trace.spec_file
            old.tests = list(trace.tests)
            old.status = trace.status
            old.notes = trace.notes
        else:
            matrix.requirements.append(trace)

    kept: list[RequirementTrace] = []
    for requirement in matrix.requirements:
        if requirement.requirement_id in reported:
            kept.append(requirement)
        elif _spec_file_exists(ctx, requirement.spec_file):
            stale = "not reported by the verifier in the latest pass"
            requirement.notes = (
                f"{requirement.notes}; {stale}" if requirement.notes else stale
            )
            kept.append(requirement)
        # else: spec file gone — the requirement no longer exists; drop it.
    matrix.requirements[:] = kept


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def _syntax_errors(ctx: FeatureContext, matrix: TraceabilityMatrix) -> str:
    """Syntax-only py_compile check on every unique mapped test file (§9)."""

    files = sorted(
        {
            ref.split("::", 1)[0]
            for s in matrix.requirements
            for ref in s.tests
            if "::" in ref
        }
    )
    target = tdd_wsl.wsl_target(ctx.repo_root)
    problems = []
    for rel in files:
        if target is None:
            args, kwargs = [sys.executable, "-m", "py_compile", rel], {"cwd": ctx.repo_root}
        else:
            distro, linux_path = target
            abs_rel = linux_path.rstrip("/") + "/" + rel
            args = tdd_wsl.exec_argv(distro, ["python3", "-m", "py_compile", abs_rel])
            kwargs = {}  # absolute path carries the location; a UNC cwd is unusable
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            **kwargs,
        )
        if proc.returncode != 0:
            problems.append(f"{rel}:\n{(proc.stderr or proc.stdout).strip()}")
    return "\n\n".join(problems)


def _red_commit(ctx: FeatureContext, matrix: TraceabilityMatrix) -> LoopOutcome:
    """Full coverage: red commit (§9, BEFORE Loop 3 — the recovery anchor).

    Only test paths are committed; the feature folder (requirements,
    traceability) lives under the gitignored .shepherd/ workspace.
    """

    tdd_git.commit_paths(
        ctx.repo_root,
        COMMIT_RED.format(slug=ctx.slug),
        list(ctx.config.test.paths),
    )
    ctx.state.red_commit_count = 1
    ctx.store.transition(
        ctx.state,
        Phase.RED_COMMITTED,
        session_id=ctx.state.session_ids.get("loop2"),
    )
    requirement_count = len(matrix.requirements)
    test_count = len({t for s in matrix.requirements for t in s.tests})
    return LoopOutcome(
        LoopStatus.ADVANCE,
        detail=(
            f"red committed: {requirement_count} requirements covered by "
            f"{test_count} tests"
        ),
    )


def _write_gap_report(
    ctx: FeatureContext,
    matrix: TraceabilityMatrix,
    iteration_notes: list[str],
) -> tuple[Path, str]:
    """Write coverage_gap.md to reports/; return (path, one-line summary)."""

    report = gap_report(matrix)
    if iteration_notes:
        report += "\n## Iteration notes\n\n"
        report += "\n\n".join(f"- {note}" for note in iteration_notes) + "\n"
    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = ctx.reports_dir / _GAP_REPORT_NAME
    report_path.write_text(report, encoding="utf-8")
    gaps = [
        s for s in matrix.requirements if s.status != COVERAGE_COVERED or not s.tests
    ]
    summary = (
        f"{len(gaps)} of {len(matrix.requirements)} requirement(s) not fully covered"
    )
    return report_path, summary


def _gap_exit(
    ctx: FeatureContext,
    matrix: Optional[TraceabilityMatrix],
    iteration_notes: list[str],
    iterations: int,
) -> LoopOutcome:
    """Iterations exhausted with gaps: gap report + CHECKPOINT/COVERAGE_GAP."""

    if matrix is None:
        try:
            matrix = load_matrix(ctx.feature_dir)
        except ValueError:
            matrix = None
    if matrix is None:
        matrix = TraceabilityMatrix(slug=ctx.slug)
    report_path, summary = _write_gap_report(ctx, matrix, iteration_notes)
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.COVERAGE_GAP,
        f"{report_path}: {summary} after {iterations} iteration(s)",
    )


# ---------------------------------------------------------------------------
# Loop 2 entry point
# ---------------------------------------------------------------------------


def run_loop2(ctx: FeatureContext, runner: AgentRunner) -> LoopOutcome:
    """Generate tests from approved requirements and verify coverage (§9)."""

    try:
        phase = Phase(ctx.state.phase)
    except ValueError:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            f"unknown phase {ctx.state.phase!r}",
        )
    if phase is Phase.REQUIREMENTS_APPROVED:
        ctx.store.transition(ctx.state, Phase.GENERATING_TESTS)
    elif phase not in (Phase.GENERATING_TESTS, Phase.VERIFYING_COVERAGE):
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            f"loop 2 cannot run from phase {phase.value}",
        )
    # GENERATING_TESTS / VERIFYING_COVERAGE: crash re-entry, continue below.

    generator_system = TESTGEN_PROMPT_FILE.read_text(encoding="utf-8")
    verifier_system = VERIFIER_PROMPT_FILE.read_text(encoding="utf-8")
    requirements = _requirements_content(ctx)
    design = _design_content(ctx)  # approved design sketch (§8 precursor)
    scan_summary = _scan_summary(ctx)  # deterministic pre-scan (§9)

    gap_text: Optional[str] = None
    iteration_notes: list[str] = []
    touched: set[Path] = set()
    matrix: Optional[TraceabilityMatrix] = None
    max_iterations = ctx.config.budgets.max_coverage_iterations

    for iteration in range(1, max_iterations + 1):
        # a. Generator turn.
        if Phase(ctx.state.phase) is Phase.VERIFYING_COVERAGE:
            ctx.store.transition(ctx.state, Phase.GENERATING_TESTS)
        guard = _budget_guard(ctx)
        if guard is not None:
            return guard
        if ctx.state.session_ids.get("loop2") is None:
            # First-ever turn: stable sections only (§12 cache prefix).
            sections = [("Approved requirements", requirements)]
            if design:
                sections.append(("Approved design", design))
            sections.append(("Project test conventions", scan_summary))
        else:
            # Resumed session: ONLY the volatile coverage gaps.
            sections = [("Coverage gaps", gap_text or _reentry_gap_text(ctx))]
        result = runner.run(
            _generator_spec(ctx, build_prompt(sections), generator_system)
        )
        failure = _record_run(ctx, result, persist_session=True)
        if failure is not None:
            return failure
        touched |= _touched_paths(result)

        # b. Verifier turn (stateless one-shot, bounded parse retry).
        ctx.store.transition(
            ctx.state,
            Phase.VERIFYING_COVERAGE,
            session_id=ctx.state.session_ids.get("loop2"),
        )
        verifier_sections = [
            ("Requirements", requirements),
            ("Tests", _tests_section(ctx, touched)),
        ]
        parsed, outcome, parse_note = _verifier_pass(
            ctx, runner, verifier_system, verifier_sections
        )
        if outcome is not None:
            return outcome
        if parsed is None:
            # Failed iteration: counts against max_coverage_iterations.
            iteration_notes.append(f"iteration {iteration}: {parse_note}")
            gap_text = (
                "The coverage verifier could not produce a parseable matrix "
                "last iteration. Re-check that every requirement has a clearly "
                "named test tagged with its `# requirement:` comment.\n"
                f"({parse_note})"
            )
            continue

        # c. Merge into the persistent matrix (revisions preserved).
        try:
            matrix = load_matrix(ctx.feature_dir) or TraceabilityMatrix(slug=ctx.slug)
        except ValueError as exc:
            return LoopOutcome(LoopStatus.FAILED, ExitCode.INTERNAL_ERROR, str(exc))
        _merge_into_matrix(ctx, matrix, parsed)
        save_matrix(ctx.feature_dir, matrix)

        # d. Fully covered → optional syntax check, then the red commit.
        if matrix_fully_covered(matrix):
            if ctx.config.test.syntax_check:
                errors = _syntax_errors(ctx, matrix)
                if errors:
                    iteration_notes.append(
                        f"iteration {iteration}: syntax check failed:\n{errors}"
                    )
                    gap_text = (
                        "All requirements are covered, but these test files fail "
                        "a syntax-only compile check. Fix the syntax errors in "
                        "the test code itself; import errors for "
                        "not-yet-implemented modules are expected and are NOT "
                        "the problem here.\n\n" + errors
                    )
                    continue  # counts as an iteration
            return _red_commit(ctx, matrix)

        # e. Gaps remain → iterate with the gap text as the next turn.
        gap_text = _gap_text(matrix)

    # 4. Iterations exhausted with gaps. Phase stays VERIFYING_COVERAGE.
    return _gap_exit(ctx, matrix, iteration_notes, max_iterations)


# ---------------------------------------------------------------------------
# Re-sync after an approved escalation amendment (§10) — called by Loop 3
# ---------------------------------------------------------------------------


def resync_tests(
    ctx: FeatureContext, runner: AgentRunner, requirement_ids: list[str]
) -> LoopOutcome:
    """Re-sync ONLY the tests mapped to the amended requirements (§10).

    No phase transitions and no commit here — Loop 3 owns both (the red(n)
    commit per the §15 flow).
    """

    try:
        matrix = load_matrix(ctx.feature_dir)
    except ValueError as exc:
        return LoopOutcome(LoopStatus.FAILED, ExitCode.INTERNAL_ERROR, str(exc))
    if matrix is None:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            "traceability matrix missing; cannot resync tests",
        )

    guard = _budget_guard(ctx)
    if guard is not None:
        return guard

    ids = list(requirement_ids)
    id_set = set(ids)
    rows = [s for s in matrix.requirements if s.requirement_id in id_set]

    # The spec files containing the amended requirements: from their matrix
    # rows, falling back to the <stem>:REQ-<nnn> convention for unknown ids.
    spec_files = sorted({s.spec_file for s in rows})
    known = {s.requirement_id for s in rows}
    for rid in ids:
        if rid not in known and ":" in rid:
            candidate = rid.split(":", 1)[0] + ".md"
            if (ctx.requirements_dir / candidate).is_file() and candidate not in spec_files:
                spec_files.append(candidate)

    amended = "\n\n".join(
        f"### {name}\n\n{(ctx.requirements_dir / name).read_text(encoding='utf-8')}"
        for name in spec_files
        if (ctx.requirements_dir / name).is_file()
    ) or "(no spec files found for the given requirement ids)"
    affected = "\n".join(
        f"- {s.requirement_id} -> {', '.join(s.tests) or '(no tests mapped)'}"
        for s in rows
    ) or "(no matrix rows for the given requirement ids)"

    # Generator: resume the loop2 session with the scoped amendment turn.
    generator_system = TESTGEN_PROMPT_FILE.read_text(encoding="utf-8")
    sections = [
        ("Amended requirements", amended),
        ("Affected tests", affected),
        ("Instruction", _RESYNC_INSTRUCTION),
    ]
    result = runner.run(
        _generator_spec(ctx, build_prompt(sections), generator_system)
    )
    failure = _record_run(ctx, result, persist_session=True)
    if failure is not None:
        return failure

    # One verifier pass (full requirements + all tests: simpler, still correct).
    verifier_system = VERIFIER_PROMPT_FILE.read_text(encoding="utf-8")
    verifier_sections = [
        ("Requirements", _requirements_content(ctx)),
        ("Tests", _tests_section(ctx, _touched_paths(result))),
    ]
    parsed, outcome, parse_note = _verifier_pass(
        ctx, runner, verifier_system, verifier_sections
    )
    if outcome is not None:
        return outcome
    if parsed is None:
        return LoopOutcome(
            LoopStatus.FAILED,
            ExitCode.INTERNAL_ERROR,
            f"resync failed: {parse_note}",
        )

    _merge_into_matrix(ctx, matrix, parsed)
    bump_revisions(
        matrix,
        ids,
        kind="resync",
        description=(
            "tests re-synced after approved escalation amendment of: "
            + ", ".join(ids)
        ),
    )
    save_matrix(ctx.feature_dir, matrix)

    affected_rows = [s for s in matrix.requirements if s.requirement_id in id_set]
    if affected_rows and all(
        s.status == COVERAGE_COVERED and s.tests for s in affected_rows
    ):
        return LoopOutcome(
            LoopStatus.ADVANCE,
            detail=(
                f"resync complete: {len(affected_rows)} amended requirement(s) "
                "covered again"
            ),
        )

    report_path, summary = _write_gap_report(ctx, matrix, [])
    return LoopOutcome(
        LoopStatus.CHECKPOINT,
        ExitCode.COVERAGE_GAP,
        f"{report_path}: amended requirements not fully covered after resync "
        f"({summary})",
    )
