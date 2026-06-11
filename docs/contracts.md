# TDD skill contracts — the shared vocabulary

Contracts for the harness plugin's TDD skill (other skills will pin their own).
Source of truth: `skills/tdd/scripts/tdd_contracts.py` (stdlib-only, import it — never re-derive).
This doc is the prose companion for artifacts that can't import Python (SKILL.md, the command,
setup.sh, prompts). Requirement references (§n): `docs/tdd-skill-requirements.md`.

## Exit codes (§13)

| Code | Name | Outer command's action |
|------|------|------------------------|
| 0 | DONE | Report success |
| 10 | AWAITING_APPROVAL | AskUserQuestion: approve or give corrections |
| 11 | COVERAGE_GAP | Surface gap report (`.tdd/reports/`) to human |
| 12 | ESCALATED | AskUserQuestion: approve (→ Loop 1 amend) or reject |
| 13 | BUDGET_EXCEEDED | Surface status report |
| 20 | NO_FEATURE_RESOLVED | Present feature list, re-invoke with `--feature` |
| 21 | BRANCH_MISMATCH | Warn human; re-invoke with `--force` only if intended |
| 22 | HARNESS_NOT_INITIALIZED | Offer `tdd.py init`, then review generated config |
| 1 | INTERNAL_ERROR | Unexpected failure; surface stderr verbatim |

## CLI grammar (§6) — pinned

```
tdd.py init [--force]
tdd.py new <title...>
tdd.py run [--feature SLUG] [--force] [--decision approve|reject] [--feedback TEXT]
tdd.py status [--json]
```

- The human-input channel is `--decision` / `--feedback` on `run`:
  - exit 10 → re-invoke with `--decision approve` **or** `--feedback "<corrections>"`
  - exit 12 → re-invoke with `--decision approve` or `--decision reject [--feedback "<why>"]`
- `--force` overrides BRANCH_MISMATCH (21) only; never anything else.
- All informational output on stdout; errors on stderr; the exit code is the protocol.
- Invocation from a target project root: `python3 <plugin>/skills/tdd/scripts/tdd.py …`
  (the command resolves the plugin path via `${CLAUDE_PLUGIN_ROOT}`).

## Phases (§14)

`DRAFTING_GHERKIN → AWAITING_APPROVAL ⇄ (corrections) → GHERKIN_APPROVED → GENERATING_TESTS ⇄
VERIFYING_COVERAGE → RED_COMMITTED → IMPLEMENTING → (ESCALATED → AMENDING_GHERKIN → RED_COMMITTED)
→ GREEN → DONE`, `FAILED` reachable from anywhere, terminal. Legal transitions:
`PHASE_TRANSITIONS` in the contracts module; the state store refuses anything else.

## On-disk artifacts (§5)

| Path (per feature) | Schema (contracts module) | Git |
|---|---|---|
| `.harness/config.yaml` | `HarnessConfig` | committed |
| `.harness/manifest.json` | `HarnessManifest` | committed |
| `features/<slug>/task.md` | verbatim text | committed |
| `features/<slug>/gherkin/*.feature` | Gherkin | committed |
| `features/<slug>/.tdd/state.json` | `FeatureState` | **gitignored** |
| `features/<slug>/.tdd/traceability.json` | `TraceabilityMatrix` | committed |
| `features/<slug>/.tdd/reports/*` | markdown + json | committed |

Branch convention: `tdd/<slug>` (`BRANCH_PREFIX`). No ACTIVE pointer file, ever (§7).

## Commit messages (§16) — format strings, exact

```
tdd(<slug>): spec — gherkin scenarios
tdd(<slug>): red — failing tests
tdd(<slug>): red(<n>) — amended scenarios
tdd(<slug>): green — implementation
```

The outer agent must NEVER hand-create commits matching `tdd(...)`.

## Path policy (the mechanical boundary, §9–10)

- Loops 1–2: `ALLOW_ONLY` — Write/Edit/MultiEdit/NotebookEdit permitted only under the listed
  paths (Loop 1: the feature's `gherkin/`; Loop 2: `test.paths` from config).
- Loop 3: `DENY_UNDER` — same tools denied under `test.paths` + the `gherkin/` folder.
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
