"""Tests for spec_implement_fake_runner.FakeAgentRunner (owned by this track).

Only the path-policy test needs the sibling spec_implement_hooks module; everything else
runs against spec_implement_contracts + the fake alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_implement_contracts import PathPolicyMode, RunSpec
from spec_implement_fake_runner import FakeAgentRunner


def _spec(**overrides) -> RunSpec:
    defaults = dict(
        prompt="do the thing",
        model="claude-haiku-4-5",
        system_prompt="you are a test",
    )
    defaults.update(overrides)
    return RunSpec(**defaults)


def _script(tmp_path: Path, runs: list[dict], name: str = "script.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"runs": runs}))
    return path


class TestScriptConsumption:
    def test_consumes_runs_in_order(self, tmp_path: Path) -> None:
        script = _script(
            tmp_path,
            [
                {"text": "first", "session_id": "s1", "cost_usd": 0.5, "num_turns": 3},
                {"text": "second"},
            ],
        )
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        r1 = runner.run(_spec())
        r2 = runner.run(_spec())

        assert (r1.text, r2.text) == ("first", "second")
        assert r1.session_id == "s1"
        assert r1.cost_usd == 0.5
        assert r1.num_turns == 3
        assert r1.is_error is False

    def test_defaults_applied(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "only"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        result = runner.run(_spec())

        assert result.session_id == "fake-1"  # auto "fake-<n>"
        assert result.cost_usd == 0.01
        assert result.num_turns == 1
        assert result.is_error is False
        assert result.tool_events == []

    def test_auto_session_id_counts_runs(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "a"}, {"text": "b"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        assert runner.run(_spec()).session_id == "fake-1"
        assert runner.run(_spec()).session_id == "fake-2"

    def test_spec_session_id_used_when_entry_has_none(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "resumed"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        result = runner.run(_spec(session_id="resume-me"))

        assert result.session_id == "resume-me"

    def test_entry_session_id_wins_over_spec(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "x", "session_id": "scripted"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        assert runner.run(_spec(session_id="resume-me")).session_id == "scripted"

    def test_exhaustion_raises_with_clear_message(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "only"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)
        runner.run(_spec())

        with pytest.raises(IndexError) as excinfo:
            runner.run(_spec())
        assert "exhausted" in str(excinfo.value)

    def test_received_records_every_spec(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "a"}, {"text": "b"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        spec1 = _spec(prompt="one")
        spec2 = _spec(prompt="two", session_id="s")
        runner.run(spec1)
        runner.run(spec2)

        assert runner.received == [spec1, spec2]


class TestFileSideEffects:
    def test_files_written_without_policy(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        script = _script(
            tmp_path,
            [{"text": "wrote", "files": [{"path": "notes/a.txt", "content": "hi"}]}],
        )
        runner = FakeAgentRunner.from_script(str(script), repo)

        result = runner.run(_spec())  # path_policy_mode=None -> allow

        assert (repo / "notes" / "a.txt").read_text() == "hi"
        assert len(result.tool_events) == 1
        event = result.tool_events[0]
        assert event.tool_name == "Write"
        assert event.denied is False
        assert event.tool_input["file_path"] == str(repo / "notes" / "a.txt")

    def test_policy_gates_file_writes(self, tmp_path: Path) -> None:
        pytest.importorskip("spec_implement_hooks")  # real policy engine, parallel track

        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "src").mkdir()
        script = _script(
            tmp_path,
            [
                {
                    "text": "tests written",
                    "files": [
                        {"path": "tests/test_new.py", "content": "def test_a(): pass\n"},
                        {"path": "src/evil.py", "content": "# must not land\n"},
                    ],
                }
            ],
        )
        runner = FakeAgentRunner.from_script(str(script), repo)
        spec = _spec(
            path_policy_mode=PathPolicyMode.ALLOW_ONLY,
            path_policy_paths=["tests"],
            cwd=str(repo),
        )

        result = runner.run(spec)

        assert (repo / "tests" / "test_new.py").exists()
        assert not (repo / "src" / "evil.py").exists()
        assert [e.denied for e in result.tool_events] == [False, True]
        denied = result.tool_events[1]
        assert denied.deny_reason  # non-empty reason recorded

    def test_tool_calls_appended_verbatim(self, tmp_path: Path) -> None:
        call = {
            "tool_name": "mcp__spec_implement__propose_test_change",
            "tool_input": {
                "test_file": "tests/test_auth.py",
                "related_requirement": "auth:Login",
                "reason": "rename fixture",
                "proposed_diff": "- old\n+ new\n",
            },
        }
        script = _script(tmp_path, [{"text": "proposing", "tool_calls": [call]}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        result = runner.run(_spec())

        assert len(result.tool_events) == 1
        event = result.tool_events[0]
        assert event.tool_name == "mcp__spec_implement__propose_test_change"
        assert event.tool_input == call["tool_input"]
        assert event.denied is False


class TestCallLog:
    def test_calls_jsonl_written_per_run(self, tmp_path: Path) -> None:
        script = _script(tmp_path, [{"text": "a"}, {"text": "b"}])
        runner = FakeAgentRunner.from_script(str(script), tmp_path)

        runner.run(_spec(prompt="first prompt", model="model-a"))
        runner.run(
            _spec(
                prompt="second",
                model="model-b",
                path_policy_mode=PathPolicyMode.DENY_UNDER,
                path_policy_paths=["tests"],
            )
        )

        log = Path(str(script) + ".calls.jsonl")
        lines = [json.loads(l) for l in log.read_text().splitlines()]
        assert len(lines) == 2
        assert lines[0]["model"] == "model-a"
        assert lines[0]["prompt_len"] == len("first prompt")
        assert lines[0]["path_policy_mode"] is None
        assert lines[1]["model"] == "model-b"
        assert lines[1]["path_policy_mode"] == "deny_under"
        assert lines[1]["path_policy_paths"] == ["tests"]
        assert lines[1]["session_id"] == "fake-2"


class TestScriptErrors:
    def test_missing_script_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            FakeAgentRunner.from_script(str(tmp_path / "nope.json"), tmp_path)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json {")
        with pytest.raises(ValueError):
            FakeAgentRunner.from_script(str(bad), tmp_path)

    def test_runs_must_be_a_list(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad2.json"
        bad.write_text(json.dumps({"runs": "nope"}))
        with pytest.raises(ValueError):
            FakeAgentRunner.from_script(str(bad), tmp_path)
