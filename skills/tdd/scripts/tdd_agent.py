"""Claude-Agent-SDK adapter — the production AgentRunner (§9, §10, §12).

`SdkAgentRunner` is the only module that talks to claude-agent-sdk; loops go
through the `AgentRunner` protocol (tdd_contracts). All SDK imports live
inside function/method bodies so that `get_runner` with a fake runner
(TDD_RUNNER=fake:<script.json>) works without the SDK installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tdd_contracts import (
    RUNNER_ENV_VAR,
    AgentRunner,
    RunResult,
    RunSpec,
    ToolEvent,
)
from tdd_hooks import PathPolicy, make_pretooluse_hook


def build_prompt(sections: list[tuple[str, str]]) -> str:
    """Assemble named prompt sections, in the given order, as markdown blocks.

    Each (name, content) pair becomes ``## <name>\\n\\n<content>``.

    §12 stability rule: callers MUST order sections stable -> volatile —
    task statement, approved requirements, conventions first; the latest test
    output LAST — and must NOT embed timestamps or other mutable status in
    early sections. The prompt-cache prefix invalidates everything downstream
    of a changed early block, so early sections must be byte-stable across
    iterations of a loop.
    """
    return "\n\n".join(f"## {name}\n\n{content}" for name, content in sections)


def _format_tool_use(name: str, tool_input: dict) -> str:
    """Compact one-line description of a tool call for the verbose trace.

    Shows the argument that matters for the common tools (the file path for
    edits/reads, the command for Bash) and falls back to the tool name alone.
    """
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return f"{name} {target}".rstrip()
    if name == "Bash":
        cmd = " ".join(str(tool_input.get("command", "")).split())
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"$ {cmd}"
    return name


def _emit_assistant(message, out) -> None:
    """Stream one AssistantMessage's prose and tool activity to `out` (stderr).

    This is display only — the engine's protocol is the exit code and stdout;
    the trace lets a human watch the headless agent the way Claude Code does.
    """
    from claude_agent_sdk import TextBlock, ToolUseBlock

    for block in message.content:
        if isinstance(block, TextBlock):
            text = block.text.strip()
            if text:
                print(text, file=out, flush=True)
        elif isinstance(block, ToolUseBlock):
            print(
                f"  → {_format_tool_use(block.name, block.input)}",
                file=out,
                flush=True,
            )


class SdkAgentRunner:
    """Production AgentRunner backed by the Claude Agent SDK.

    One `run()` call = one turn-batch in one session: a new session when
    `spec.session_id` is None, otherwise a resume of that session (preserving
    the cached prefix, §12). Synchronous wrapper; wraps anyio internally.
    """

    def __init__(self, repo_root: Path, verbose: bool = False):
        self.repo_root = repo_root
        self.verbose = verbose

    def run(self, spec: RunSpec) -> RunResult:
        import anyio

        return anyio.run(self._run_async, spec)

    async def _run_async(self, spec: RunSpec) -> RunResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            CLIConnectionError,
            CLINotFoundError,
            HookMatcher,
            ProcessError,
            ResultMessage,
            create_sdk_mcp_server,
            query,
            tool,
        )

        events: list[ToolEvent] = []
        allowed_tools = list(spec.allowed_tools)
        cwd = str(spec.cwd or self.repo_root)

        # Path policy -> PreToolUse hook (the mechanical boundary, §9/§10).
        hooks = None
        if spec.path_policy_mode is not None:
            policy = PathPolicy(
                mode=spec.path_policy_mode,
                paths=list(spec.path_policy_paths),
                repo_root=Path(cwd),
            )
            hooks = {
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[make_pretooluse_hook(policy, events)],
                    )
                ]
            }

        # Custom Loop-3 channels, each gated by its own RunSpec flag. Both
        # live on one `tdd` MCP server; the server is created only if at least
        # one tool is requested.
        mcp_servers = None
        custom_tools = []

        # Escalation channel (§10): the ONLY way to change tests in Loop 3.
        if spec.expose_propose_test_change:

            @tool(
                "propose_test_change",
                "Propose a modification to a test file. This is the ONLY way "
                "to change tests; direct edits are denied.",
                {
                    "test_file": str,
                    "related_requirement": str,
                    "reason": str,
                    "proposed_diff": str,
                },
            )
            async def propose_test_change(args: dict) -> dict:
                events.append(
                    ToolEvent(
                        tool_name="propose_test_change", tool_input=dict(args)
                    )
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Proposal recorded for triage; continue "
                            "with other work or end your turn.",
                        }
                    ]
                }

            custom_tools.append(propose_test_change)
            allowed_tools.append("mcp__tdd__propose_test_change")

        # Blocker channel: pause for a human decision the agent cannot make.
        if spec.expose_request_human_input:

            @tool(
                "request_human_input",
                "Pause and ask the human a question when you are blocked on "
                "information or a decision only a human can make. Use this "
                "instead of guessing; do NOT use it for problems you can "
                "investigate and fix yourself.",
                {
                    "question": str,
                    "context": str,
                    "suggested_options": str,
                },
            )
            async def request_human_input(args: dict) -> dict:
                events.append(
                    ToolEvent(
                        tool_name="request_human_input", tool_input=dict(args)
                    )
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Question recorded; the human will answer "
                            "and the run will resume. End your turn.",
                        }
                    ]
                }

            custom_tools.append(request_human_input)
            allowed_tools.append("mcp__tdd__request_human_input")

        if custom_tools:
            mcp_servers = {
                "tdd": create_sdk_mcp_server("tdd", tools=custom_tools)
            }

        # NOTE: setting_sources deliberately unset (None = full isolation
        # from the target project's CLAUDE.md/settings, verified); `agents`
        # deliberately unset (subagent fan-out skips prompt caching, §12).
        opts = ClaudeAgentOptions(
            model=spec.model,
            system_prompt=spec.system_prompt,
            cwd=cwd,
            permission_mode="bypassPermissions",
            allowed_tools=allowed_tools,
            max_turns=spec.max_turns,
            max_budget_usd=spec.max_budget_usd,
            resume=spec.session_id,  # None = new session
            hooks=hooks,
            mcp_servers=mcp_servers or {},
        )

        try:
            result_msg = None
            async for message in query(prompt=spec.prompt, options=opts):
                if self.verbose and isinstance(message, AssistantMessage):
                    _emit_assistant(message, sys.stderr)
                if isinstance(message, ResultMessage):
                    result_msg = message

            if result_msg is None:
                return RunResult(
                    session_id=spec.session_id or "",
                    text="",
                    tool_events=events,
                    is_error=True,
                    error="no ResultMessage received from SDK",
                )

            errors = result_msg.errors
            return RunResult(
                session_id=result_msg.session_id,
                text=result_msg.result or "",
                tool_events=events,
                cost_usd=result_msg.total_cost_usd or 0.0,
                num_turns=result_msg.num_turns,
                is_error=result_msg.is_error,
                error=", ".join(errors) if errors else None,
            )
        except (CLINotFoundError, CLIConnectionError, ProcessError) as e:
            return RunResult(
                session_id=spec.session_id or "",
                text="",
                tool_events=events,
                is_error=True,
                error=str(e),
            )


def get_runner(repo_root: Path, verbose: bool = False) -> AgentRunner:
    """Select the AgentRunner: real SDK, or a fake via TDD_RUNNER (tests).

    TDD_RUNNER="fake:<path-to-script-json>" selects FakeAgentRunner so that
    subprocess-level tests never touch the SDK; anything else (or unset)
    returns the production SdkAgentRunner. `verbose` streams the agent's prose
    and tool activity to stderr (SdkAgentRunner only; the fake has no stream).
    """
    runner_spec = os.environ.get(RUNNER_ENV_VAR, "")
    if runner_spec.startswith("fake:"):
        import tdd_fake_runner

        script_path = runner_spec.split(":", 1)[1]
        return tdd_fake_runner.FakeAgentRunner.from_script(
            script_path, repo_root
        )
    return SdkAgentRunner(repo_root, verbose=verbose)
