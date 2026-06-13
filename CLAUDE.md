# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code **plugin** packaging engineering workflows as skills and commands. It is not TDD-specific — strict TDD is the first skill; more skills/commands will be added. Each skill is self-contained under `skills/<name>/` (instructions in SKILL.md + `references/`); the matching command in `commands/` is a thin entry point that invokes the skill.

## Commands

```bash
# one-time dev setup
uv venv .venv && uv pip install --python .venv/bin/python claude-agent-sdk pytest pyyaml

.venv/bin/pytest                          # full suite (no API calls — fake SDK seam)
.venv/bin/pytest tests/test_loop3.py      # one file
.venv/bin/pytest tests/test_loop3.py -k escalation   # one test by keyword

claude --plugin-dir /path/to/shepherd      # load the plugin surface in a scratch project
bin/setup.sh                              # install into a target project (run from ITS root)
```

The TDD engine CLI (run from a target project root): `python3 skills/tdd/scripts/tdd.py init|new|run|status`.

## Architecture

**The contracts module is the single source of truth.** `skills/tdd/scripts/tdd_contracts.py` (stdlib-only) pins everything two modules could disagree on: exit codes, phases + legal transitions (`PHASE_TRANSITIONS` — the state store refuses anything else), on-disk schemas, CLI grammar, commit formats, and the `AgentRunner` protocol. Import it; never re-derive these values. `docs/contracts.md` is its prose companion for artifacts that can't import Python (SKILL.md, the playbook, setup.sh, prompts) — keep the two in sync.

**Checkpoint state machine.** `tdd.py run` executes headlessly via the Claude Agent SDK and exits with a distinct code whenever human input is needed (0 done, 10 awaiting requirements approval, 11 coverage gap, 12 escalated, 13 budget exceeded, 14 needs input, 15 awaiting design approval, 16 awaiting test-framework approval, 20–22 resolution errors). The outer agent — driven by the TDD skill (`skills/tdd/SKILL.md`, with the per-code playbook in `skills/tdd/references/playbook.md`; `commands/tdd.md` is a thin alias that invokes the skill) — interprets `$?`, gathers the human decision with AskUserQuestion, and re-invokes with `--decision approve|reject [--feedback …]`. The engine resumes the same SDK session (`resume=<session_id>`) to preserve the prompt-cache prefix. The exit code is the protocol; informational output goes to stdout, errors to stderr.

**Four loops** (`tdd_loop0.py` → `tdd_loop3.py`): design sketch → EARS requirements spec → test generation (traceability matrix, red commit) → implementation (green commit). Loop 0 drafts a human-approved design (free-form markdown in `design/`) that Loops 1 and 2 both consume: Loop 1 derives the EARS requirements (the "what") from it, and Loop 2 writes unit tests against its named classes/functions (the "how"), each test still tagged `# requirement: <id>` so the requirement→test traceability matrix holds.

**Test-framework bootstrap** (`tdd_bootstrap.py`, the pre-step between Loop 1 and Loop 2): only entered when the project has no test framework/library. `propose_framework` deterministically picks the idiomatic framework from the project's own markers (pytest/jest/vitest/JUnit; Go and Rust have stdlib harnesses and never bootstrap; an unrecognized language has no recipe and is skipped). It checkpoints for human approval (exit 16), then an ALLOW_ONLY (manifest-scoped) + Bash agent declares and installs the dependency, the engine commits `tdd(<slug>): chore — add <framework>` and records `test.command`/`test.paths` in config, and the flow advances to test generation. The dispatcher (`cmd_run`) re-derives the phase each turn and intercepts the bootstrap phases (`PROPOSING_FRAMEWORK`/`AWAITING_FRAMEWORK_APPROVAL`/`INSTALLING_FRAMEWORK`) before the loop-number dispatch. Test generation otherwise **follows the existing conventions** when a framework is already present (scan + exemplar, never introducing a new framework). `DESIGN_APPROVED` is Loop 1's entry phase from Loop 0 (Loop 1 transitions it into `DRAFTING_REQUIREMENTS`, mirroring how Loop 2 enters from `REQUIREMENTS_APPROVED`). Loop boundaries are **hooks, not prompts**: a PreToolUse path policy (pure decision function `is_path_allowed` in `tdd_hooks.py`) makes cross-boundary edits impossible — Loops 0–2 are ALLOW_ONLY (design/ resp. requirements/ resp. `test.paths`), Loop 3 is DENY_UNDER the test/requirements paths. Test edits in Loop 3 go through an escalation channel; approved escalations re-enter Loop 1 (requirements amend) + Loop 2 (resync) and produce `red(n)` commits — escalations never touch the design.

**The SDK seam.** Loops never import the SDK directly — they depend on the `AgentRunner` protocol. Production uses `SdkAgentRunner` (`tdd_agent.py`); tests script `FakeAgentRunner` (`tdd_fake_runner.py`), selected in subprocess tests via the `TDD_RUNNER=fake:<script.json>` env var. The fake routes simulated file writes through the real path-policy function, so it exercises the same mechanical boundary as production. Verified SDK behavior (resume, hooks deny shape, settings isolation, budgets) is documented in `docs/sdk-notes.md` — those notes were live-verified; trust them over intuition.

**Automated commits** follow exact format strings: `tdd(<slug>): chore — add <framework> | red | red(<n>) | green` (see contracts module). There is no spec commit — `.shepherd/` is gitignored, so Loop 1 approval only advances the phase. The bootstrap `chore` commit is the only `tdd(...)` commit that touches source-controlled files outside `test.paths` (dependency manifest + lockfiles, never tests). Nothing outside the engine may create commits matching `tdd(...)`.

**Target-project footprint:** a `.shepherd/` workspace (config.yaml, manifest.json, one folder per feature), gitignored in its entirety — every artifact in it is machine-local — and one `enabledPlugins` entry in `.claude/settings.json`. Branch convention: `tdd/<slug>`.

## Test-suite conventions

- Fixtures (`tests/conftest.py`) build their worlds with plain subprocess git + file writes — they must never call the engine's `init`/`new` (fixtures cannot depend on the code under test). `test_cli_exitcodes.py` is deliberately the only place the engine's verbs run as subprocesses; `test_e2e.py` drives full journeys through the CLI with fake-runner scripts.
- `skills/tdd/scripts/` is put on `sys.path` by conftest; engine modules are imported flat (`import tdd_contracts`), not as a package.

## Commit messages

Conventional prefixes (`feat`, `fix`, `test`, `docs`, `chore`, `build`). Describe what was implemented; no process details (test counts, parallel-track logistics, session/agent chatter).
