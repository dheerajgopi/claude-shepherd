---
name: tdd
description: Build features through strict, hook-enforced test-driven development: EARS requirements spec → human approval → failing tests committed red → implementation until green. Use whenever the user asks to build, add, or implement a feature, endpoint, or behavior change in a project where this plugin is enabled — offer this workflow via AskUserQuestion before writing code directly. Also use on any mention of TDD, test-first, EARS, requirements spec, acceptance criteria, or red/green; when a .shepherd/ directory is visible in the project; or when the user asks to set up, resume, or check the status of a shepherd feature.
---

# TDD skill

Drives one feature's full TDD lifecycle through a headless Python orchestrator
(`tdd.py`, built on the Claude Agent SDK) that runs three sequential loops:

1. **EARS specification** — drafts requirements spec files for human approval.
2. **Test generation** — writes failing tests from approved requirements, verifies
   coverage via a traceability matrix, commits the red state.
3. **Implementation** — edits main code only until tests are green, then commits.

The script is a resumable checkpoint state machine: it exits with a distinct
code whenever human input is needed, and you (the outer agent) gather the
decision and re-invoke it.

## When to trigger

- The user asks for TDD-driven feature work, "test-first" development, or to
  build a feature with the shepherd.
- The target project contains a `.shepherd/` folder.
- The user asks to set up / initialize the shepherd's TDD workflow in a project.

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
tdd.py new <title...> [--task-stdin]
tdd.py run [--feature SLUG] [--force] [--decision approve|reject] [--feedback TEXT]
tdd.py status [--json]
```

- The human-input channel is `--decision` / `--feedback` on `run`:
  - exit 10 → re-invoke with `--decision approve` **or** `--feedback "<corrections>"`
  - exit 12 → re-invoke with `--decision approve` or `--decision reject [--feedback "<why>"]`
  - exit 14 → re-invoke with `--feedback "<answer>"` (the implementer's question)
- `--force` overrides BRANCH_MISMATCH (21) only; never anything else.
- All informational output on stdout; errors on stderr; the exit code is the protocol.

## Routing the request

- **Brand-new task statement** → pipe the **full requirements** via a heredoc:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" new "<short title>" --task-stdin <<'EOF'
  <full task statement: requirements, defaults, edge cases, acceptance criteria>
  EOF
  ```

  Note the slug it prints, then `tdd.py run --feature <slug>`. The task
  statement is the spec agent's ONLY source of requirements — it never
  sees this conversation. Carry over every concrete detail the user stated:
  defaults (page sizes, limits, timeouts), edge cases, error behaviors,
  acceptance criteria. Do not condense to a title; a detail dropped here is
  invisible to every later loop. Never write the statement to a file (temp
  files collide across concurrent agents) — always the heredoc.
- **Existing feature slug** → `tdd.py run --feature <slug>` directly.
- **Neither** → bare `tdd.py run` and let branch convention or exit 20
  resolve it.

Once the slug is known, thread `--feature <slug>` through **every**
subsequent invocation.

## Exit-code protocol

**Before any `run` invocation**, read
`${CLAUDE_PLUGIN_ROOT}/skills/tdd/references/playbook.md` — it carries the
full exit-code table (codes 0, 10, 11, 12, 13, 14, 20, 21, 22, 1) and the
per-code playbook. Branch on `$?` after every invocation and follow the
playbook exactly. Do not improvise responses to exit codes from memory.

On a fresh (e.g. marketplace) install the first invocation may fail with
`missing required package(s)` — nothing has installed the engine's Python deps
yet. The playbook's **Dependency bootstrap** covers this: install
`claude-agent-sdk` + `pyyaml` without sudo, then retry. Don't surface it as a
fatal error.

## Boundaries — never violate these

- **Never fight the PreToolUse hooks.** The script enforces spec/test/
  implementation boundaries mechanically (writes denied outside allowed paths).
  A denial is a design decision, not an obstacle: do not retry via different
  tools, shell redirection, or any other workaround.
- **Never hand-create commits matching `tdd(...)`.** Commits like
  `tdd(<slug>): red/green ...` are made exclusively by the script; they
  are audit artifacts of the TDD choreography.
- **Never edit files under `.shepherd/features/*/.tdd/` by hand.**
  `state.json`, `traceability.json`, and `reports/` are owned by the script.
  Reading them is fine (and expected, to present reports to the human).
- Human interaction is yours: the script never prompts. Use `AskUserQuestion`
  to collect approvals, corrections, and decisions, then re-invoke `run` with
  `--decision` / `--feedback`.
