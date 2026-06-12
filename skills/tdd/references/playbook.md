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
| 20 | NO_FEATURE_RESOLVED | Present feature list, re-invoke with `--feature` |
| 21 | BRANCH_MISMATCH | Warn human; re-invoke with `--force` only if intended |
| 22 | SLUICE_NOT_INITIALIZED | Offer `tdd.py init`, then review generated config |
| 1 | INTERNAL_ERROR | Unexpected failure; surface stderr verbatim |

## Per-code playbook

### 0 — DONE
All tests green, traceability intact. Report success to the user, including
the script's stdout summary. Stop.

### 10 — AWAITING_APPROVAL (Gherkin drafted/revised)
1. Read the drafted `.feature` files from `.sluice/features/<slug>/gherkin/`.
2. Present them to the user via `AskUserQuestion` with options:
   **Approve** / **Give corrections**.
3. On approve: re-invoke `run --feature <slug> --decision approve`.
4. On corrections: collect the user's feedback text, then re-invoke
   `run --feature <slug> --feedback "<text>"`. This cycle repeats until
   approval.

### 11 — COVERAGE_GAP
Read the gap report from `.sluice/features/<slug>/.tdd/reports/` and surface
its content to the user. Stop — this needs human judgment, not a retry.

### 12 — ESCALATED (significant test change proposed)
1. Read the proposal report from `.sluice/features/<slug>/.tdd/reports/`.
2. Present it via `AskUserQuestion` with options: **Approve** / **Reject**.
3. On approve: re-invoke `run --feature <slug> --decision approve`
   (the script amends the affected scenarios via Loop 1 and re-syncs tests).
4. On reject: ask why, then re-invoke
   `run --feature <slug> --decision reject --feedback "<why>"`.

### 13 — BUDGET_EXCEEDED
Surface the status report (stdout and any report file under
`.sluice/features/<slug>/.tdd/reports/`) to the user. Stop.

### 20 — NO_FEATURE_RESOLVED
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" status`.
2. Present the features and their phases via `AskUserQuestion`.
3. Re-invoke `run --feature <slug>` with the chosen slug.

### 21 — BRANCH_MISMATCH
Warn the user that the current branch differs from the one recorded in the
feature's state. Only re-invoke with `--force` if the user explicitly confirms
they intend to proceed on this branch. Otherwise stop (or help them switch to
the recorded `tdd/<slug>` branch).

### 22 — SLUICE_NOT_INITIALIZED
1. Offer (via `AskUserQuestion`) to run
   `python3 "${CLAUDE_PLUGIN_ROOT}/skills/tdd/scripts/tdd.py" init`.
2. After init, show the user the generated `.sluice/config.yaml` for review —
   **especially `test.command` and `test.paths`**, which feed the Loop 2/3
   enforcement hooks; a wrong boundary undermines the safety model.
3. Once the config is confirmed, resume the original `run` invocation.

### 1 — INTERNAL_ERROR (or any other nonzero code)
Show stderr verbatim to the user. Stop. Do not retry blindly.

## Run mechanics

- Thread `--feature <slug>` through every re-invocation once known.
- Always use long Bash timeouts (600000 ms) for `run`.
- `--force` is for exit 21 only.
- The hard boundaries (hooks, `tdd(...)` commits, `.tdd/` files) are in
  SKILL.md's **Boundaries** section — they apply to every step here.
