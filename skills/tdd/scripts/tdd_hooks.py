"""Path-policy hooks — the mechanical boundary (§9/§10).

The pure decision function `is_path_allowed` is the unit-tested core; the
SDK-facing hook factory `make_pretooluse_hook` wraps it in the verified
PreToolUse hook signature/shape documented in docs/sdk-notes.md §2.

This module has NO SDK imports at top level (or anywhere): the hook is a
plain async function returning plain dicts, so the module is importable
without claude-agent-sdk installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tdd_contracts import PathPolicyMode, ToolEvent, WRITE_TOOLS

# Tool name -> key in tool_input that carries the target path.
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


@dataclass
class PathPolicy:
    mode: PathPolicyMode
    paths: list[str]  # repo-relative
    repo_root: Path


def is_path_allowed(
    tool_name: str, tool_input: dict, policy: PathPolicy
) -> tuple[bool, str]:
    """Pure decision: may `tool_name` with `tool_input` proceed under `policy`?

    Returns (allowed, reason). Reason is "" when allowed; when denied it is a
    message suitable for feeding back to the model verbatim.
    """
    if tool_name not in WRITE_TOOLS:
        return (True, "")

    path_key = _PATH_KEYS[tool_name]
    raw_path = tool_input.get(path_key)
    if not raw_path:
        return (False, "no path in tool input")

    repo_root = policy.repo_root.resolve(strict=False)

    # Resolve the target to an absolute, normalized path ('..' collapsed)
    # without requiring it to exist; relative paths resolve against repo_root.
    target = Path(raw_path)
    if not target.is_absolute():
        target = repo_root / target
    target = target.resolve(strict=False)

    # Paths escaping the repository root are never allowed, in either mode.
    if not target.is_relative_to(repo_root):
        return (
            False,
            f"'{raw_path}' resolves to '{target}', outside the repository "
            f"root '{repo_root}' — this boundary is mechanical, do not retry",
        )

    # Segment-aware containment against each policy path (repo-relative).
    resolved_policy_paths = [
        (p, (repo_root / p).resolve(strict=False)) for p in policy.paths
    ]
    matching = [p for p, rp in resolved_policy_paths if target.is_relative_to(rp)]

    if policy.mode is PathPolicyMode.ALLOW_ONLY:
        if matching:
            return (True, "")
        return (
            False,
            f"writes are restricted to {policy.paths}; '{raw_path}' is "
            f"outside — this boundary is mechanical, do not retry",
        )

    # PathPolicyMode.DENY_UNDER
    if matching:
        return (
            False,
            f"'{raw_path}' is under protected path(s) {matching}; tests/specs "
            f"are the contract. To request a test change use the "
            f"propose_test_change tool",
        )
    return (True, "")


def make_pretooluse_hook(
    policy: PathPolicy, events: list | None = None
) -> Callable:
    """Build a PreToolUse hook enforcing `policy`.

    The returned coroutine function has the verified SDK hook signature
    `(input_data, tool_use_id, context)` and returns the verified deny shape
    (docs/sdk-notes.md §2) on deny, `{}` (no opinion) on allow.

    If `events` is provided, a tdd_contracts.ToolEvent is appended for every
    WRITE_TOOLS invocation observed, with denied/deny_reason set accordingly.
    """

    async def pretooluse_hook(
        input_data: dict, tool_use_id: Any, context: Any
    ) -> dict:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}

        allowed, reason = is_path_allowed(tool_name, tool_input, policy)

        if events is not None and tool_name in WRITE_TOOLS:
            events.append(
                ToolEvent(
                    tool_name=tool_name,
                    tool_input=dict(tool_input),
                    denied=not allowed,
                    deny_reason=None if allowed else reason,
                )
            )

        if allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return pretooluse_hook
