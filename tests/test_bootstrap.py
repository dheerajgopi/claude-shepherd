"""Tests for tdd_bootstrap — the test-framework bootstrap pre-step.

Two layers: (1) `propose_framework` is a pure, deterministic recipe over a
project's markers — tested against tiny scratch dirs; (2) `run_bootstrap`
drives the propose → approve → install phase machine in-process against a real
git repo and a scripted FakeAgentRunner (the install agent's manifest edit is
gated by the REAL ALLOW_ONLY path policy, exactly like production).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tdd_bootstrap import (
    FrameworkProposal,
    propose_framework,
    run_bootstrap,
    should_bootstrap,
)
from tdd_contracts import ExitCode, LoopStatus, PathPolicyMode, Phase
from tdd_fake_runner import FakeAgentRunner
from tdd_state import StateStore, load_config, resolve_feature


# ---------------------------------------------------------------------------
# propose_framework — deterministic recipe
# ---------------------------------------------------------------------------


class TestProposePython:
    def test_pyproject_without_pytest_proposes_pytest(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        proposal = propose_framework(tmp_path)
        assert proposal is not None
        assert proposal.framework == "pytest"
        assert proposal.language == "python"
        assert "pyproject.toml" in proposal.manifest_files
        assert proposal.install_command == "pip install pytest"
        assert proposal.test_command == "pytest"
        assert proposal.test_paths == ["tests"]

    def test_pytest_already_declared_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n'
            "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
        )
        assert propose_framework(tmp_path) is None

    def test_pytest_in_requirements_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("pytest>=8\n")
        assert propose_framework(tmp_path) is None

    def test_uv_lock_uses_uv_add(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "uv.lock").write_text("# lock\n")
        assert propose_framework(tmp_path).install_command == "uv add --dev pytest"

    def test_poetry_uses_poetry_add(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "demo"\n'
        )
        assert (
            propose_framework(tmp_path).install_command
            == "poetry add --group dev pytest"
        )


class TestProposeJavaScript:
    def test_package_json_without_framework_defaults_to_jest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}))
        proposal = propose_framework(tmp_path)
        assert proposal.framework == "jest"
        assert proposal.manifest_files == ["package.json"]
        assert proposal.install_command == "npm install -D jest"
        assert proposal.test_command == "npm test"

    def test_vite_project_prefers_vitest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"vite": "^5"}})
        )
        assert propose_framework(tmp_path).framework == "vitest"

    def test_feedback_overrides_framework_choice(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}))
        assert propose_framework(tmp_path, "please use mocha").framework == "mocha"

    def test_existing_framework_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"jest": "^29"}})
        )
        assert propose_framework(tmp_path) is None

    def test_pnpm_lock_uses_pnpm_add(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}))
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        proposal = propose_framework(tmp_path)
        assert proposal.install_command == "pnpm add -D jest"
        assert proposal.test_command == "pnpm test"


class TestProposeJava:
    def test_maven_without_junit_proposes_junit(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project></project>")
        proposal = propose_framework(tmp_path)
        assert proposal.framework == "JUnit 5"
        assert proposal.manifest_files == ["pom.xml"]
        assert proposal.test_command == "mvn -q test"
        assert proposal.test_paths == ["src/test/java"]

    def test_maven_with_junit_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependency>junit-jupiter</dependency></project>"
        )
        assert propose_framework(tmp_path) is None

    def test_gradle_without_junit_proposes_junit(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
        proposal = propose_framework(tmp_path)
        assert proposal.framework == "JUnit 5"
        assert proposal.manifest_files == ["build.gradle"]
        assert proposal.test_command == "gradle test"


class TestNoRecipe:
    def test_go_module_has_stdlib_testing(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module demo\n")
        assert propose_framework(tmp_path) is None

    def test_rust_crate_has_stdlib_testing(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")
        assert propose_framework(tmp_path) is None

    def test_unknown_language_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# just docs\n")
        assert propose_framework(tmp_path) is None


# ---------------------------------------------------------------------------
# run_bootstrap — the propose → approve → install phase machine
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def bare_feature(tmp_path: Path) -> SimpleNamespace:
    """A git repo with a Python project that has NO test framework, plus a
    feature scaffolded at REQUIREMENTS_APPROVED and a config with no test setup."""

    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "shepherd-tests@example.com")
    _git(repo, "config", "user.name", "Shepherd Tests")
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")

    from tdd_contracts import FeatureState, HistoryEntry, asdict_state
    from tdd_state import utc_now_iso

    shepherd = repo / ".shepherd"
    (shepherd / "features").mkdir(parents=True)
    (shepherd / "config.yaml").write_text(
        "models:\n  testgen: claude-sonnet-4-6\n  implement: claude-sonnet-4-6\n"
        "test:\n  command: \"\"\n  paths: []\n  syntax_check: false\n"
        "budgets:\n  max_turns_per_loop: 40\n  max_coverage_iterations: 5\n"
        "  max_cost_usd: 10.0\n  max_wall_clock_minutes: 120\n"
    )

    slug = "user-auth"
    branch = f"tdd/{slug}"
    feature_dir = shepherd / "features" / slug
    tdd_dir = feature_dir / ".tdd"
    (feature_dir / "design").mkdir(parents=True)
    (feature_dir / "requirements").mkdir(parents=True)
    (tdd_dir / "reports").mkdir(parents=True)
    (feature_dir / "task.md").write_text("Build user auth.\n")
    _git(repo, "checkout", "-b", branch)

    now = utc_now_iso()
    state = FeatureState(
        slug=slug,
        branch=branch,
        base_commit=_git(repo, "rev-parse", "HEAD").strip(),
        phase=Phase.REQUIREMENTS_APPROVED.value,
        history=[HistoryEntry(phase=Phase.REQUIREMENTS_APPROVED.value, timestamp=now)],
    )
    (tdd_dir / "state.json").write_text(json.dumps(asdict_state(state), indent=2))

    return SimpleNamespace(
        repo=repo, slug=slug, branch=branch, feature_dir=feature_dir
    )


@pytest.fixture
def script_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("fake-scripts")


def _runner(script_dir: Path, repo: Path, runs: list[dict]) -> FakeAgentRunner:
    path = script_dir / "script.json"
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    return FakeAgentRunner.from_script(str(path), repo)


def _ctx(bare_feature: SimpleNamespace):
    return resolve_feature(bare_feature.repo, bare_feature.slug, False)


#: An install run that edits pyproject.toml (inside the manifest scope) and
#: runs the install command, plus a denied write outside the scope.
INSTALL_RUN = {
    "text": "Added pytest.",
    "session_id": "boot-sess",
    "cost_usd": 0.2,
    "num_turns": 3,
    "files": [
        {
            "path": "pyproject.toml",
            "content": (
                '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
                "[project.optional-dependencies]\ntest = [\"pytest\"]\n\n"
                "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
            ),
        },
        {"path": "src/evil.py", "content": "# must be denied\n"},
    ],
    "tool_calls": [
        {"tool_name": "Bash", "tool_input": {"command": "pip install pytest"}}
    ],
}


class TestProposePhase:
    def test_entry_proposes_and_checkpoints_16(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        runner = _runner(script_dir, bare_feature.repo, [])

        outcome = run_bootstrap(ctx, runner, None, None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_FRAMEWORK_APPROVAL
        assert int(outcome.exit_code) == 16
        assert runner.received == []  # proposal is deterministic — no agent run

        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.AWAITING_FRAMEWORK_APPROVAL.value
        report = ctx.reports_dir / "framework_proposal.md"
        data = ctx.reports_dir / "framework_proposal.json"
        assert report.is_file() and "pytest" in report.read_text()
        assert json.loads(data.read_text())["framework"] == "pytest"

    def test_should_bootstrap_true_for_bare_python(self, bare_feature) -> None:
        assert should_bootstrap(_ctx(bare_feature)) is True


class TestApproveInstalls:
    def test_approve_installs_commits_and_advances(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        runner = _runner(script_dir, bare_feature.repo, [INSTALL_RUN])

        run_bootstrap(ctx, runner, None, None)              # propose → 16
        commits_before = _git(bare_feature.repo, "rev-list", "--count", "HEAD").strip()

        outcome = run_bootstrap(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.ADVANCE
        assert "pytest" in outcome.detail
        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.GENERATING_TESTS.value
        assert state.session_ids.get("bootstrap") == "boot-sess"
        assert state.budgets_spent.turns_bootstrap == 3

        # A single bootstrap commit landed, with the exact subject.
        commits_after = _git(bare_feature.repo, "rev-list", "--count", "HEAD").strip()
        assert int(commits_after) == int(commits_before) + 1
        assert _git(bare_feature.repo, "log", "-1", "--format=%s").strip() == (
            "tdd(user-auth): chore — add pytest"
        )

        # The denied write never landed; pyproject did.
        assert not (bare_feature.repo / "src" / "evil.py").exists()
        assert "pytest" in (bare_feature.repo / "pyproject.toml").read_text()

        # Config now carries the framework's test command + paths for Loop 2/3.
        cfg = load_config(bare_feature.repo)
        assert cfg.test.command == "pytest"
        assert cfg.test.paths == ["tests"]

    def test_install_runspec_is_manifest_scoped(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        runner = _runner(script_dir, bare_feature.repo, [INSTALL_RUN])
        run_bootstrap(ctx, runner, None, None)

        run_bootstrap(ctx, runner, "approve", None)

        (spec,) = runner.received
        assert spec.path_policy_mode is PathPolicyMode.ALLOW_ONLY
        assert "pyproject.toml" in spec.path_policy_paths
        assert "Bash" in spec.allowed_tools
        assert spec.model == ctx.config.models.implement

    def test_no_changes_fails(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        # An install run that writes nothing → engine has nothing to commit.
        runner = _runner(
            script_dir, bare_feature.repo,
            [{"text": "did nothing", "session_id": "boot-sess"}],
        )
        run_bootstrap(ctx, runner, None, None)

        outcome = run_bootstrap(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.FAILED
        assert outcome.exit_code is ExitCode.INTERNAL_ERROR
        assert "no manifest changes" in outcome.detail


class TestCorrectionsCycle:
    def test_feedback_reproposes_and_checkpoints_again(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        runner = _runner(script_dir, bare_feature.repo, [])
        run_bootstrap(ctx, runner, None, None)  # → AWAITING_FRAMEWORK_APPROVAL

        outcome = run_bootstrap(ctx, runner, None, "actually keep using pytest")

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.AWAITING_FRAMEWORK_APPROVAL
        assert runner.received == []
        state = StateStore(ctx.feature_dir).load()
        assert state.phase == Phase.AWAITING_FRAMEWORK_APPROVAL.value
        # the corrections are recorded in the persisted proposal
        data = json.loads((ctx.reports_dir / "framework_proposal.json").read_text())
        assert data["feedback"] == "actually keep using pytest"


class TestBudgetGuard:
    def test_cost_budget_blocks_install(self, bare_feature, script_dir) -> None:
        ctx = _ctx(bare_feature)
        runner = _runner(script_dir, bare_feature.repo, [INSTALL_RUN])
        run_bootstrap(ctx, runner, None, None)
        ctx.state.budgets_spent.cost_usd = ctx.config.budgets.max_cost_usd
        ctx.store.save(ctx.state)

        outcome = run_bootstrap(ctx, runner, "approve", None)

        assert outcome.status is LoopStatus.CHECKPOINT
        assert outcome.exit_code is ExitCode.BUDGET_EXCEEDED
        assert runner.received == []  # guard trips before the agent runs
