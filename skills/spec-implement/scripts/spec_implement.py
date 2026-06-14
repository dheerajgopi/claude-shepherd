#!/usr/bin/env python3
"""spec-implement shepherd CLI — the phased orchestrator entry point (§6, §13).

Subcommands per the pinned grammar in docs/contracts.md:

    spec_implement.py init [--force]
    spec_implement.py new <title...> [--task-stdin]
    spec_implement.py run [--feature SLUG] [--force] [--decision approve|reject] [--feedback TEXT]
               [--verbose | --no-verbose]
    spec_implement.py status [--json]

All informational output goes to stdout, errors to stderr; the exit code is
the protocol (spec_implement_contracts.ExitCode). Sibling modules are importable because
this file's directory lands on sys.path when it is executed as a script.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

import spec_implement_git
from spec_implement_contracts import (
    BRANCH_PREFIX,
    CONFIG_FILE,
    DECISION_APPROVE,
    DECISION_REJECT,
    DESIGN_DIR,
    FEATURES_DIR,
    REQUIREMENTS_DIR,
    GITIGNORE_ENTRIES,
    SHEPHERD_DIR,
    REPORTS_DIR,
    TASK_FILE,
    ExitCode,
    FeatureState,
    ShepherdConfig,
    HistoryEntry,
    LoopStatus,
    Phase,
    RESUMABLE_PHASES,
)
from spec_implement_scan import ConventionScan, scan_conventions
from spec_implement_state import (
    FeatureContext,
    ShepherdError,
    StateStore,
    load_config,
    resolve_feature,
    slugify,
    utc_now_iso,
)

#: Packages whose absence must surface at init time, not mid-Loop-1 (§6).
_REQUIRED_PACKAGES = ("claude_agent_sdk", "yaml")


def _require_repo_root(cwd: Path, missing_code: ExitCode) -> Path:
    """Resolve the git repo root containing `cwd` or fail with `missing_code`."""

    root = spec_implement_git.repo_root(cwd)
    if root is None:
        raise ShepherdError(
            missing_code, f"{cwd} is not inside a git repository"
        )
    return root


def _refuse_dirty_tree(repo_root: Path) -> None:
    """Refuse to operate on a dirty tree (§16; untracked .shepherd excepted)."""

    if spec_implement_git.is_dirty(repo_root, excepted_paths=(".gitignore",)):
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            "working tree is dirty; commit or stash your changes first "
            "(untracked files under .shepherd/ and .gitignore are excepted)",
        )


def _write_config(config_path: Path, scan: ConventionScan) -> None:
    """Write a default config.yaml prefilled with the convention-scan result."""

    import yaml

    defaults = ShepherdConfig()
    data = {
        "models": dataclasses.asdict(defaults.models),
        "test": {
            "command": scan.test_command or "",
            "paths": list(scan.test_paths),
            "syntax_check": defaults.test.syntax_check,
        },
        "budgets": dataclasses.asdict(defaults.budgets),
    }
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _append_gitignore_entries(repo_root: Path) -> None:
    """Append GITIGNORE_ENTRIES to .gitignore, only the ones not yet present."""

    gitignore = repo_root / ".gitignore"
    existing = (
        gitignore.read_text(encoding="utf-8").splitlines() if gitignore.is_file() else []
    )
    to_add = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if not to_add:
        return
    text = "\n".join(existing + to_add) + "\n"
    gitignore.write_text(text, encoding="utf-8")


def cmd_init(force: bool) -> int:
    """`spec_implement.py init [--force]` — bootstrap .shepherd, explicit and idempotent (§6)."""

    cwd = Path.cwd().resolve()
    root = spec_implement_git.repo_root(cwd)
    if root is None or root != cwd:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"init must be run at a git repository root (cwd: {cwd}, "
            f"detected root: {root})",
        )

    missing = [
        pkg for pkg in _REQUIRED_PACKAGES if importlib.util.find_spec(pkg) is None
    ]
    if missing:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"missing required package(s): {', '.join(missing)}. Install them in "
            "this interpreter (e.g. `pip install claude-agent-sdk pyyaml`); init "
            "checks preconditions so failures surface now, not mid-Loop-1",
        )

    (root / FEATURES_DIR).mkdir(parents=True, exist_ok=True)
    scan = scan_conventions(root)
    config_path = root / CONFIG_FILE
    if config_path.is_file() and not force:
        print("already initialized; config preserved (use --force to regenerate)")
    else:
        _write_config(config_path, scan)
        print(f"wrote {CONFIG_FILE}")
    _append_gitignore_entries(root)

    print("")
    print("Detected test conventions — REVIEW THIS before running `spec_implement.py run`;")
    print(f"detection is a best guess, and {CONFIG_FILE} test.command / test.paths")
    print("feed the Loop 2/3 enforcement hooks:")
    print(f"  test command: {scan.test_command or '(none detected)'}")
    print(f"  test paths:   {scan.test_paths or '(none detected)'}")
    print(f"  framework:    {scan.framework or '(none detected)'}")
    print(f"  exemplar:     {scan.exemplar_test or '(none found)'}")
    for note in scan.notes:
        print(f"  note: {note}")
    return int(ExitCode.DONE)


def _read_task_statement() -> str:
    """Read the full task statement from stdin (`--task-stdin`)."""

    text = sys.stdin.read()
    if not text.strip():
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            "--task-stdin was given but stdin is empty; pipe or heredoc the "
            "task statement",
        )
    return text


def _read_task_file(path: str, root: Path) -> str:
    """Read the full task statement from a file (`--task-file`).

    The stdin alternative for hosts where piping into the subprocess is
    unreliable (notably the Windows+WSL interop boundary, which silently
    delivers empty stdin). The agent writes the statement with the Write tool
    to a scratch path under `.shepherd/` — gitignored and excepted from the
    dirty-tree check — and passes it here. A scratch file under `.shepherd/` is
    unlinked once copied into task.md so nothing stale lingers.
    """

    task_path = Path(path)
    try:
        text = task_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR, f"--task-file {path!r} is not readable: {exc}"
        ) from exc
    if not text.strip():
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"--task-file {path!r} is empty; write the task statement to it first",
        )
    try:
        resolved = task_path.resolve()
        if resolved.is_relative_to((root / SHEPHERD_DIR).resolve()):
            resolved.unlink()
    except OSError:
        pass  # best-effort cleanup; a leftover gitignored scratch file is harmless
    return text


def cmd_new(title: str, task_stdin: bool = False, task_file: Optional[str] = None) -> int:
    """`spec_implement.py new <title...>` — scaffold a feature folder + spec-implement/<slug> branch (§6).

    The slug and branch always derive from the title. task.md — the Loop 1
    agent's only source of requirements — holds the full task statement, taken
    from `--task-file` if given, else stdin when `--task-stdin` is given, else
    the title.
    """

    cwd = Path.cwd()
    root = _require_repo_root(cwd, ExitCode.SHEPHERD_NOT_INITIALIZED)
    load_config(root)  # raises SHEPHERD_NOT_INITIALIZED if init has not run
    _refuse_dirty_tree(root)

    if task_file is not None:
        task_text = _read_task_file(task_file, root)
    elif task_stdin:
        task_text = _read_task_statement()
    else:
        task_text = title

    try:
        slug = slugify(title)
    except ValueError as exc:
        raise ShepherdError(ExitCode.INTERNAL_ERROR, str(exc)) from exc

    feature_dir = root / FEATURES_DIR / slug
    branch = BRANCH_PREFIX + slug
    if feature_dir.exists():
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"feature {slug!r} already exists; choose a different title",
        )
    if spec_implement_git.branch_exists(root, branch):
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"branch {branch!r} already exists; choose a different title",
        )

    base_commit = spec_implement_git.head_sha(root)
    spec_implement_git.create_branch(root, branch)
    (feature_dir / DESIGN_DIR).mkdir(parents=True)
    (feature_dir / REQUIREMENTS_DIR).mkdir(parents=True)
    (feature_dir / REPORTS_DIR).mkdir(parents=True)
    (feature_dir / TASK_FILE).write_text(
        task_text.rstrip("\n") + "\n", encoding="utf-8"
    )

    state = FeatureState(
        slug=slug,
        branch=branch,
        base_commit=base_commit,
        phase=Phase.SKETCHING_DESIGN.value,
        history=[
            HistoryEntry(phase=Phase.SKETCHING_DESIGN.value, timestamp=utc_now_iso())
        ],
    )
    StateStore(feature_dir).save(state)
    print(f"feature: {slug}")
    print(f"branch:  {branch}")
    return int(ExitCode.DONE)


def _import_loop(loop_number: int):
    """Import spec_implement_loop<N> lazily; exit INTERNAL_ERROR if it does not exist yet."""

    try:
        return importlib.import_module(f"spec_implement_loop{loop_number}")
    except ImportError:
        print(f"loop {loop_number} not yet implemented", file=sys.stderr)
        sys.exit(int(ExitCode.INTERNAL_ERROR))


def _get_runner(ctx: FeatureContext, verbose: bool):
    """Obtain the AgentRunner lazily so the SDK is only touched by `run`."""

    try:
        from spec_implement_agent import get_runner
    except ImportError:
        print("agent runner unavailable (spec_implement_agent not yet implemented)", file=sys.stderr)
        sys.exit(int(ExitCode.INTERNAL_ERROR))
    return get_runner(ctx.repo_root, verbose=verbose)


def cmd_run(
    feature: Optional[str],
    force: bool,
    decision: Optional[str],
    feedback: Optional[str],
    verbose: bool = True,
) -> int:
    """`spec_implement.py run` — dispatch the three-loop state machine on the active feature."""

    cwd = Path.cwd()
    root = _require_repo_root(cwd, ExitCode.SHEPHERD_NOT_INITIALIZED)
    if not (root / SHEPHERD_DIR).is_dir():
        raise ShepherdError(
            ExitCode.SHEPHERD_NOT_INITIALIZED,
            f"no {SHEPHERD_DIR} folder found at {root}; run `spec_implement.py init` first",
        )
    ctx = resolve_feature(root, feature, force)
    _refuse_dirty_tree(root)

    phase = Phase(ctx.state.phase)
    if phase is Phase.DONE:
        print(f"feature {ctx.slug!r} is already DONE")
        return int(ExitCode.DONE)
    if phase is Phase.FAILED:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"feature {ctx.slug!r} is in a terminal FAILED state; see its history",
        )

    import spec_implement_bootstrap

    runner = None
    while True:
        # Re-derive the phase each turn: a loop that ADVANCEs leaves the phase
        # at the next loop's entry, so phase — not a counter — drives dispatch.
        phase = Phase(ctx.state.phase)
        if runner is None:
            runner = _get_runner(ctx, verbose)

        # Bootstrap pre-step (between Loop 1 and Loop 2): when the project has
        # no test framework, add one — with a human checkpoint — before any
        # test is written. Intercepts REQUIREMENTS_APPROVED and owns its own
        # phases; on ADVANCE the phase lands at GENERATING_TESTS and the next
        # turn dispatches Loop 2.
        if phase in spec_implement_bootstrap.BOOTSTRAP_PHASES or (
            phase is Phase.REQUIREMENTS_APPROVED and spec_implement_bootstrap.should_bootstrap(ctx)
        ):
            outcome = spec_implement_bootstrap.run_bootstrap(ctx, runner, decision, feedback)
            decision = feedback = None
            if outcome.status is LoopStatus.ADVANCE:
                if outcome.detail:
                    print(outcome.detail)
                continue
            code = (
                outcome.exit_code if outcome.exit_code is not None
                else ExitCode.INTERNAL_ERROR
            )
            if outcome.status is LoopStatus.CHECKPOINT:
                if outcome.detail:
                    print(outcome.detail)
            elif outcome.detail:  # FAILED
                print(outcome.detail, file=sys.stderr)
            return int(code)

        loop_number = RESUMABLE_PHASES[phase]
        module = _import_loop(loop_number)
        if loop_number == 0:
            outcome = module.run_loop0(ctx, runner, decision, feedback)
        elif loop_number == 1:
            outcome = module.run_loop1(ctx, runner, decision, feedback)
        elif loop_number == 2:
            outcome = module.run_loop2(ctx, runner)
        else:
            outcome = module.run_loop3(ctx, runner, decision, feedback)
        decision = feedback = None  # human input is consumed by its target loop

        if outcome.status is LoopStatus.ADVANCE:
            if outcome.detail:
                print(outcome.detail)
            if loop_number == 3:
                return int(ExitCode.DONE)
            continue
        code = outcome.exit_code if outcome.exit_code is not None else ExitCode.INTERNAL_ERROR
        if outcome.status is LoopStatus.CHECKPOINT:
            if outcome.detail:
                print(outcome.detail)
        else:  # FAILED
            if outcome.detail:
                print(outcome.detail, file=sys.stderr)
        return int(code)


def _status_rows(root: Path) -> list[dict[str, Optional[str]]]:
    """One row per feature folder: slug, phase, branch, last history timestamp."""

    rows: list[dict[str, Optional[str]]] = []
    features_dir = root / FEATURES_DIR
    if not features_dir.is_dir():
        return rows
    for entry in sorted(features_dir.iterdir()):
        if not entry.is_dir():
            continue
        slug = entry.name
        try:
            state = StateStore(entry).load()
            rows.append(
                {
                    "slug": slug,
                    "phase": state.phase,
                    "branch": state.branch,
                    "last_updated": state.history[-1].timestamp if state.history else None,
                }
            )
        except ShepherdError:
            rows.append(
                {
                    "slug": slug,
                    "phase": (
                        "UNKNOWN (state.json missing — the .shepherd/ workspace is "
                        "machine-local; resume on the original machine or start "
                        "fresh)"
                    ),
                    "branch": BRANCH_PREFIX + slug,
                    "last_updated": None,
                }
            )
    return rows


def cmd_status(as_json: bool) -> int:
    """`spec_implement.py status [--json]` — phases of all features; no branch requirements."""

    cwd = Path.cwd()
    root = _require_repo_root(cwd, ExitCode.SHEPHERD_NOT_INITIALIZED)
    if not (root / SHEPHERD_DIR).is_dir():
        raise ShepherdError(
            ExitCode.SHEPHERD_NOT_INITIALIZED,
            f"no {SHEPHERD_DIR} folder found at {root}; run `spec_implement.py init` first",
        )
    rows = _status_rows(root)
    if as_json:
        print(json.dumps(rows, indent=2))
        return int(ExitCode.DONE)
    if not rows:
        print("no features yet — create one with `spec_implement.py new <title>`")
        return int(ExitCode.DONE)
    for row in rows:
        updated = row["last_updated"] or "-"
        print(
            f"{row['slug']}  phase={row['phase']}  branch={row['branch']}  "
            f"updated={updated}"
        )
    return int(ExitCode.DONE)


def _build_parser() -> argparse.ArgumentParser:
    """The argparse tree, exactly per the pinned CLI grammar (docs/contracts.md)."""

    parser = argparse.ArgumentParser(
        prog="spec_implement.py", description="spec-implement shepherd — phased three-loop orchestrator"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="bootstrap .shepherd (explicit, never silent)")
    p_init.add_argument(
        "--force", action="store_true", help="overwrite an existing config.yaml"
    )

    p_new = sub.add_parser("new", help="scaffold a feature folder + spec-implement/<slug> branch")
    p_new.add_argument(
        "title", nargs="+", help="feature title (names the slug and branch)"
    )
    p_new.add_argument(
        "--task-stdin",
        action="store_true",
        help=(
            "read the full task statement for task.md from stdin "
            "(pipe or heredoc); without it, the title is the task statement"
        ),
    )
    p_new.add_argument(
        "--task-file",
        help=(
            "read the full task statement for task.md from this file instead "
            "of stdin (use when piping is unreliable, e.g. Windows+WSL); a "
            "scratch file under .shepherd/ is unlinked after it is read. Wins "
            "over --task-stdin if both are given"
        ),
    )

    p_run = sub.add_parser("run", help="run the three-loop state machine")
    p_run.add_argument("--feature", help="explicit feature slug (always wins, §7)")
    p_run.add_argument(
        "--force", action="store_true", help="override BRANCH_MISMATCH only"
    )
    p_run.add_argument(
        "--decision",
        choices=[DECISION_APPROVE, DECISION_REJECT],
        help="human decision after a checkpoint exit (10/12)",
    )
    p_run.add_argument("--feedback", help="human corrections/rationale text")
    p_run.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "stream the agent's prose and tool activity to stderr (on by "
            "default; use --no-verbose for a silent headless run)"
        ),
    )

    p_status = sub.add_parser("status", help="phases of all features")
    p_status.add_argument(
        "--json", dest="as_json", action="store_true", help="emit a JSON array"
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    """Parse args, dispatch, and translate every outcome into an exit code."""

    args = _build_parser().parse_args(argv)
    try:
        if args.command == "init":
            code = cmd_init(args.force)
        elif args.command == "new":
            code = cmd_new(" ".join(args.title), args.task_stdin, args.task_file)
        elif args.command == "run":
            code = cmd_run(
                args.feature, args.force, args.decision, args.feedback, args.verbose
            )
        else:  # status
            code = cmd_status(args.as_json)
    except ShepherdError as exc:
        print(exc.message, file=sys.stderr)
        sys.exit(int(exc.exit_code))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(int(ExitCode.INTERNAL_ERROR))
    sys.exit(code)


if __name__ == "__main__":
    main()
