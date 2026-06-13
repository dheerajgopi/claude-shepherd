"""Test-framework bootstrap — the pre-step between Loop 1 and Loop 2.

When a project has no test framework/library, tests cannot be generated and
the test command cannot run. This module adds one — with a human checkpoint —
before any test is written:

    PROPOSING_FRAMEWORK  -> draft a deterministic proposal, checkpoint (exit 16)
    AWAITING_FRAMEWORK_APPROVAL -> consume the human's approve/corrections
    INSTALLING_FRAMEWORK -> an agent declares the dependency + runs the
                            installer; the engine commits and records the test
                            command/paths, then advances to GENERATING_TESTS

The proposal is deterministic (`propose_framework`): which library, which
manifest file(s) to edit, the install command, and the resulting test command
and paths — chosen by inspecting the project's own markers, never guessed by a
model. Go and Rust ship stdlib test harnesses, so they never bootstrap; a
project with no recognized language marker has no recipe and is left untouched
(`should_bootstrap` returns False and Loop 2 proceeds as before).

The install agent's writes are mechanically scoped (ALLOW_ONLY) to the
proposal's manifest files; it never writes tests or source and never commits —
the engine owns the `tdd(<slug>): chore — add <framework>` commit. The approved
proposal is persisted to reports/framework_proposal.json so the install step
adds exactly what the human saw.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tdd_git
from tdd_agent import build_prompt
from tdd_contracts import (
    COMMIT_BOOTSTRAP,
    DECISION_APPROVE,
    AgentRunner,
    ExitCode,
    LoopOutcome,
    LoopStatus,
    PathPolicyMode,
    Phase,
    RunResult,
    RunSpec,
    WRITE_TOOLS,
)
from tdd_state import FeatureContext, save_config, utc_now_iso

_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
BOOTSTRAP_PROMPT_FILE = _REFERENCES_DIR / "bootstrap_prompt.md"

#: Human-facing proposal report + the machine copy the install step reads back.
_PROPOSAL_REPORT_NAME = "framework_proposal.md"
_PROPOSAL_DATA_NAME = "framework_proposal.json"

#: The bootstrap phases the dispatcher routes here (between Loops 1 and 2).
BOOTSTRAP_PHASES = (
    Phase.PROPOSING_FRAMEWORK,
    Phase.AWAITING_FRAMEWORK_APPROVAL,
    Phase.INSTALLING_FRAMEWORK,
)

#: Lockfiles to fold into the bootstrap commit when the installer writes them.
_LOCKFILES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)


@dataclass
class FrameworkProposal:
    """A deterministic recipe for adding a test framework to this project."""

    framework: str                  # display name, e.g. "pytest", "vitest"
    language: str                   # "python" | "javascript" | "java"
    manifest_files: list[str] = field(default_factory=list)  # repo-relative; ALLOW_ONLY scope
    install_command: str = ""       # the single command the agent runs
    test_command: str = ""          # what Loop 3 will run
    test_paths: list[str] = field(default_factory=list)      # config.test.paths
    rationale: str = ""             # one line: why this framework/tooling
    feedback: Optional[str] = None  # reviewer corrections folded into the proposal


# ---------------------------------------------------------------------------
# Deterministic detection + proposal
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return ""


def _exists_any(repo_root: Path, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if (repo_root / n).is_file()]


def _python_manifest_text(repo_root: Path) -> str:
    """Concatenated text of every Python dependency manifest, for token checks."""

    names = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")
    parts = [_read(repo_root / n) for n in names]
    for req in sorted(repo_root.glob("requirements*.txt")):
        parts.append(_read(req))
    parts.append(_read(repo_root / "Pipfile"))
    return "\n".join(parts)


def _propose_python(repo_root: Path) -> Optional[FrameworkProposal]:
    """pytest for a Python project that declares/configures no test framework."""

    py_markers = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
    has_python = bool(_exists_any(repo_root, py_markers)) or bool(
        list(repo_root.glob("requirements*.txt"))
    )
    if not has_python:
        return None

    manifest_text = _python_manifest_text(repo_root)
    if "pytest" in manifest_text:
        return None  # pytest already declared or configured — nothing to add

    pyproject = repo_root / "pyproject.toml"
    pyproject_text = _read(pyproject)
    if (repo_root / "uv.lock").is_file() or "[tool.uv" in pyproject_text:
        install = "uv add --dev pytest"
    elif (repo_root / "poetry.lock").is_file() or "[tool.poetry" in pyproject_text:
        install = "poetry add --group dev pytest"
    elif (repo_root / "Pipfile").is_file():
        install = "pipenv install --dev pytest"
    else:
        install = "pip install pytest"

    # Editable manifest scope: existing Python manifests + the conventional
    # files pytest config commonly lands in (need not exist yet).
    manifests = _exists_any(repo_root, py_markers)
    for candidate in ("pyproject.toml", "pytest.ini", "requirements-dev.txt"):
        if candidate not in manifests:
            manifests.append(candidate)

    return FrameworkProposal(
        framework="pytest",
        language="python",
        manifest_files=manifests,
        install_command=install,
        test_command="pytest",
        test_paths=["tests"],
        rationale="pytest is the de-facto standard test framework for Python.",
    )


_JS_FRAMEWORKS = ("jest", "vitest", "mocha")


def _propose_javascript(
    repo_root: Path, feedback: Optional[str]
) -> Optional[FrameworkProposal]:
    """vitest/jest for a package.json project with no test framework declared."""

    pkg_path = repo_root / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        data = json.loads(_read(pkg_path))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    if any(fw in deps for fw in _JS_FRAMEWORKS):
        return None  # a JS test framework is already present

    # Corrections may name the framework; otherwise prefer vitest on a Vite
    # project, else jest (the broadest-compatibility default).
    framework = None
    if feedback:
        for fw in _JS_FRAMEWORKS:
            if fw in feedback.lower():
                framework = fw
                break
    if framework is None:
        on_vite = "vite" in deps or bool(list(repo_root.glob("vite.config.*")))
        framework = "vitest" if on_vite else "jest"

    if (repo_root / "pnpm-lock.yaml").is_file():
        install, runner = f"pnpm add -D {framework}", "pnpm test"
    elif (repo_root / "yarn.lock").is_file():
        install, runner = f"yarn add -D {framework}", "yarn test"
    elif (repo_root / "bun.lockb").is_file():
        install, runner = f"bun add -d {framework}", "bun test"
    else:
        install, runner = f"npm install -D {framework}", "npm test"

    return FrameworkProposal(
        framework=framework,
        language="javascript",
        manifest_files=["package.json"],
        install_command=install,
        test_command=runner,
        test_paths=["tests"],
        rationale=f"{framework} is a widely-used JS/TS test framework; "
        "the project declares none.",
        feedback=feedback,
    )


def _propose_java(repo_root: Path) -> Optional[FrameworkProposal]:
    """JUnit 5 for a Maven/Gradle project whose build declares no JUnit."""

    pom = repo_root / "pom.xml"
    if pom.is_file():
        if "junit" in _read(pom).lower():
            return None
        return FrameworkProposal(
            framework="JUnit 5",
            language="java",
            manifest_files=["pom.xml"],
            install_command="mvn -q test-compile",
            test_command="mvn -q test",
            test_paths=["src/test/java"],
            rationale="JUnit 5 (junit-jupiter) is the standard Java test "
            "framework; the Maven build declares none.",
        )
    for gradle in ("build.gradle", "build.gradle.kts"):
        gpath = repo_root / gradle
        if gpath.is_file():
            if "junit" in _read(gpath).lower():
                return None
            return FrameworkProposal(
                framework="JUnit 5",
                language="java",
                manifest_files=[gradle],
                install_command="gradle testClasses",
                test_command="gradle test",
                test_paths=["src/test/java"],
                rationale="JUnit 5 (junit-jupiter) is the standard Java test "
                "framework; the Gradle build declares none.",
            )
    return None


def propose_framework(
    repo_root: Path, feedback: Optional[str] = None
) -> Optional[FrameworkProposal]:
    """The deterministic bootstrap recipe for `repo_root`, or None.

    Returns None when a test framework is already present, when the language
    ships a stdlib harness (Go, Rust), or when no recognized language marker is
    found — in all those cases the bootstrap pre-step is skipped entirely.
    """

    proposal = _propose_python(repo_root)
    if proposal is not None:
        proposal.feedback = feedback
        return proposal
    proposal = _propose_javascript(repo_root, feedback)
    if proposal is not None:
        return proposal
    proposal = _propose_java(repo_root)
    if proposal is not None:
        proposal.feedback = feedback
    return proposal


def should_bootstrap(ctx: FeatureContext) -> bool:
    """True when this project needs a test framework added before Loop 2."""

    return propose_framework(ctx.repo_root) is not None


# ---------------------------------------------------------------------------
# Budget guard (loop-local, mirrors the other loops)
# ---------------------------------------------------------------------------


def _budget_checkpoint(ctx: FeatureContext) -> Optional[LoopOutcome]:
    """CHECKPOINT/BUDGET_EXCEEDED when a budget is exhausted, else None."""

    spent = ctx.state.budgets_spent
    if spent.started_at is None:
        spent.started_at = utc_now_iso()
        ctx.store.save(ctx.state)
    budgets = ctx.config.budgets

    report: Optional[str] = None
    if spent.cost_usd >= budgets.max_cost_usd:
        report = (
            f"cost ${spent.cost_usd:.2f} >= max ${budgets.max_cost_usd:.2f}"
        )
    elif spent.turns_bootstrap >= budgets.max_turns_per_loop:
        report = (
            f"bootstrap turns {spent.turns_bootstrap} >= max "
            f"{budgets.max_turns_per_loop}"
        )
    else:
        started = datetime.fromisoformat(spent.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed_min = (
            datetime.now(timezone.utc) - started
        ).total_seconds() / 60.0
        if elapsed_min >= budgets.max_wall_clock_minutes:
            report = (
                f"wall clock {elapsed_min:.1f}m >= max "
                f"{budgets.max_wall_clock_minutes}m"
            )
    if report is None:
        return None
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.BUDGET_EXCEEDED,
        detail=f"bootstrap budget exceeded: {report} (phase: {ctx.state.phase})",
    )


# ---------------------------------------------------------------------------
# Proposal persistence + report
# ---------------------------------------------------------------------------


def _proposal_paths(ctx: FeatureContext) -> tuple[Path, Path]:
    return (
        ctx.reports_dir / _PROPOSAL_REPORT_NAME,
        ctx.reports_dir / _PROPOSAL_DATA_NAME,
    )


def _write_proposal(ctx: FeatureContext, proposal: FrameworkProposal) -> Path:
    """Persist the human report (.md) and the machine copy (.json); return .md."""

    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path, data_path = _proposal_paths(ctx)

    lines = [
        f"# Proposed test framework: {proposal.framework}",
        "",
        "This project has no test framework, so tests cannot be generated or "
        "run yet. The shepherd proposes adding one before writing any test.",
        "",
        f"- **Framework:** {proposal.framework}",
        f"- **Why:** {proposal.rationale}",
        f"- **Manifest file(s) to edit:** {', '.join(proposal.manifest_files)}",
        f"- **Install command:** `{proposal.install_command}`",
        f"- **Resulting test command:** `{proposal.test_command}`",
        f"- **Test directory:** {', '.join(proposal.test_paths)}",
    ]
    if proposal.feedback:
        lines += ["", f"_Incorporating your corrections:_ {proposal.feedback}"]
    lines += [
        "",
        "Approve to add it and commit, or reply with corrections (e.g. a "
        "different framework or install command).",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    data_path.write_text(
        json.dumps(dataclasses.asdict(proposal), indent=2), encoding="utf-8"
    )
    return report_path


def _load_proposal(ctx: FeatureContext) -> Optional[FrameworkProposal]:
    """Read back the approved proposal; fall back to a fresh deterministic one."""

    _, data_path = _proposal_paths(ctx)
    if data_path.is_file():
        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            names = {f.name for f in dataclasses.fields(FrameworkProposal)}
            return FrameworkProposal(**{k: v for k, v in raw.items() if k in names})
        except (ValueError, TypeError):
            pass
    return propose_framework(ctx.repo_root)


# ---------------------------------------------------------------------------
# Phase handlers
# ---------------------------------------------------------------------------


def _propose(ctx: FeatureContext, feedback: Optional[str]) -> LoopOutcome:
    """Draft the proposal, persist it, and checkpoint for human review."""

    proposal = propose_framework(ctx.repo_root, feedback)
    if proposal is None:
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail="bootstrap entered but no test-framework recipe applies",
        )
    report_path = _write_proposal(ctx, proposal)
    ctx.store.transition(ctx.state, Phase.AWAITING_FRAMEWORK_APPROVAL)
    return LoopOutcome(
        status=LoopStatus.CHECKPOINT,
        exit_code=ExitCode.AWAITING_FRAMEWORK_APPROVAL,
        detail=(
            f"{report_path}: proposing to add {proposal.framework}. Re-invoke "
            "with --decision approve or --feedback"
        ),
    )


def _repropose(ctx: FeatureContext, feedback: str) -> LoopOutcome:
    """Reviewer corrections: revise the proposal and checkpoint again."""

    ctx.store.transition(ctx.state, Phase.PROPOSING_FRAMEWORK)
    return _propose(ctx, feedback)


def _install_spec(ctx: FeatureContext, prompt: str, proposal: FrameworkProposal) -> RunSpec:
    """Install-agent spec: manifest-scoped writes + Bash, resumes its session."""

    return RunSpec(
        prompt=prompt,
        model=ctx.config.models.implement,
        system_prompt=BOOTSTRAP_PROMPT_FILE.read_text(encoding="utf-8"),
        session_id=ctx.state.session_ids.get("bootstrap"),
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
        path_policy_mode=PathPolicyMode.ALLOW_ONLY,
        path_policy_paths=list(proposal.manifest_files),
        max_turns=ctx.config.budgets.max_turns_per_loop,
        cwd=str(ctx.repo_root),
    )


def _commit_paths(ctx: FeatureContext, proposal: FrameworkProposal, result: RunResult) -> list[str]:
    """Repo-relative paths to stage in the bootstrap commit (existing only)."""

    repo = ctx.repo_root
    candidates: set[str] = set(proposal.manifest_files)
    candidates.update(_LOCKFILES)
    for event in result.tool_events:
        if event.tool_name in WRITE_TOOLS and not event.denied:
            raw = event.tool_input.get("file_path") or event.tool_input.get(
                "notebook_path"
            )
            if not raw:
                continue
            p = Path(raw)
            try:
                candidates.add(
                    (p if p.is_absolute() else repo / p)
                    .resolve(strict=False)
                    .relative_to(repo.resolve(strict=False))
                    .as_posix()
                )
            except ValueError:
                continue  # outside the repo — never commit it
    return sorted(c for c in candidates if (repo / c).exists())


def _record_config(ctx: FeatureContext, proposal: FrameworkProposal) -> None:
    """Record the framework's test command/paths so Loop 2/3 can use them."""

    changed = False
    if proposal.test_command and not ctx.config.test.command:
        ctx.config.test.command = proposal.test_command
        changed = True
    if proposal.test_paths and not ctx.config.test.paths:
        ctx.config.test.paths = list(proposal.test_paths)
        changed = True
    if changed:
        save_config(ctx.repo_root, ctx.config)


