"""Shared contracts for the TDD sluice engine.

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
    AWAITING_APPROVAL = 10        # Gherkin drafted/revised, needs human review
    COVERAGE_GAP = 11             # scenarios uncoverable after max iterations
    ESCALATED = 12                # significant test change proposed
    BUDGET_EXCEEDED = 13          # turn/cost/time limit hit
    NO_FEATURE_RESOLVED = 20      # no --feature arg, no tdd/<slug> branch
    BRANCH_MISMATCH = 21          # current branch != branch recorded in state
    SLUICE_NOT_INITIALIZED = 22  # no .sluice folder found

    # Conventional failure for unexpected errors (not part of the §13 table;
    # the outer command treats any other nonzero code as a hard error).
    INTERNAL_ERROR = 1


# ---------------------------------------------------------------------------
# Phases (§14)
# ---------------------------------------------------------------------------


class Phase(str, enum.Enum):
    DRAFTING_GHERKIN = "DRAFTING_GHERKIN"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    GHERKIN_APPROVED = "GHERKIN_APPROVED"
    GENERATING_TESTS = "GENERATING_TESTS"
    VERIFYING_COVERAGE = "VERIFYING_COVERAGE"
    RED_COMMITTED = "RED_COMMITTED"
    IMPLEMENTING = "IMPLEMENTING"
    ESCALATED = "ESCALATED"
    AMENDING_GHERKIN = "AMENDING_GHERKIN"
    GREEN = "GREEN"
    DONE = "DONE"
    # FAILED_<reason> terminals are stored as FAILED with a reason field in
    # state.json history entries, keeping the enum closed.
    FAILED = "FAILED"


#: Legal phase transitions. The state store must refuse any transition not
#: listed here (FAILED is reachable from anywhere and is terminal).
PHASE_TRANSITIONS: dict[Phase, tuple[Phase, ...]] = {
    Phase.DRAFTING_GHERKIN: (Phase.AWAITING_APPROVAL,),
    Phase.AWAITING_APPROVAL: (Phase.DRAFTING_GHERKIN, Phase.GHERKIN_APPROVED),
    Phase.GHERKIN_APPROVED: (Phase.GENERATING_TESTS,),
    Phase.GENERATING_TESTS: (Phase.VERIFYING_COVERAGE,),
    Phase.VERIFYING_COVERAGE: (Phase.GENERATING_TESTS, Phase.RED_COMMITTED),
    Phase.RED_COMMITTED: (Phase.IMPLEMENTING,),
    Phase.IMPLEMENTING: (Phase.ESCALATED, Phase.GREEN),
    Phase.ESCALATED: (Phase.AMENDING_GHERKIN, Phase.IMPLEMENTING),
    Phase.AMENDING_GHERKIN: (Phase.RED_COMMITTED,),
    Phase.GREEN: (Phase.DONE,),
    Phase.DONE: (),
    Phase.FAILED: (),
}

#: Phases at which `run` may be (re-)entered after a crash or checkpoint exit.
#: The dispatcher maps each to the loop that owns it.
RESUMABLE_PHASES: dict[Phase, int] = {
    Phase.DRAFTING_GHERKIN: 1,
    Phase.AWAITING_APPROVAL: 1,
    Phase.GHERKIN_APPROVED: 2,
    Phase.GENERATING_TESTS: 2,
    Phase.VERIFYING_COVERAGE: 2,
    Phase.RED_COMMITTED: 3,
    Phase.IMPLEMENTING: 3,
    Phase.ESCALATED: 3,
    Phase.AMENDING_GHERKIN: 3,
    Phase.GREEN: 3,
}


# ---------------------------------------------------------------------------
# Commit messages (§16)
# ---------------------------------------------------------------------------

COMMIT_RED = "tdd({slug}): red — failing tests"
COMMIT_RED_AMENDED = "tdd({slug}): red({n}) — amended scenarios"
COMMIT_GREEN = "tdd({slug}): green — implementation"


# ---------------------------------------------------------------------------
# Workspace layout (§5)
# ---------------------------------------------------------------------------

SLUICE_DIR = ".sluice"
CONFIG_FILE = ".sluice/config.yaml"
FEATURES_DIR = ".sluice/features"
MANIFEST_FILE = ".sluice/manifest.json"
# Per feature (relative to .sluice/features/<slug>/). The entire .sluice/
# workspace is gitignored (§5): every artifact below is machine-local.
TASK_FILE = "task.md"
GHERKIN_DIR = "gherkin"
TDD_DIR = ".tdd"
STATE_FILE = ".tdd/state.json"
TRACE_FILE = ".tdd/traceability.json"
REPORTS_DIR = ".tdd/reports"

BRANCH_PREFIX = "tdd/"  # feature branch = tdd/<slug>

#: .gitignore entries appended by init (§5 version-control policy):
#: the whole workspace is machine-local, nothing under it is ever committed.
GITIGNORE_ENTRIES = [
    ".sluice/",
]


# ---------------------------------------------------------------------------
# CLI grammar (§6) — pinned so the command (T1-PKG) and engine (T1-CORE) agree
# ---------------------------------------------------------------------------
#
#   tdd.py init [--force]
#   tdd.py new <title...>
#   tdd.py run [--feature SLUG] [--force]
#              [--decision approve|reject] [--feedback TEXT]
#   tdd.py status [--json]
#
# `--decision/--feedback` is the human-input channel: the outer command
# re-invokes `run` with the human's answer after a checkpoint exit (10/12).
# `--feedback` alone (no --decision) means "corrections" for the exit-10
# revision cycle. `--force` on run overrides BRANCH_MISMATCH (§7).

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"

#: Env var that swaps the SDK runner for a fake in tests (subprocess-level).
RUNNER_ENV_VAR = "TDD_RUNNER"  # value: "fake:<path-to-script-json>" | unset = real


# ---------------------------------------------------------------------------
# config.yaml schema (§11)
# ---------------------------------------------------------------------------


@dataclass
class ModelsConfig:
    gherkin: str = "claude-opus-4-8"
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
class SluiceConfig:
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
    turns_loop1: int = 0
    turns_loop2: int = 0
    turns_loop3: int = 0
    started_at: Optional[str] = None  # ISO-8601; wall-clock anchor


@dataclass
class FeatureState:
    slug: str
    branch: str                      # tdd/<slug>
    base_commit: str                 # SHA at feature creation
    phase: str                       # current Phase value
    session_ids: dict[str, Optional[str]] = field(
        default_factory=lambda: {"loop1": None, "loop2": None, "loop3": None}
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
class ScenarioTrace:
    scenario_id: str                 # "<feature-file-stem>:<scenario name>"
    feature_file: str                # path relative to gherkin/
    revision: int                    # bumped on each approved amendment (§10)
    tests: list[str] = field(default_factory=list)  # "path::test_function"
    status: str = COVERAGE_MISSING   # one of COVERAGE_STATUSES
    notes: Optional[str] = None


@dataclass
class TraceRevision:
    """Audit log entry: any applied test change or scenario amendment."""

    timestamp: str                   # ISO-8601 UTC
    kind: str                        # "auto_applied_minor" | "escalation_approved" | "resync"
    scenario_ids: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TraceabilityMatrix:
    slug: str
    scenarios: list[ScenarioTrace] = field(default_factory=list)
    revisions: list[TraceRevision] = field(default_factory=list)
    schema_version: int = 1


#: JSON Schema for the verifier model's REQUIRED output (embedded verbatim in
#: verifier_coverage_prompt.md; parsed with bounded retry in Loop 2).
VERIFIER_MATRIX_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scenarios"],
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["scenario_id", "feature_file", "tests", "status"],
                "properties": {
                    "scenario_id": {"type": "string"},
                    "feature_file": {"type": "string"},
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
class SluiceManifest:
    sluice_sha: str                          # git SHA of the sluice repo
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
#   tdd_loop1.run_loop1(ctx, runner, decision: str | None, feedback: str | None) -> LoopOutcome
#   tdd_loop1.amend_scenarios(ctx, runner, proposal: dict) -> list[str]   # amended scenario_ids
#   tdd_loop2.run_loop2(ctx, runner) -> LoopOutcome
#   tdd_loop2.resync_tests(ctx, runner, scenario_ids: list[str]) -> LoopOutcome
#   tdd_loop3.run_loop3(ctx, runner, decision: str | None, feedback: str | None) -> LoopOutcome
#
# `ctx` is tdd_state.FeatureContext: repo_root, slug, paths, SluiceConfig,
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
