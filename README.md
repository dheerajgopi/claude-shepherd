# Sluice — skills & commands for Claude Code

A Claude Code plugin that packages engineering workflows as skills and
commands. Where a workflow needs hard boundaries, they are **hooks, not
prompts**: PreToolUse path policies make violations impossible — not merely
discouraged. The plugin currently ships one skill, **strict TDD**, with more
skills and commands to follow.

Pinned contracts (exit codes, schemas, CLI): [docs/contracts.md](docs/contracts.md).

## Install into a project

```bash
cd /path/to/your/project
/path/to/sluice/bin/setup.sh
```

Idempotent. It registers the plugin project-scoped in `.claude/settings.json`
(committed, so teammates get it on pull), bootstraps the `.sluice/`
workspace, and records install state in `.sluice/manifest.json`. **Review
`.sluice/config.yaml` afterwards** — especially `test.command` and
`test.paths`, which feed the enforcement hooks.

Requires: git, Python ≥ 3.10, `claude-agent-sdk` + `pyyaml` importable, and
the Claude Code CLI authenticated.

## Skills

### TDD — `/sluice:tdd`

Drives **strict test-driven development** for one feature at a time through
three sequential loops:

1. **Specification** — an agent explores your repo read-only and drafts
   Gherkin scenarios; a human approves them.
2. **Test generation** — approved scenarios become executable tests in your
   existing framework, verified scenario-by-scenario via a traceability
   matrix, then committed *red* before any implementation exists.
3. **Implementation** — an agent edits main code only until the suite is
   green; test edits are mechanically denied and must go through an
   auditable escalation channel.

Full requirements: [docs/tdd-skill-requirements.md](docs/tdd-skill-requirements.md).

In Claude Code, in an installed project:

```
/sluice:tdd Add rate limiting to the login endpoint
```

The command drives the engine and pauses at human checkpoints (scenario
approval, escalations, coverage gaps). The engine is also usable directly:

```
tdd.py init                  # bootstrap .sluice (explicit, never silent)
tdd.py new "Add user auth"   # feature folder + tdd/<slug> branch
tdd.py run [--feature slug]  # the three-loop state machine
tdd.py status                # phases of all features
```

`run` communicates through exit codes (0 done, 10 awaiting approval,
11 coverage gap, 12 escalated, 13 budget exceeded, 20–22 resolution errors);
human decisions return via `run --decision approve|reject [--feedback …]`.

What the TDD skill lands in your repo:

- `.sluice/` — config, one folder per feature (task statement, approved
  `.feature` files, committed traceability matrix + reports; machine-local
  `state.json` is gitignored).
- Automated commits per feature: `tdd(<slug>): spec` → `red` →
  [`red(n)` after approved escalations] → `green`. The red commit is the
  recovery anchor and the proof the tests failed before the implementation
  existed.
- One `enabledPlugins` entry in `.claude/settings.json`. Nothing else.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python claude-agent-sdk pytest pyyaml
.venv/bin/pytest                      # 200+ tests, no API calls (fake SDK seam)
claude --plugin-dir /path/to/sluice  # load the plugin surface in a scratch project
```

Plugin surface: `.claude-plugin/plugin.json`, plus one folder per capability
under `skills/` and `commands/`. Each skill is self-contained: the TDD engine
lives in `skills/tdd/scripts/` (`tdd_contracts.py` pins every shared contract;
`tdd.py` is the CLI; loops in `tdd_loop{1,2,3}.py`; the SDK seam is the
`AgentRunner` protocol — tests script `FakeAgentRunner` while production uses
`SdkAgentRunner`), with its skill definition in `skills/tdd/SKILL.md`, the
exit-code playbook in `skills/tdd/references/playbook.md`, and a thin
entry-point command in `commands/tdd.md` that invokes the skill. Verified SDK
behavior notes (shared
by any skill built on the Agent SDK): [docs/sdk-notes.md](docs/sdk-notes.md).
