# TDD run playbook — exit-code protocol

Read this in full before any `tdd.py run` invocation. `tdd.py` is a headless,
resumable TDD state machine: it exits with a distinct code whenever human
input is needed; you interpret the code, gather the human's decision with
`AskUserQuestion`, and re-invoke. The exit code is the protocol — branch on
`$?` after every invocation.

## Invocation

Run via Bash **from the target project root**, with a long timeout
(600000 ms — loops are long-running agent sessions):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" run [--feature <slug>] [--decision approve|reject] [--feedback "<text>"] [--force]
echo "exit: $?"
```

Once the slug is known, thread `--feature <slug>` through **every**
re-invocation.

## Exit-code contract

| Code | Name | Outer agent's action |
|------|------|----------------------|
| 0 | DONE | Report success |
| 10 | AWAITING_APPROVAL | AskUserQuestion: approve or give corrections |
| 11 | COVERAGE_GAP | Surface gap report (`.tdd/reports/`) to human |
| 12 | ESCALATED | AskUserQuestion: approve (→ Loop 1 amend) or reject |
| 13 | BUDGET_EXCEEDED | Surface status report |
| 14 | NEEDS_INPUT | AskUserQuestion: answer the implementer's question, re-invoke with `--feedback` |
| 15 | AWAITING_DESIGN_APPROVAL | AskUserQuestion: approve the design sketch or give corrections |
| 16 | AWAITING_FRAMEWORK_APPROVAL | AskUserQuestion: approve adding the proposed test framework or give corrections |
| 20 | NO_FEATURE_RESOLVED | Present feature list, re-invoke with `--feature` |
| 21 | BRANCH_MISMATCH | Warn human; re-invoke with `--force` only if intended |
| 22 | SHEPHERD_NOT_INITIALIZED | Offer `tdd.py init`, then review generated config |
| 1 | INTERNAL_ERROR | Unexpected failure; surface stderr verbatim |

## Per-code playbook

### 0 — DONE
All tests green, traceability intact. Report success to the user, including
the script's stdout summary. Stop.

### 15 — AWAITING_DESIGN_APPROVAL (design sketch drafted/revised)
This is the **first** checkpoint of a fresh feature — Loop 0 drafts a rough
design before any requirement is written.
1. Read the drafted design (`.md`) files from
   `.shepherd/features/<slug>/design/`.
2. Present them to the user via `AskUserQuestion` with options:
   **Approve** / **Give corrections**.
3. On approve: re-invoke `run --feature <slug> --decision approve`. The same
   invocation advances into Loop 1, which drafts the EARS requirements and then
   checkpoints again at exit 10.
4. On corrections: collect the user's feedback text, then re-invoke
   `run --feature <slug> --feedback "<text>"`. This cycle repeats until approval.

### 16 — AWAITING_FRAMEWORK_APPROVAL (test-framework bootstrap proposed)
Only reached when the project has **no test framework** — tests cannot be
generated or run until one is added. This checkpoint sits between requirements
approval and test generation.
1. Read the proposal `framework_proposal.md` from
   `.shepherd/features/<slug>/.tdd/reports/` — it names the framework, the
   manifest file(s) to edit, the install command, and the resulting test
   command and test directory.
2. Present it via `AskUserQuestion` with options: **Approve** / **Give corrections**.
3. On approve: re-invoke `run --feature <slug> --decision approve`. The script
   runs an install agent (scoped to the manifest), commits
   `tdd(<slug>): chore — add <framework>`, records the test command/paths in
   config, and continues into test generation.
4. On corrections: collect the user's feedback (e.g. a different framework),
   then re-invoke `run --feature <slug> --feedback "<text>"`. This re-proposes
   and checkpoints again until approval.

### 10 — AWAITING_APPROVAL (requirements drafted/revised)
1. Read the drafted EARS spec (`.md`) files from
   `.shepherd/features/<slug>/requirements/`.
2. Present them to the user via `AskUserQuestion` with options:
   **Approve** / **Give corrections**.
3. On approve: re-invoke `run --feature <slug> --decision approve`.
4. On corrections: collect the user's feedback text, then re-invoke
   `run --feature <slug> --feedback "<text>"`. This cycle repeats until
   approval.

### 11 — COVERAGE_GAP
Read the gap report from `.shepherd/features/<slug>/.tdd/reports/` and surface
its content to the user. Stop — this needs human judgment, not a retry.

### 12 — ESCALATED (significant test change proposed)
1. Read the proposal report from `.shepherd/features/<slug>/.tdd/reports/`.
2. Present it via `AskUserQuestion` with options: **Approve** / **Reject**.
3. On approve: re-invoke `run --feature <slug> --decision approve`
   (the script amends the affected requirements via Loop 1 and re-syncs tests).
4. On reject: ask why, then re-invoke
   `run --feature <slug> --decision reject --feedback "<why>"`.

### 13 — BUDGET_EXCEEDED
Surface the status report (stdout and any report file under
`.shepherd/features/<slug>/.tdd/reports/`) to the user. Stop.

### 14 — NEEDS_INPUT (implementer is blocked, asked a question)
1. Read the latest blocker report `blocker_<n>.md` from
   `.shepherd/features/<slug>/.tdd/reports/`.
2. Present its **Question** (and **Suggested options** as choices, if present)
   to the user via `AskUserQuestion`. The user may always answer freely.
3. Collect the answer, then re-invoke
   `run --feature <slug> --feedback "<answer>"`. The script resumes the same
   implementer session with the answer in hand and continues toward green.

### 20 — NO_FEATURE_RESOLVED
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" status`.
2. Present the features and their phases via `AskUserQuestion`.
3. Re-invoke `run --feature <slug>` with the chosen slug.