def _install(ctx: FeatureContext, runner: AgentRunner) -> LoopOutcome:
    """Run the install agent, commit its changes, record config, advance."""

    over = _budget_checkpoint(ctx)
    if over is not None:
        return over

    proposal = _load_proposal(ctx)
    if proposal is None:
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail="no framework proposal to install",
        )

    if Phase(ctx.state.phase) is Phase.AWAITING_FRAMEWORK_APPROVAL:
        ctx.store.transition(ctx.state, Phase.INSTALLING_FRAMEWORK)

    prompt = build_prompt(
        [
            (
                "Approved framework",
                f"Framework: {proposal.framework}\n"
                f"Language: {proposal.language}\n"
                f"Edit only these manifest file(s): "
                f"{', '.join(proposal.manifest_files)}\n"
                f"Install command to run: {proposal.install_command}\n"
                f"Resulting test command: {proposal.test_command}\n"
                f"Test directory: {', '.join(proposal.test_paths)}",
            ),
        ]
        + ([("Reviewer corrections", proposal.feedback)] if proposal.feedback else [])
    )
    result = runner.run(_install_spec(ctx, prompt, proposal))
    if result.session_id:
        ctx.state.session_ids["bootstrap"] = result.session_id
    ctx.state.budgets_spent.cost_usd += result.cost_usd
    ctx.state.budgets_spent.turns_bootstrap += result.num_turns
    ctx.store.save(ctx.state)
    if result.is_error:
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail=result.error or "bootstrap agent failed without an error message",
        )

    if not tdd_git.is_dirty(ctx.repo_root):
        return LoopOutcome(
            status=LoopStatus.FAILED,
            exit_code=ExitCode.INTERNAL_ERROR,
            detail=(
                f"bootstrap agent made no manifest changes; {proposal.framework} "
                "was not added"
            ),
        )

    tdd_git.commit_paths(
        ctx.repo_root,
        COMMIT_BOOTSTRAP.format(slug=ctx.slug, framework=proposal.framework),
        _commit_paths(ctx, proposal, result),
    )
    _record_config(ctx, proposal)
    ctx.store.transition(
        ctx.state,
        Phase.GENERATING_TESTS,
        session_id=ctx.state.session_ids.get("bootstrap"),
    )
    return LoopOutcome(
        status=LoopStatus.ADVANCE,
        detail=f"added {proposal.framework}; test command set to "
        f"`{ctx.config.test.command}`",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_bootstrap(
    ctx: FeatureContext,
    runner: AgentRunner,
    decision: Optional[str],
    feedback: Optional[str],
) -> LoopOutcome:
    """Drive the test-framework bootstrap from the feature's current phase."""

    phase = Phase(ctx.state.phase)

    if phase is Phase.REQUIREMENTS_APPROVED:
        ctx.store.transition(ctx.state, Phase.PROPOSING_FRAMEWORK)
        return _propose(ctx, None)

    if phase is Phase.PROPOSING_FRAMEWORK:
        # Crash re-entry mid-proposal: redraft the (deterministic) proposal.
        return _propose(ctx, None)

    if phase is Phase.AWAITING_FRAMEWORK_APPROVAL:
        if decision == DECISION_APPROVE:
            return _install(ctx, runner)
        if feedback:
            return _repropose(ctx, feedback)
        report_path, _ = _proposal_paths(ctx)
        return LoopOutcome(
            status=LoopStatus.CHECKPOINT,
            exit_code=ExitCode.AWAITING_FRAMEWORK_APPROVAL,
            detail=(
                f"{report_path}: framework proposal awaiting review. Re-invoke "
                "with --decision approve or --feedback"
            ),
        )

    if phase is Phase.INSTALLING_FRAMEWORK:
        # Crash re-entry mid-install: re-run the install agent (resumes session).
        return _install(ctx, runner)

    return LoopOutcome(
        status=LoopStatus.FAILED,
        exit_code=ExitCode.INTERNAL_ERROR,
        detail=f"bootstrap called in phase {phase.value}",
    )
