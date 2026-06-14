# Shepherd — skills & commands for Claude Code

A Claude Code plugin that packages engineering workflows as skills and
commands. Where a workflow needs hard boundaries, they are **hooks, not
prompts**: PreToolUse path policies make violations impossible — not merely
discouraged. The plugin currently ships one skill, **spec-implement**, with more
skills and commands to follow.

Pinned contracts (exit codes, schemas, CLI): [docs/contracts.md](docs/contracts.md).

## Install into a project

Add this repo as a marketplace and install the plugin from Claude Code:

```
/plugin marketplace add /path/to/shepherd
/plugin install shepherd
```

On its first run in a project the spec-implement skill bootstraps the `.shepherd/`
workspace itself (`spec_implement.py init`, explicit and idempotent). **Review `.shepherd/config.yaml` afterwards** — especially
`test.command` and `test.paths`, which feed the enforcement hooks.

Requires: git, Python ≥ 3.10, and the Claude Code CLI authenticated. The
runtime deps (`claude-agent-sdk` + `pyyaml`) are installed by the skill on
first run when missing — sudo-free, into the ambient `python3`'s per-user
site-packages.

## Skills

### spec-implement — `/shepherd:spec-implement`

Drives a **strict, spec-driven, test-first** build loop for one feature at a
time (red-green), through four sequential loops:

0. **Design sketch** — an agent explores your repo read-only and drafts a
   rough design (components, responsibilities, optional flowcharts); a human
   approves it before any requirement is written.
1. **Specification** — an agent turns the approved design into EARS
   requirements (`WHEN …, THE SYSTEM SHALL …`); a human approves them.
2. **Test generation** — unit tests are written against the design's named
   classes/functions (covering every requirement) in your existing framework,
   verified requirement-by-requirement via a traceability matrix, then
   committed *red* before any implementation exists.
3. **Implementation** — an agent edits main code only until the suite is
   green; test edits are mechanically denied and must go through an
   auditable escalation channel.

Full requirements: [docs/spec-implement-skill-requirements.md](docs/spec-implement-skill-requirements.md).

In Claude Code, in an installed project:

```
/shepherd:spec-implement Add rate limiting to the login endpoint
```

The command drives the engine and pauses at human checkpoints (design
approval, requirements approval, escalations, coverage gaps). The engine is
also usable directly:

```
spec_implement.py init                  # bootstrap .shepherd (explicit, never silent)
spec_implement.py new "Add user auth"   # feature folder + spec-implement/<slug> branch
spec_implement.py run [--feature slug]  # the three-loop state machine
spec_implement.py status                # phases of all features
```

`run` communicates through exit codes (0 done, 10 awaiting approval,
11 coverage gap, 12 escalated, 13 budget exceeded, 14 needs input,
20–22 resolution errors); human decisions return via
`run --decision approve|reject [--feedback …]`.

What the spec-implement skill lands in your repo:

- `.shepherd/` — config, one folder per feature (task statement, approved
  EARS spec files, traceability matrix + reports, session state). The whole
  workspace is gitignored: everything in it is machine-local.
- Automated commits per feature: `spec-implement(<slug>): red` →
  [`red(n)` after approved escalations] → `green`. The red commit is the
  recovery anchor and the proof the tests failed before the implementation
  existed.

Plugin enablement itself is handled by the marketplace install (Claude Code's
plugin config), not written into your repo by the skill.

## Development

```bash
uv sync                                # install the dev/test toolchain from pyproject.toml
uv run pytest                          # 200+ tests, no API calls (fake SDK seam)
claude --plugin-dir /path/to/shepherd  # load the plugin surface in a scratch project
```

Plugin surface: `.claude-plugin/plugin.json`, plus one folder per capability
under `skills/` and `commands/`. Each skill is self-contained: the spec-implement engine
lives in `skills/spec-implement/scripts/` (`spec_implement_contracts.py` pins every shared contract;
`spec_implement.py` is the CLI; loops in `spec_implement_loop{1,2,3}.py`; the SDK seam is the
`AgentRunner` protocol — tests script `FakeAgentRunner` while production uses
`SdkAgentRunner`), with its skill definition in `skills/spec-implement/SKILL.md`, the
exit-code playbook in `skills/spec-implement/references/playbook.md`, and a thin
entry-point command in `commands/spec-implement.md` that invokes the skill. Verified SDK
behavior notes (shared
by any skill built on the Agent SDK): [docs/sdk-notes.md](docs/sdk-notes.md).
