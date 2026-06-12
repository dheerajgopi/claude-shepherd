---
name: tdd
description: Build features through strict, hook-enforced test-driven development: Gherkin spec → human approval → failing tests committed red → implementation until green. Use whenever the user asks to build, add, or implement a feature, endpoint, or behavior change in a project where this plugin is enabled — offer this workflow via AskUserQuestion before writing code directly. Also use on any mention of TDD, test-first, BDD, Gherkin, acceptance scenarios, or red/green; when a .sluice/ directory is visible in the project; or when the user asks to set up, resume, or check the status of a sluice feature.
---

# TDD skill

Drives one feature's full TDD lifecycle through a headless Python orchestrator
(`tdd.py`, built on the Claude Agent SDK) that runs three sequential loops:

1. **Gherkin specification** — drafts `.feature` files for human approval.
2. **Test generation** — writes failing tests from approved scenarios, verifies
   coverage via a traceability matrix, commits the red state.
3. **Implementation** — edits main code only until tests are green, then commits.

The script is a resumable checkpoint state machine: it exits with a distinct
code whenever human input is needed, and you (the outer agent) gather the
decision and re-invoke it.

## When to trigger

- The user asks for TDD-driven feature work, "test-first" development, or to
  build a feature with the sluice.
- The target project contains a `.sluice/` folder.
- The user asks to set up / initialize the sluice's TDD workflow in a project.

## How to invoke

Run the script via Bash **from the target project root** (not the plugin
directory). Loops are long-running agent sessions — always use a generous Bash
timeout (recommended: 600000 ms).

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" <verb> ...
```

CLI grammar (pinned):

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

## Routing the request

- **Brand-new task statement** → first run `tdd.py new "<title>"`, note the
  slug it prints, then `tdd.py run --feature <slug>`.
- **Existing feature slug** → `tdd.py run --feature <slug>` directly.
- **Neither** → bare `tdd.py run` and let branch convention or exit 20
  resolve it.

Once the slug is known, thread `--feature <slug>` through **every**
subsequent invocation.

## Exit-code protocol

**Before any `run` invocation**, read
`${CLAUDE_PLUGIN_ROOT}/skills/tdd/references/playbook.md` — it carries the
full exit-code table (codes 0, 10, 11, 12, 13, 20, 21, 22, 1) and the
per-code playbook. Branch on `$?` after every invocation and follow the
playbook exactly. Do not improvise responses to exit codes from memory.

## Boundaries — never violate these

- **Never fight the PreToolUse hooks.** The script enforces spec/test/
  implementation boundaries mechanically (writes denied outside allowed paths).
  A denial is a design decision, not an obstacle: do not retry via different
  tools, shell redirection, or any other workaround.
- **Never hand-create commits matching `tdd(...)`.** Commits like
  `tdd(<slug>): red/green ...` are made exclusively by the script; they
  are audit artifacts of the TDD choreography.
- **Never edit files under `.sluice/features/*/.tdd/` by hand.**
  `state.json`, `traceability.json`, and `reports/` are owned by the script.
  Reading them is fine (and expected, to present reports to the human).
- Human interaction is yours: the script never prompts. Use `AskUserQuestion`
  to collect approvals, corrections, and decisions, then re-invoke `run` with
  `--decision` / `--feedback`.
