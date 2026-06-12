# Claude Agent SDK — verified API notes (T0-SDK-SPIKE)

Verified against `claude-agent-sdk==0.2.96` (Python 3.11, Claude Code CLI 2.1.172) by running live
code on 2026-06-11. Spike scripts: `/tmp/sdk-spike/spike1.py`, `spike1b.py`, `spike2.py`.

## 1. Session resume — VERIFIED live

- `ClaudeAgentOptions(resume="<session_id>")` resumes an existing session **in a new process**.
- The resumed `ResultMessage.session_id` is the **same id** (no fork unless `fork_session=True`).
- Context is preserved (model recalled a codeword from the prior process) and the prefix is served
  from cache (`usage.cache_read_input_tokens` ≈ 15.9k on a trivial resume).
- Session id is obtained from `ResultMessage.session_id` (also on `SystemMessage` init).

## 2. Hooks vs `can_use_tool` — DECISION: hooks

Both exist. **Use PreToolUse hooks** — they receive `tool_name`/`tool_input` the same way, support
matcher filtering, and their deny reason is surfaced to the model as a tool error.

```python
async def path_hook(input_data, tool_use_id, context):
    # input_data: PreToolUseHookInput TypedDict: tool_name, tool_input, session_id, cwd, ...
    if denied:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",        # 'allow' | 'deny' | 'ask' | 'defer'
            "permissionDecisionReason": "...",   # shown to the model
        }}
    return {}  # empty dict = no opinion (allow)

options = ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[path_hook])]})
```

Verified live: allowed path written, forbidden path denied with the reason fed back; the agent did
not retry. `matcher` can be a tool-name regex (e.g. `"Write|Edit"`); `None` matches all.

`can_use_tool: Callable[[tool_name, tool_input, ToolPermissionContext] -> PermissionResultAllow|PermissionResultDeny]`
is the alternative single-callback channel (`PermissionResultDeny(message=..., interrupt=False)`).
We standardize on hooks; do not mix both for the same policy.

## 3. Custom in-process tools — VERIFIED live

```python
@tool("propose_test_change", "Propose a change to a test file", {"test_file": str, "reason": str})
async def propose_test_change(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "Proposal recorded."}]}

server = create_sdk_mcp_server("tdd", tools=[propose_test_change])
options = ClaudeAgentOptions(
    mcp_servers={"tdd": server},
    allowed_tools=["Write", "mcp__tdd__propose_test_change"],  # name = mcp__<server>__<tool>
)
```

Coexists fine with a restricted built-in tool list. `input_schema` accepts a `{param: type}` dict
or a full JSON schema dict.

## 4. Model & system prompt

- `model="claude-haiku-4-5-20251001"` etc. per options (per loop/session for us).
- `system_prompt` accepts a plain string (fully custom), a preset, or a file ref. Plain string used.
- `fallback_model` available.

## 5. Usage / cost — VERIFIED live

`ResultMessage` fields: `total_cost_usd: float`, `num_turns: int`, `usage` dict with
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, ...;
plus `model_usage` (per-model breakdown), `is_error`, `stop_reason`, `result` (final text),
`structured_output`, `permission_denials`.

## 6. Tool restriction

- `allowed_tools: list[str]` — only these are available. `disallowed_tools` subtracts.
- There is **no native path scoping** for Write/Edit — path policy must be a PreToolUse hook
  (our design assumption confirmed).
- `permission_mode="bypassPermissions"` needed for headless writes; hooks still fire and deny.
  This is the key combination for the shepherd: bypass CLI permission prompts, enforce via hooks.

## 7. Settings isolation

`setting_sources: list['user'|'project'|'local'] | None` — **default `None` loads no filesystem
settings** (no CLAUDE.md, no project settings). Leave it unset for full isolation of the inner
agent from the target project's Claude config. `cwd` sets the working directory.

## 8. Prompt caching

Automatic (`cache_control` managed by CLI/SDK). Resuming a session preserves the cached prefix —
verified via `cache_read_input_tokens` on resume. No SDK-level cache knobs needed; our job is
prefix stability + session reuse (requirement §12).

## 9. Structured output

`output_format: dict` option exists (JSON-schema based), with `ResultMessage.structured_output`.
For the verifier we will still embed the schema in the prompt and parse `result`, with
`output_format` as an enhancement to try in Loop 2 — bounded parse-retry remains the fallback.

## 10. Budget / turn controls

- `max_turns: int` — per-session turn cap (native).
- `max_budget_usd: float` — native cost cap per session run. We still track cumulative cost across
  sessions in `state.json` (the requirement's budget is per feature, not per session).
- Wall-clock: not native; enforced by the orchestrator.

## Gotchas

- `query()` is async (`anyio`-friendly); each call yields `UserMessage|AssistantMessage|SystemMessage|ResultMessage|StreamEvent`.
- Errors: `CLINotFoundError`, `CLIConnectionError`, `ProcessError` — preconditions for `init`.
- Requires the Claude Code CLI on PATH and authenticated (uses its auth; no separate API key needed
  when the CLI is logged in).
- Subagent fan-out skips prompt caching (requirement §12) — loops stay flat single-agent sessions;
  do not set `agents`.
- Package: `claude-agent-sdk` (import `claude_agent_sdk`), Python ≥ 3.10.
