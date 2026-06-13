"""Shared contracts for the TDD shepherd engine.

This module is the single source of truth for everything two modules could
disagree on: exit codes, phases and their legal transitions, on-disk schemas
(state.json, traceability.json, config.yaml, manifest.json), CLI grammar,
commit message formats, the AgentRunner protocol (the SDK seam), and the
loop interfaces.

Stdlib-only. Importable by the engine, the loops, the tests, and quotable by
docs. Requirement references (§n) point at docs/tdd-skill-requirements.md.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

# ---------------------------------------------------------------------------
# Exit codes (§13)
# ---------------------------------------------------------------------------


class ExitCode(enum.IntEnum):
    DONE = 0                      # all tests green, traceability intact
    AWAITING_APPROVAL = 10        # requirements drafted/revised, needs human review
    COVERAGE_GAP = 11             # requirements uncoverable after max iterations
    ESCALATED = 12                # significant test change proposed
    BUDGET_EXCEEDED = 13          # turn/cost/time limit hit
    NEEDS_INPUT = 14              # implementer is blocked, asked the human a question
    AWAITING_DESIGN_APPROVAL = 15  # design sketch drafted/revised, needs human review
    AWAITING_FRAMEWORK_APPROVAL = 16  # test-framework bootstrap proposed, needs human review
    NO_FEATURE_RESOLVED = 20      # no --feature arg, no tdd/<slug> branch
    BRANCH_MISMATCH = 21          # current branch != branch recorded in state
    SHEPHERD_NOT_INITIALIZED = 22  # no .shepherd folder found

    # Conventional failure for unexpected errors (not part of the §13 table;
    # the outer command treats any other nonzero code as a hard error).
    INTERNAL_ERROR = 1


# ---------------------------------------------------------------------------
# Phases (§14)
# ---------------------------------------------------------------------------


class Phase(str, enum.Enum):
    SKETCHING_DESIGN = "SKETCHING_DESIGN"
    AWAITING_DESIGN_APPROVAL = "AWAITING_DESIGN_APPROVAL"
    DESIGN_APPROVED = "DESIGN_APPROVED"
    DRAFTING_REQUIREMENTS = "DRAFTING_REQUIREMENTS"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REQUIREMENTS_APPROVED = "REQUIREMENTS_APPROVED"
    # Test-framework bootstrap (between Loop 1 and Loop 2): only entered when
    # the project has no test framework/library; adds one, with a human
    # checkpoint, before any test is written.
    PROPOSING_FRAMEWORK = "PROPOSING_FRAMEWORK"
    AWAITING_FRAMEWORK_APPROVAL = "AWAITING_FRAMEWORK_APPROVAL"
    INSTALLING_FRAMEWORK = "INSTALLING_FRAMEWORK"
    GENERATING_TESTS = "GENERATING_TESTS"
    VERIFYING_COVERAGE = "VERIFYING_COVERAGE"
    RED_COMMITTED = "RED_COMMITTED"
    IMPLEMENTING = "IMPLEMENTING"
    ESCALATED = "ESCALATED"
    BLOCKED = "BLOCKED"
    AMENDING_REQUIREMENTS = "AMENDING_REQUIREMENTS"
    GREEN = "GREEN"
    DONE = "DONE"
    # FAILED_<reason> terminals are stored as FAILED with a reason field in
    # state.json history entries, keeping the enum closed.
    FAILED = "FAILED"


#: Legal phase transitions. The state store must refuse any transition not
#: listed here (FAILED is reachable from anywhere and is terminal).
PHASE_TRANSITIONS: dict[Phase, tuple[Phase, ...]] = {
    Phase.SKETCHING_DESIGN: (Phase.AWAITING_DESIGN_APPROVAL,),
    Phase.AWAITING_DESIGN_APPROVAL: (Phase.SKETCHING_DESIGN, Phase.DESIGN_APPROVED),
    Phase.DESIGN_APPROVED: (Phase.DRAFTING_REQUIREMENTS,),
    Phase.DRAFTING_REQUIREMENTS: (Phase.AWAITING_APPROVAL,),
    Phase.AWAITING_APPROVAL: (Phase.DRAFTING_REQUIREMENTS, Phase.REQUIREMENTS_APPROVED),
    # REQUIREMENTS_APPROVED forks: straight to test generation when a framework
    # is present, or through the bootstrap pre-step when one must be added.
    Phase.REQUIREMENTS_APPROVED: (Phase.GENERATING_TESTS, Phase.PROPOSING_FRAMEWORK),
    Phase.PROPOSING_FRAMEWORK: (Phase.AWAITING_FRAMEWORK_APPROVAL,),
    Phase.AWAITING_FRAMEWORK_APPROVAL: (
        Phase.PROPOSING_FRAMEWORK,    # corrections cycle (revise the proposal)
        Phase.INSTALLING_FRAMEWORK,   # approval
    ),
    Phase.INSTALLING_FRAMEWORK: (Phase.GENERATING_TESTS,),
    Phase.GENERATING_TESTS: (Phase.VERIFYING_COVERAGE,),
    Phase.VERIFYING_COVERAGE: (Phase.GENERATING_TESTS, Phase.RED_COMMITTED),
    Phase.RED_COMMITTED: (Phase.IMPLEMENTING,),
    Phase.IMPLEMENTING: (Phase.ESCALATED, Phase.BLOCKED, Phase.GREEN),
    Phase.ESCALATED: (Phase.AMENDING_REQUIREMENTS, Phase.IMPLEMENTING),
    Phase.BLOCKED: (Phase.IMPLEMENTING,),
    Phase.AMENDING_REQUIREMENTS: (Phase.RED_COMMITTED,),
    Phase.GREEN: (Phase.DONE,),
    Phase.DONE: (),
    Phase.FAILED: (),
}

#: Phases at which `run` may be (re-)entered after a crash or checkpoint exit.
#: The dispatcher maps each to the loop that owns it.
RESUMABLE_PHASES: dict[Phase, int] = {
    Phase.SKETCHING_DESIGN: 0,
    Phase.AWAITING_DESIGN_APPROVAL: 0,
    Phase.DESIGN_APPROVED: 1,
    Phase.DRAFTING_REQUIREMENTS: 1,
    Phase.AWAITING_APPROVAL: 1,
    Phase.REQUIREMENTS_APPROVED: 2,
    # Bootstrap phases sit at the front of Loop 2's territory; the dispatcher
    # intercepts them before the loop-number dispatch (tdd_bootstrap owns them).
    Phase.PROPOSING_FRAMEWORK: 2,
    Phase.AWAITING_FRAMEWORK_APPROVAL: 2,
    Phase.INSTALLING_FRAMEWORK: 2,
    Phase.GENERATING_TESTS: 2,
    Phase.VERIFYING_COVERAGE: 2,
    Phase.RED_COMMITTED: 3,
    Phase.IMPLEMENTING: 3,
    Phase.ESCALATED: 3,
    Phase.BLOCKED: 3,
    Phase.AMENDING_REQUIREMENTS: 3,
    Phase.GREEN: 3,
}


# ---------------------------------------------------------------------------
# Commit messages (§16)
# ---------------------------------------------------------------------------

COMMIT_RED = "tdd({slug}): red — failing tests"
COMMIT_RED_AMENDED = "tdd({slug}): red({n}) — amended requirements"
COMMIT_GREEN = "tdd({slug}): green — implementation"
#: Bootstrap commit (test-framework pre-step): carries dependency-manifest and
#: lockfile changes only — never test.paths content (tests come later).
COMMIT_BOOTSTRAP = "tdd({slug}): chore — add {framework}"


# ---------------------------------------------------------------------------
# Workspace layout (§5)
# ---------------------------------------------------------------------------

SHEPHERD_DIR = ".shepherd"
CONFIG_FILE = ".shepherd/config.yaml"
FEATURES_DIR = ".shepherd/features"
MANIFEST_FILE = ".shepherd/manifest.json"
# Per feature (relative to .shepherd/features/<slug>/). The entire .shepherd/
# workspace is gitignored (§5): every artifact below is machine-local.
TASK_FILE = "task.md"
#: Design sketch files are markdown (classes, functions, responsibilities,
#: optional mermaid flowcharts) drafted in Loop 0 for human approval before any
#: EARS requirement is written. Free-form prose — no pinned heading grammar.
DESIGN_DIR = "design"
DESIGN_FILE_GLOB = "*.md"
REQUIREMENTS_DIR = "requirements"
#: EARS spec files are markdown; one `## REQ-<nnn>: <title>` heading per
#: requirement, each holding exactly one EARS statement (WHEN/WHILE/WHERE/
#: IF…THEN/ubiquitous) plus an optional examples table. requirement_id is
#: "<spec-file-stem>:REQ-<nnn>"; ids are never renumbered or reused.
SPEC_FILE_GLOB = "*.md"
REQUIREMENT_HEADING_PATTERN = r"^##\s+(REQ-\d+)\b.*$"
TDD_DIR = ".tdd"
STATE_FILE = ".tdd/state.json"
TRACE_FILE = ".tdd/traceability.json"
REPORTS_DIR = ".tdd/reports"

BRANCH_PREFIX = "tdd/"  # feature branch = tdd/<slug>

#: .gitignore entries appended by init (§5 version-control policy):
#: the whole workspace is machine-local, nothing under it is ever committed.
GITIGNORE_ENTRIES = [
    ".shepherd/",
]


# ---------------------------------------------------------------------------
# CLI grammar (§6) — pinned so the command (T1-PKG) and engine (T1-CORE) agree
# ---------------------------------------------------------------------------
#
#   tdd.py init [--force]
#   tdd.py new <title...> [--task-stdin | --task-file PATH]
#   tdd.py run [--feature SLUG] [--force]
#              [--decision approve|reject] [--feedback TEXT]
#              [--verbose | --no-verbose]
#   tdd.py status [--json]
#
# `--decision/--feedback` is the human-input channel: the outer command
# re-invokes `run` with the human's answer after a checkpoint exit (10/12/14).
# `--feedback` alone (no --decision) means "corrections" for the exit-10
# revision cycle and "the answer" for the exit-14 blocker cycle (Loop 3's
# request_human_input). `--force` on run overrides BRANCH_MISMATCH (§7).
# `--verbose` (default on) streams the agent's prose and tool activity to
# stderr so a human can watch the headless run; `--no-verbose` silences it.
# It is display-only — stdout and the exit code remain the machine protocol.
# `--task-stdin` on new reads the full task statement for task.md from
# stdin (pipe/heredoc — no temp files, so concurrent agents cannot
# collide); the title still names the slug/branch. `--task-file PATH` is
# the alternative for hosts where piping into the subprocess is unreliable
# (notably the Windows+WSL interop boundary, which silently delivers empty
# stdin): the statement is read from PATH, and a PATH under .shepherd/ is
# unlinked once copied into task.md. --task-file wins if both are given.
# Without either, the title is the task statement. task.md is write-once at
# `new` time — it is read into Loop 1's first session turn and never re-read.

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"

#: Env var that swaps the SDK runner for a fake in tests (subprocess-level).
RUNNER_ENV_VAR = "TDD_RUNNER"  # value: "fake:<path-to-script-json>" | unset = real


# ---------------------------------------------------------------------------
# config.yaml schema (§11)
# ---------------------------------------------------------------------------


@dataclass
class ModelsConfig:
    design: str = "claude-opus-4-8"
    requirements: str = "claude-opus-4-8"
    testgen: str = "claude-sonnet-4-6"
    verifier: str = "claude-haiku-4-5"
    implement: str = "claude-sonnet-4-6"


@dataclass
class TestConfig:
    command: str = ""              # e.g. "pytest -x -q"; detected by init scan
    paths: list[str] = field(default_factory=list)  # feeds allow/deny hooks
    syntax_check: bool = False     # optional Loop 2 syntax-only check (§9)


@dataclass
class BudgetsConfig:
    max_turns_per_loop: int = 40
    max_coverage_iterations: int = 5
    max_cost_usd: float = 10.0
    max_wall_clock_minutes: int = 120


@dataclass
class ShepherdConfig:
    models: ModelsConfig = field(default_factory=ModelsConfig)
    test: TestConfig = field(default_factory=TestConfig)
    budgets: BudgetsConfig = field(default_factory=BudgetsConfig)


# ---------------------------------------------------------------------------
# state.json schema (§5, §14) — machine-local, gitignored
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    phase: str           # Phase value, or "FAILED"
    timestamp: str       # ISO-8601 UTC
    session_id: Optional[str] = None
    reason: Optional[str] = None  # set for FAILED / ESCALATED entries


@dataclass
class BudgetsSpent:
    cost_usd: float = 0.0
    turns_loop0: int = 0
    turns_loop1: int = 0
    turns_loop2: int = 0
    turns_loop3: int = 0
    turns_bootstrap: int = 0  # test-framework bootstrap pre-step
    started_at: Optional[str] = None  # ISO-8601; wall-clock anchor


@dataclass
class FeatureState:
    slug: str
    branch: str                      # tdd/<slug>
    base_commit: str                 # SHA at feature creation
    phase: str                       # current Phase value
    session_ids: dict[str, Optional[str]] = field(
        default_factory=lambda: {
            "loop0": None,
            "loop1": None,
            "loop2": None,
            "loop3": None,
        }
    )
    history: list[HistoryEntry] = field(default_factory=list)
    budgets_spent: BudgetsSpent = field(default_factory=BudgetsSpent)
    red_commit_count: int = 0        # n for COMMIT_RED_AMENDED
    overrides: dict[str, Any] = field(default_factory=dict)  # per-feature config overrides
    schema_version: int = 1


# ---------------------------------------------------------------------------
# traceability.json schema (§9, §10) — machine-local audit artifact
# ---------------------------------------------------------------------------

COVERAGE_COVERED = "covered"
COVERAGE_PARTIAL = "partial"
COVERAGE_MISSING = "missing"
COVERAGE_STATUSES = (COVERAGE_COVERED, COVERAGE_PARTIAL, COVERAGE_MISSING)


@dataclass
class RequirementTrace:
    requirement_id: str              # "<spec-file-stem>:REQ-<nnn>"
    spec_file: str                   # path relative to requirements/
    revision: int                    # bumped on each approved amendment (§10)
    tests: list[str] = field(default_factory=list)  # "path::test_function"
    status: str = COVERAGE_MISSING   # one of COVERAGE_STATUSES
    notes: Optional[str] = None


@dataclass
class TraceRevision:
    """Audit log entry: any applied test change or requirement amendment."""

    timestamp: str                   # ISO-8601 UTC
    kind: str                        # "auto_applied_minor" | "escalation_approved" | "resync"
    requirement_ids: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TraceabilityMatrix:
    slug: str
    requirements: list[RequirementTrace] = field(default_factory=list)
    revisions: list[TraceRevision] = field(default_factory=list)
    schema_version: int = 1


#: JSON Schema for the verifier model's REQUIRED output (embedded verbatim in
#: verifier_coverage_prompt.md; parsed with bounded retry in Loop 2).
VERIFIER_MATRIX_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["requirements"],
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["requirement_id", "spec_file", "tests", "status"],
                "properties": {
                    "requirement_id": {"type": "string"},
                    "spec_file": {"type": "string"},
                    "tests": {"type": "array", "items": {"type": "string"}},
                    "status": {"enum": list(COVERAGE_STATUSES)},
                    "notes": {"type": "string"},
                },
            },
        }
    },
}

#: JSON Schema for the verifier's escalation-triage verdict (§10).
VERIFIER_TRIAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "rationale"],
    "properties": {
        "verdict": {"enum": ["minor", "significant", "unsure"]},
        "rationale": {"type": "string"},
    },
}


# ---------------------------------------------------------------------------
# manifest.json schema (§4)
# ---------------------------------------------------------------------------


@dataclass
class ShepherdManifest:
    shepherd_sha: str                          # git SHA of the shepherd repo
    installed_at: str                         # ISO-8601 UTC
    artifacts: dict[str, str] = field(default_factory=dict)  # path -> sha256
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Path policy (the mechanical boundary, §9/§10)
# ---------------------------------------------------------------------------


class PathPolicyMode(str, enum.Enum):
    ALLOW_ONLY = "allow_only"   # writes permitted ONLY under listed paths (Loops 1-2)
    DENY_UNDER = "deny_under"   # writes denied under listed paths (Loop 3)


#: Built-in tools whose path argument the policy hook must inspect.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


# ---------------------------------------------------------------------------
# AgentRunner protocol — the SDK seam (everything below the loops fakes this)
# ---------------------------------------------------------------------------


@dataclass
class ToolEvent:
    """A tool invocation observed during a run (custom tools + denials)."""

    tool_name: str
    tool_input: dict[str, Any]
    denied: bool = False
    deny_reason: Optional[str] = None


@dataclass
class RunResult:
    session_id: str
    text: str                        # final assistant text (ResultMessage.result)
    tool_events: list[ToolEvent] = field(default_factory=list)
    cost_usd: float = 0.0
    num_turns: int = 0
    is_error: bool = False
    error: Optional[str] = None


@dataclass
class RunSpec:
    """One agent turn-batch: start a session or resume one with a new prompt."""

    prompt: str
    model: str
    system_prompt: str
    session_id: Optional[str] = None          # None = new session; else resume
    allowed_tools: list[str] = field(default_factory=list)
    path_policy_mode: Optional[PathPolicyMode] = None
    path_policy_paths: list[str] = field(default_factory=list)
    expose_propose_test_change: bool = False  # Loop 3 only
    expose_request_human_input: bool = False  # Loop 3 only (blocker channel)
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    cwd: Optional[str] = None                 # repo root


class AgentRunner(Protocol):
    """The single seam between the orchestrator and the Claude Agent SDK.

    Production: SdkAgentRunner (tdd_agent.py). Tests: FakeAgentRunner
    (tests/fakes.py), scripted per test, with file side-effects gated by the
    REAL path-policy decision function from tdd_hooks.py.
    """

    def run(self, spec: RunSpec) -> RunResult:  # blocking; wraps anyio internally
        ...


# ---------------------------------------------------------------------------
# Loop interfaces (so Loop 3's escalation re-entry can be written before
# Loops 1-2 exist; §10)
# ---------------------------------------------------------------------------


class LoopStatus(str, enum.Enum):
    CHECKPOINT = "checkpoint"    # exit with .exit_code, await human input
    ADVANCE = "advance"          # phase complete, dispatcher moves to next loop
    FAILED = "failed"


@dataclass
class LoopOutcome:
    status: LoopStatus
    exit_code: Optional[ExitCode] = None   # set when status == CHECKPOINT/FAILED
    detail: str = ""                       # human-readable summary for stdout


# Signatures pinned for the loop modules (implemented in Wave 2):
#
#   tdd_loop0.run_loop0(ctx, runner, decision: str | None, feedback: str | None) -> LoopOutcome
#   tdd_loop1.run_loop1(ctx, runner, decision: str | None, feedback: str | None) -> LoopOutcome
#   tdd_loop1.amend_requirements(ctx, runner, proposal: dict) -> list[str]  # amended requirement_ids
#   tdd_loop2.run_loop2(ctx, runner) -> LoopOutcome
#   tdd_loop2.resync_tests(ctx, runner, requirement_ids: list[str]) -> LoopOutcome
#   tdd_loop3.run_loop3(ctx, runner, decision: str | None, feedback: str | None) -> LoopOutcome
#
# `ctx` is tdd_state.FeatureContext: repo_root, slug, paths, ShepherdConfig,
# FeatureState store handle, and git helpers — defined in T1-CORE and kept
# minimal; loops receive everything through it.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asdict_state(obj: Any) -> dict[str, Any]:
    """Dataclass -> JSON-ready dict (single canonical serializer)."""

    return dataclasses.asdict(obj)


def validate_transition(current: Phase, new: Phase) -> bool:
    """True if `current -> new` is a legal phase transition (FAILED always legal)."""

    if new is Phase.FAILED:
        return True
    return new in PHASE_TRANSITIONS.get(current, ())
