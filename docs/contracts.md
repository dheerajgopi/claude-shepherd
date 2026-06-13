# TDD skill contracts — the shared vocabulary

Contracts for the shepherd plugin's TDD skill (other skills will pin their own).
Source of truth: `skills/tdd/scripts/tdd_contracts.py` (stdlib-only, import it — never re-derive).
This doc is the prose companion for artifacts that can't import Python (SKILL.md, the playbook
in `skills/tdd/references/playbook.md`, setup.sh, prompts). Requirement references (§n):
`docs/tdd-skill-requirements.md`.

## Exit codes (§13)

| Code | Name | Outer command's action |
|------|------|------------------------|
| 0 | DONE | Report success |
| 10 | AWAITING_APPROVAL | AskUserQuestion: approve or give corrections |
| 11 | COVERAGE_GAP | Surface gap report (`.tdd/reports/`) to human |
| 12 | ESCALATED | AskUserQuestion: approve (→ Loop 1 amend) or reject |
| 13 | BUDGET_EXCEEDED | Surface status report |
| 14 | NEEDS_INPUT | AskUserQuestion: answer the implementer's question, re-invoke with `--feedback` |
| 15 | AWAITING_DESIGN_APPROVAL | AskUserQuestion: approve the design sketch or give corrections |
| 20 | NO_FEATURE_RESOLVED | Present feature list, re-invoke with `--feature` |
| 21 | BRANCH_MISMATCH | Warn human; re-invoke with `--force` only if intended |
| 22 | SHEPHERD_NOT_INITIALIZED | Offer `tdd.py init`, then review generated config |
| 1 | INTERNAL_ERROR | Unexpected failure; surface stderr verbatim |

## CLI grammar (§6) — pinned

```
tdd.py init [--force]
tdd.py new <title...> [--task-stdin]
tdd.py run [--feature SLUG] [--force] [--decision approve|reject] [--feedback TEXT]
           [--verbose | --no-verbose]
tdd.py status [--json]
```

- `--task-stdin` on `new` reads the **full task statement** for `task.md`
  from stdin (pipe or heredoc — no temp files, so concurrent agents cannot
  collide); the title still names the slug and branch. Without it, the title
  is the task statement. `task.md` is write-once at `new` time: it is read
  into Loop 0's first session turn (and Loop 1's) and never re-read, so editing
  it after `new` has no effect on the run.
- The human-input channel is `--decision` / `--feedback` on `run`:
  - exit 15 → re-invoke with `--decision approve` **or** `--feedback "<corrections>"` (design)
  - exit 10 → re-invoke with `--decision approve` **or** `--feedback "<corrections>"`
  - exit 12 → re-invoke with `--decision approve` or `--decision reject [--feedback "<why>"]`
  - exit 14 → re-invoke with `--feedback "<answer>"` — the answer to the
    implementer's `request_human_input` question (Loop 3 blocker channel)
- `--force` overrides BRANCH_MISMATCH (21) only; never anything else.
- `--verbose` (default **on**) streams the agent's prose and tool activity to
  stderr so a human can watch the headless run, the way Claude Code does;
  `--no-verbose` silences it. Display-only — it never changes stdout, the exit
  code, or token cost (that output is generated either way).
- All informational output on stdout; errors on stderr; the exit code is the protocol.
- Invocation from a target project root: `python3 <plugin>/skills/tdd/scripts/tdd.py …`
  (the command resolves the plugin path via `${CLAUDE_PLUGIN_ROOT}`).

## Phases (§14)

`SKETCHING_DESIGN → AWAITING_DESIGN_APPROVAL ⇄ (corrections) → DESIGN_APPROVED →
DRAFTING_REQUIREMENTS → AWAITING_APPROVAL ⇄ (corrections) → REQUIREMENTS_APPROVED →
GENERATING_TESTS ⇄ VERIFYING_COVERAGE → RED_COMMITTED → IMPLEMENTING → (ESCALATED →
AMENDING_REQUIREMENTS → RED_COMMITTED) → GREEN → DONE`, `FAILED` reachable from anywhere,
terminal. Legal transitions: `PHASE_TRANSITIONS` in the contracts module; the state store
refuses anything else. Loop ownership (`RESUMABLE_PHASES`): Loop 0 owns the SKETCHING_DESIGN /
AWAITING_DESIGN_APPROVAL design phases; DESIGN_APPROVED is Loop 1's entry from Loop 0 (Loop 1
transitions it into DRAFTING_REQUIREMENTS, mirroring how Loop 2 enters from REQUIREMENTS_APPROVED).

