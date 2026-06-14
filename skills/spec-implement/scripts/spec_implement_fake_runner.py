"""FakeAgentRunner — scripted test double for the AgentRunner protocol.

Owned by the test track (T2-TESTS). Production code never imports this except
through `spec_implement_agent.get_runner` when the SPEC_IMPLEMENT_RUNNER env var selects
"fake:<script.json>" (subprocess-level tests).

Stdlib only. Imports spec_implement_contracts always, and spec_implement_hooks lazily — the REAL
path-policy decision function gates scripted file side-effects, so the fake
exercises the same mechanical boundary as production. Runs without a path
policy never touch spec_implement_hooks at all.

Script file format (JSON)::

    {
      "runs": [
        {
          "text": "final assistant text",            // required-ish, default ""
          "session_id": "sess-1",                    // default: spec.session_id or "fake-<n>"
          "cost_usd": 0.01,                          // default 0.01
          "num_turns": 1,                            // default 1
          "is_error": false,                         // default false
          "files": [                                 // simulated Write side-effects
            {"path": "tests/test_x.py", "content": "..."}   // path repo-relative
          ],
          "tool_calls": [                            // appended verbatim as ToolEvents
            {"tool_name": "mcp__spec_implement__propose_test_change", "tool_input": {...}}
          ]
        }
      ]
    }

Each `.run(spec)` consumes the next entry IN ORDER and raises IndexError with
a clear message when the script is exhausted. Every received RunSpec is kept
in `self.received` for in-process assertions, and a JSON line per run is
appended to "<script_path>.calls.jsonl" so subprocess-level tests can assert
on what the engine sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spec_implement_contracts import RunResult, RunSpec, ToolEvent


class FakeAgentRunner:
    """Scripted implementation of the AgentRunner protocol (spec_implement_contracts)."""

    def __init__(
        self,
        runs: list[dict[str, Any]],
        script_path: str | Path,
        repo_root: Path,
    ) -> None:
        self._runs = list(runs)
        self._script_path = Path(script_path)
        self._repo_root = Path(repo_root)
        self._index = 0
        #: every RunSpec received, in order, for test assertions.
        self.received: list[RunSpec] = []

    @classmethod
    def from_script(cls, script_path: str, repo_root: Path) -> "FakeAgentRunner":
        path = Path(script_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(
                f"FakeAgentRunner script not found: {path}"
            ) from None
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"FakeAgentRunner script {path} is not valid JSON: {exc}"
            ) from exc
        runs = data.get("runs")
        if not isinstance(runs, list):
            raise ValueError(
                f"FakeAgentRunner script {path}: top-level 'runs' must be a list"
            )
        return cls(runs, path, Path(repo_root))

    # -- AgentRunner protocol -------------------------------------------------

    def run(self, spec: RunSpec) -> RunResult:
        if self._index >= len(self._runs):
            raise IndexError(
                f"FakeAgentRunner script exhausted: all {len(self._runs)} scripted "
                f"run(s) already consumed, but run #{self._index + 1} was requested "
                f"(script: {self._script_path}, prompt starts: {spec.prompt[:80]!r})"
            )
        entry = self._runs[self._index]
        self._index += 1
        n = self._index

        self.received.append(spec)

        session_id = entry.get("session_id") or spec.session_id or f"fake-{n}"

        tool_events: list[ToolEvent] = []
        tool_events.extend(self._apply_files(entry.get("files", []), spec))

        for call in entry.get("tool_calls", []):
            tool_events.append(
                ToolEvent(
                    tool_name=call["tool_name"],
                    tool_input=dict(call.get("tool_input", {})),
                    denied=bool(call.get("denied", False)),
                    deny_reason=call.get("deny_reason"),
                )
            )

        self._log_call(spec, session_id)

        return RunResult(
            session_id=session_id,
            text=entry.get("text", ""),
            tool_events=tool_events,
            cost_usd=float(entry.get("cost_usd", 0.01)),
            num_turns=int(entry.get("num_turns", 1)),
            is_error=bool(entry.get("is_error", False)),
        )

    # -- internals ------------------------------------------------------------

    def _apply_files(
        self, files: list[dict[str, Any]], spec: RunSpec
    ) -> list[ToolEvent]:
        """Simulate Write tool calls, gated by the REAL path policy."""

        if not files:
            return []

        policy = None
        if spec.path_policy_mode is not None:
            # Lazy import: only policy-bearing specs need the sibling module.
            from spec_implement_hooks import PathPolicy

            policy = PathPolicy(
                mode=spec.path_policy_mode,
                paths=list(spec.path_policy_paths),
                repo_root=self._repo_root,
            )

        events: list[ToolEvent] = []
        for entry in files:
            abs_path = self._repo_root / entry["path"]
            tool_input: dict[str, Any] = {"file_path": str(abs_path)}

            if policy is None:
                allowed, reason = True, ""
            else:
                from spec_implement_hooks import is_path_allowed

                allowed, reason = is_path_allowed("Write", tool_input, policy)

            if allowed:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(entry.get("content", ""), encoding="utf-8")

            events.append(
                ToolEvent(
                    tool_name="Write",
                    tool_input=tool_input,
                    denied=not allowed,
                    deny_reason=None if allowed else (reason or "denied by path policy"),
                )
            )
        return events

    def _log_call(self, spec: RunSpec, session_id: str) -> None:
        """Append one JSON line per run for subprocess-level assertions."""

        record = {
            "model": spec.model,
            "prompt_len": len(spec.prompt),
            "session_id": session_id,
            "spec_session_id": spec.session_id,
            "path_policy_mode": (
                spec.path_policy_mode.value if spec.path_policy_mode else None
            ),
            "path_policy_paths": list(spec.path_policy_paths),
        }
        log_path = Path(str(self._script_path) + ".calls.jsonl")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