### 21 — BRANCH_MISMATCH
Warn the user that the current branch differs from the one recorded in the
feature's state. Only re-invoke with `--force` if the user explicitly confirms
they intend to proceed on this branch. Otherwise stop (or help them switch to
the recorded `tdd/<slug>` branch).

### 22 — SHEPHERD_NOT_INITIALIZED
1. Offer (via `AskUserQuestion`) to run
   `python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" init`.
   - If init exits nonzero reporting `missing required package(s)`, follow
     **Dependency bootstrap** below, then re-run init.
2. After init, show the user the generated `.shepherd/config.yaml` for review —
   **especially `test.command` and `test.paths`** (the latter is a test-file
   classifier of globs — `tests/`, `**/*_test.go`, `src/test`, `**/*.test.*` —
   not just a directory), which feed the Loop 2/3 enforcement hooks; a wrong
   classifier undermines the safety model.
3. Once the config is confirmed, resume the original `run` invocation.

### 1 — INTERNAL_ERROR (or any other nonzero code)
**First, check stderr for a missing-dependency error** — a message containing
`missing required package(s)` (claude-agent-sdk / pyyaml). If present, follow
**Dependency bootstrap** below and retry the same invocation; this is the
common first-run case on a marketplace install, where nothing has installed the
engine's Python deps yet. For any other error: show stderr verbatim to the
user, stop, and do not retry blindly.

## Dependency bootstrap

The engine needs `claude-agent-sdk` and `pyyaml` importable by the **same
`python3`** that runs `tdd.py`. A marketplace install ships the plugin files
but does NOT install these — so the first `init`/`run` can fail with
`missing required package(s)`. When you see that, install them **without sudo**
into that interpreter's per-user site-packages, then retry the failed command:

```bash
python3 -m pip install --user claude-agent-sdk pyyaml
```

- Use the bare `python3` here (no venv path) — it is the same interpreter the
  engine runs under, so a `--user` install lands exactly where it looks.
- If the install fails or the error persists, the `python3` is likely
  "externally managed" (PEP 668). Surface that and suggest re-running with
  `--break-system-packages`, or installing the deps into a venv that is the
  `python3` on PATH. Do not silently add `--break-system-packages` yourself.
- `bin/setup.sh` does this same install automatically; it is only needed for the
  manual (non-marketplace) install path.

## Run mechanics

- Thread `--feature <slug>` through every re-invocation once known.
- Always use long Bash timeouts (600000 ms) for `run`.
- `--force` is for exit 21 only.
- The hard boundaries (hooks, `tdd(...)` commits, `.tdd/` files) are in
  SKILL.md's **Boundaries** section — they apply to every step here.