## On-disk artifacts (§5)

The entire `.shepherd/` workspace is **gitignored** (init appends `.shepherd/` to
`.gitignore`): every artifact below is machine-local and never committed.

| Path (per feature) | Schema (contracts module) |
|---|---|
| `.shepherd/config.yaml` | `ShepherdConfig` |
| `.shepherd/manifest.json` | `ShepherdManifest` |
| `features/<slug>/task.md` | verbatim text |
| `features/<slug>/design/*.md` | design sketch files (Loop 0) |
| `features/<slug>/requirements/*.md` | EARS spec files |
| `features/<slug>/.tdd/state.json` | `FeatureState` |
| `features/<slug>/.tdd/traceability.json` | `TraceabilityMatrix` |
| `features/<slug>/.tdd/reports/*` | markdown + json |

Branch convention: `tdd/<slug>` (`BRANCH_PREFIX`). No ACTIVE pointer file, ever (§7).

## EARS spec format (§8) — pinned

Spec files are markdown: a title, a 2–4 line `Rationale:` block for the reviewer, then one
`## REQ-<nnn>: <title>` heading per requirement. Each requirement is exactly ONE EARS
statement — `THE SYSTEM SHALL …` (ubiquitous), `WHEN …, THE SYSTEM SHALL …` (event),
`WHILE …, THE SYSTEM SHALL …` (state), `WHERE …, THE SYSTEM SHALL …` (optional feature),
`IF …, THEN THE SYSTEM SHALL …` (unwanted behavior), or a complex combination — optionally
followed by a markdown examples table whose every row must be covered by a test.
`requirement_id` is `<spec-file-stem>:REQ-<nnn>` (e.g. `login:REQ-003`); ids are sequential,
zero-padded, and never renumbered or reused. Tests are tagged `# requirement: <requirement_id>`.
Declarative only: no function names, endpoints, classes, tables, or UI widgets.

## Commit messages (§16) — format strings, exact

```
tdd(<slug>): red — failing tests
tdd(<slug>): red(<n>) — amended requirements
tdd(<slug>): green — implementation
```

The outer agent must NEVER hand-create commits matching `tdd(...)`.
There is no spec commit: Loop 1 approval only advances the phase, because
the spec artifacts live in the gitignored `.shepherd/` workspace. Red and
red(n) commits carry only `test.paths` content.

## Path policy (the mechanical boundary, §9–10)

- Loops 0–2: `ALLOW_ONLY` — Write/Edit/MultiEdit/NotebookEdit permitted only under the listed
  paths (Loop 0: the feature's `design/`; Loop 1: the feature's `requirements/`; Loop 2:
  `test.paths` from config).
- Loop 3: `DENY_UNDER` — same tools denied under `test.paths` + the `requirements/` folder.
- Enforced by a PreToolUse hook (verified deny shape in `docs/sdk-notes.md` §2); the pure decision
  function `is_path_allowed(tool_name, tool_input, policy)` lives in `tdd_hooks.py` and is the
  unit-tested core. Auto-applied minor test edits are direct Python file writes by the
  orchestrator — never an agent turn — so the "agent can never edit tests" invariant holds.

## The SDK seam

`AgentRunner.run(RunSpec) -> RunResult` (contracts module). Production adapter `SdkAgentRunner`
in `tdd_agent.py`; tests use `FakeAgentRunner`. Subprocess-level tests select the fake via the
`TDD_RUNNER` env var (`fake:<script.json>`). Loops never import the SDK directly.

## Verifier output schemas

Coverage matrix: `VERIFIER_MATRIX_JSON_SCHEMA`; escalation triage: `VERIFIER_TRIAGE_JSON_SCHEMA`
(verdicts: `minor | significant | unsure`; unsure escalates, §10). Both embedded verbatim in the
verifier prompts; Loop 2/3 parse with bounded retry.

## Settings & isolation invariants (from the SDK spike)

- Inner sessions: `permission_mode="bypassPermissions"`, `setting_sources` unset (no CLAUDE.md /
  project settings leak), `cwd` = target repo root, per-loop `model`, `resume=<session_id>` for
  every iteration after the first.
- Native caps set per session: `max_turns` from `budgets.max_turns_per_loop`; cumulative
  cost/wall-clock tracked in `state.json` by the orchestrator.
