"""End-to-end journeys through the real CLI (§15 flow), fake-SDK only.

Every step is a true `tdd.py run` subprocess with `TDD_RUNNER=fake:<script>`;
each invocation gets its OWN script file (a fresh process builds a fresh
FakeAgentRunner, which consumes its script from the top). This exercises the
full production path: argparse → feature resolution → phase dispatch → loop
chaining → exit codes at the process boundary → commit choreography.

The scratch project's test command is the PASS-marker trick: it exits 0 iff
a PASS file exists at the repo root, so scripted implementer runs flip
red → green by writing PASS (allowed under DENY_UNDER tests/+gherkin).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import run_cli
from tdd_contracts import (
    COMMIT_GREEN,
    COMMIT_RED,
    COMMIT_RED_AMENDED,
    COMMIT_SPEC,
    ExitCode,
    Phase,
)

PASS_COMMAND = (
    'python3 -c "import sys,pathlib; '
    "sys.exit(0 if pathlib.Path('PASS').exists() else 1)\""
)

SCENARIO_ID = "user_auth:Login succeeds"

FEATURE_TEXT = """Feature: User auth

  Scenario: Login succeeds
    Given a registered user
    When they submit valid credentials
    Then they are logged in
"""

TEST_CONTENT = (
    "# scenario: user_auth:Login succeeds\n"
    "import pathlib\n\n"
    "def test_login():\n"
    "    assert pathlib.Path('PASS').exists()\n"
)

GHERKIN_REL = ".harness/features/user-auth/gherkin/user_auth.feature"

MATRIX_COVERED = json.dumps(
    {
        "scenarios": [
            {
                "scenario_id": SCENARIO_ID,
                "feature_file": "user_auth.feature",
                "tests": ["tests/test_user_auth.py::test_login"],
                "status": "covered",
            }
        ]
    }
)

MATRIX_MISSING = json.dumps(
    {
        "scenarios": [
            {
                "scenario_id": SCENARIO_ID,
                "feature_file": "user_auth.feature",
                "tests": [],
                "status": "missing",
                "notes": "nothing exercises login",
            }
        ]
    }
)

# Scripted runs, by role -----------------------------------------------------

DRAFT_GHERKIN = {
    "text": "Drafted. Scenario: Login succeeds — happy-path login.",
    "session_id": "l1-sess",
    "files": [{"path": GHERKIN_REL, "content": FEATURE_TEXT}],
}
GEN_TESTS = {
    "text": "tests written",
    "session_id": "l2-sess",
    "files": [{"path": "tests/test_user_auth.py", "content": TEST_CONTENT}],
}
VERIFY_COVERED = {"text": MATRIX_COVERED}
VERIFY_MISSING = {"text": MATRIX_MISSING}
IMPLEMENT_GREEN = {
    "text": "implemented",
    "session_id": "l3-sess",
    "files": [{"path": "PASS", "content": "1"}],
}


def _propose(reason: str) -> dict:
    return {
        "text": "test seems wrong",
        "session_id": "l3-sess",
        "tool_calls": [
            {
                "tool_name": "propose_test_change",
                "tool_input": {
                    "test_file": "tests/test_user_auth.py",
                    "related_scenario": SCENARIO_ID,
                    "reason": reason,
                    "proposed_diff": "@@ -4,1 +4,1 @@\n-    assert pathlib.Path('PASS').exists()\n+    assert True\n",
                },
            }
        ],
    }


def _verdict(v: str) -> dict:
    return {"text": json.dumps({"verdict": v, "rationale": f"{v} per triage"})}


AMEND_GHERKIN = {
    "text": f"done\nAMENDED: {SCENARIO_ID}",
    "session_id": "l1-sess",
    "files": [{"path": GHERKIN_REL, "content": FEATURE_TEXT + "    # amended\n"}],
}
RESYNC_TESTS = {
    "text": "resynced",
    "session_id": "l2-sess",
    "files": [
        {
            "path": "tests/test_user_auth.py",
            "content": TEST_CONTENT + "# resynced\n",
        }
    ],
}


# World ----------------------------------------------------------------------


@pytest.fixture()
def world(tmp_repo, tmp_path_factory):
    """Initialized scratch project + per-invocation script factory."""

    scripts_dir = tmp_path_factory.mktemp("fake_scripts")
    counter = {"n": 0}

    def step(args: list[str], runs: list[dict]) -> subprocess.CompletedProcess:
        counter["n"] += 1
        script = scripts_dir / f"step_{counter['n']}.json"
        script.write_text(json.dumps({"runs": runs}))
        return run_cli(
            args, tmp_repo, env_extra={"TDD_RUNNER": f"fake:{script}"}
        )

    init = run_cli(["init"], tmp_repo)
    assert init.returncode == 0, init.stderr
    # The PASS-marker test command (and PASS itself ignored for dirty checks
    # is unnecessary: PASS is created mid-loop3 and committed by green).
    import yaml

    cfg = tmp_repo / ".harness" / "config.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["test"]["command"] = PASS_COMMAND
    data["test"]["paths"] = ["tests"]
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    subprocess.run(
        ["git", "add", "-A"], cwd=tmp_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "configure harness"],
        cwd=tmp_repo, check=True, capture_output=True,
    )

    new = run_cli(["new", "User auth"], tmp_repo)
    assert new.returncode == 0, new.stderr

    return tmp_repo, step


def _subjects(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    return out.splitlines()


def _phase(repo: Path) -> str:
    state = json.loads(
        (repo / ".harness/features/user-auth/.tdd/state.json").read_text()
    )
    return state["phase"]


# Journeys ---------------------------------------------------------------------


class TestHappyPath:
    def test_draft_approve_to_green(self, world) -> None:
        repo, step = world

        # 1. Draft → checkpoint 10.
        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL, r.stderr
        assert "user_auth.feature" in r.stdout
        assert _phase(repo) == Phase.AWAITING_APPROVAL.value

        # 2. Approve → loop1 commit, loop2 (gen+verify+red), loop3 (impl+green).
        r = step(
            ["run", "--decision", "approve"],
            [GEN_TESTS, VERIFY_COVERED, IMPLEMENT_GREEN],
        )
        assert r.returncode == ExitCode.DONE, r.stderr
        assert _phase(repo) == Phase.DONE.value

        # Commit choreography (§16), newest first.
        subjects = _subjects(repo)
        assert subjects[0] == COMMIT_GREEN.format(slug="user-auth")
        assert subjects[1] == COMMIT_RED.format(slug="user-auth")
        assert subjects[2] == COMMIT_SPEC.format(slug="user-auth")

        # Audit artifacts committed; state.json not.
        shown = subprocess.run(
            ["git", "show", "--name-only", "HEAD~1"], cwd=repo,
            check=True, capture_output=True, text=True,
        ).stdout
        assert "traceability.json" in shown
        assert "state.json" not in shown

        # Re-run after DONE: friendly exit 0.
        r = step(["run"], [])
        assert r.returncode == ExitCode.DONE

    def test_correction_cycle_resumes_session(self, world) -> None:
        repo, step = world

        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL

        r = step(["run", "--feedback", "split the scenario"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        assert _phase(repo) == Phase.AWAITING_APPROVAL.value

        # The revision run resumed the recorded loop1 session (§8, §12) —
        # visible in the fake's call log: spec_session_id == "l1-sess".
        # (Each step has its own script; the log sits next to script #2.)
        # Re-running with no input is an idempotent checkpoint.
        r = step(["run"], [])
        assert r.returncode == ExitCode.AWAITING_APPROVAL


class TestCoverageGap:
    def test_uncoverable_scenarios_exit_11(self, world) -> None:
        repo, step = world

        import yaml

        cfg = repo / ".harness" / "config.yaml"
        data = yaml.safe_load(cfg.read_text())
        data["budgets"]["max_coverage_iterations"] = 1
        cfg.write_text(yaml.safe_dump(data, sort_keys=False))
        subprocess.run(["git", "commit", "-aqm", "tighten"], cwd=repo, check=True)

        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL

        r = step(["run", "--decision", "approve"], [GEN_TESTS, VERIFY_MISSING])
        assert r.returncode == ExitCode.COVERAGE_GAP, r.stderr
        gap = repo / ".harness/features/user-auth/.tdd/reports/coverage_gap.md"
        assert gap.is_file()
        assert SCENARIO_ID in gap.read_text()
        assert _phase(repo) == Phase.VERIFYING_COVERAGE.value


class TestEscalation:
    def _to_escalated(self, repo, step) -> None:
        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        r = step(
            ["run", "--decision", "approve"],
            [GEN_TESTS, VERIFY_COVERED, _propose("weaken"), _verdict("significant")],
        )
        assert r.returncode == ExitCode.ESCALATED, r.stderr
        assert _phase(repo) == Phase.ESCALATED.value
        report = repo / ".harness/features/user-auth/.tdd/reports/escalation_1.md"
        assert report.is_file()

    def test_approve_amends_and_creates_red2(self, world) -> None:
        repo, step = world
        self._to_escalated(repo, step)

        # Approve: loop1 amend, loop2 resync (gen+verify), loop3 to green.
        r = step(
            ["run", "--decision", "approve"],
            [AMEND_GHERKIN, RESYNC_TESTS, VERIFY_COVERED, IMPLEMENT_GREEN],
        )
        assert r.returncode == ExitCode.DONE, r.stderr

        subjects = _subjects(repo)
        assert subjects[0] == COMMIT_GREEN.format(slug="user-auth")
        assert subjects[1] == COMMIT_RED_AMENDED.format(slug="user-auth", n=2)
        assert COMMIT_RED.format(slug="user-auth") in subjects
        assert _phase(repo) == Phase.DONE.value

        # The renegotiation is auditable: resync revision bump in the matrix.
        matrix = json.loads(
            (repo / ".harness/features/user-auth/.tdd/traceability.json").read_text()
        )
        kinds = [rev["kind"] for rev in matrix["revisions"]]
        assert "resync" in kinds

    def test_reject_resumes_implementation(self, world) -> None:
        repo, step = world
        self._to_escalated(repo, step)

        r = step(
            ["run", "--decision", "reject", "--feedback", "test is right"],
            [IMPLEMENT_GREEN],
        )
        assert r.returncode == ExitCode.DONE, r.stderr
        assert _subjects(repo)[0] == COMMIT_GREEN.format(slug="user-auth")


class TestBudget:
    def test_cost_budget_exit_13(self, world) -> None:
        repo, step = world

        import yaml

        cfg = repo / ".harness" / "config.yaml"
        data = yaml.safe_load(cfg.read_text())
        data["budgets"]["max_cost_usd"] = 0.001
        cfg.write_text(yaml.safe_dump(data, sort_keys=False))
        subprocess.run(["git", "commit", "-aqm", "tiny budget"], cwd=repo, check=True)

        # First draft run spends 0.01 (fake default) > 0.001 → the NEXT
        # invocation's guard trips before any run.
        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        r = step(["run", "--feedback", "more"], [])
        assert r.returncode == ExitCode.BUDGET_EXCEEDED, r.stderr
        assert "budget" in r.stdout.lower()


class TestTraceabilityGate:
    def test_tampered_matrix_blocks_green(self, world) -> None:
        repo, step = world

        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        # Stop right after red: loop3's first implement run errors out.
        r = step(
            ["run", "--decision", "approve"],
            [GEN_TESTS, VERIFY_COVERED, {"text": "", "is_error": True}],
        )
        assert r.returncode == ExitCode.INTERNAL_ERROR
        assert _phase(repo) == Phase.IMPLEMENTING.value

        # Tamper: matrix now maps a function that does not exist; make the
        # suite trivially green. A deleted/renamed test must not fake DONE.
        trace = repo / ".harness/features/user-auth/.tdd/traceability.json"
        trace.write_text(trace.read_text().replace("test_login", "test_ghost"))
        (repo / "PASS").write_text("1")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "tamper"], cwd=repo, check=True
        )

        r = step(["run"], [])
        assert r.returncode == ExitCode.INTERNAL_ERROR
        assert "traceability" in (r.stdout + r.stderr).lower()
        violation = (
            repo / ".harness/features/user-auth/.tdd/reports/traceability_violation.md"
        )
        assert violation.is_file()
        assert COMMIT_GREEN.format(slug="user-auth") not in _subjects(repo)


class TestCrashRecovery:
    def test_rerun_from_every_checkpointed_phase(self, world) -> None:
        """Walk the happy path, re-running `run` redundantly at checkpoints —
        re-entry must never corrupt state or duplicate commits."""

        repo, step = world

        r = step(["run"], [DRAFT_GHERKIN])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        before = len(_subjects(repo))

        # Redundant re-run at AWAITING_APPROVAL: no commits, same phase.
        r = step(["run"], [])
        assert r.returncode == ExitCode.AWAITING_APPROVAL
        assert len(_subjects(repo)) == before

        r = step(
            ["run", "--decision", "approve"],
            [GEN_TESTS, VERIFY_COVERED, IMPLEMENT_GREEN],
        )
        assert r.returncode == ExitCode.DONE

        # Redundant re-run at DONE: friendly, no new commits.
        after = len(_subjects(repo))
        r = step(["run"], [])
        assert r.returncode == ExitCode.DONE
        assert len(_subjects(repo)) == after
