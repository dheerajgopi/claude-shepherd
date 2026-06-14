"""Shared fixtures for the spec-implement shepherd's own test suite.

Fixtures build their worlds with plain subprocess git + file writes — they
must NEVER call the engine's `init`/`new` (fixtures cannot depend on the code
under test). The CLI contract tests (test_cli_exitcodes.py) are the only
place the engine's verbs are exercised, deliberately, as subprocesses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SHEPHERD_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SHEPHERD_ROOT / "skills" / "spec-implement" / "scripts"
SPEC_IMPLEMENT_PY = SCRIPTS_DIR / "spec_implement.py"

# Module-level insertion: pytest.importorskip() at the top of test modules
# runs at collection time, before any fixture (including autouse ones) fires.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def scripts_on_path():
    """Keep skills/spec-implement/scripts importable for every test."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    yield


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A real initialized git repo with a src/ + tests/ pytest skeleton."""

    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "shepherd-tests@example.com")
    _git(repo, "config", "user.name", "Shepherd Tests")

    (repo / "README.md").write_text("# Demo project\n\nFixture repo.\n")
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    )
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def hello() -> str:\n    return 'hello'\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert 1 + 1 == 2\n"
    )

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def shepherd_repo(tmp_repo: Path) -> Path:
    """tmp_repo with .shepherd/config.yaml written from ShepherdConfig defaults."""

    from spec_implement_contracts import ShepherdConfig

    cfg = ShepherdConfig()
    cfg.test.command = "pytest -x -q"
    cfg.test.paths = ["tests"]

    shepherd = tmp_repo / ".shepherd"
    (shepherd / "features").mkdir(parents=True)
    (shepherd / "config.yaml").write_text(
        "models:\n"
        f"  design: {cfg.models.design}\n"
        f"  requirements: {cfg.models.requirements}\n"
        f"  testgen: {cfg.models.testgen}\n"
        f"  verifier: {cfg.models.verifier}\n"
        f"  implement: {cfg.models.implement}\n"
        "test:\n"
        f'  command: "{cfg.test.command}"\n'
        "  paths:\n"
        + "".join(f"    - {p}\n" for p in cfg.test.paths)
        + f"  syntax_check: {'true' if cfg.test.syntax_check else 'false'}\n"
        "budgets:\n"
        f"  max_turns_per_loop: {cfg.budgets.max_turns_per_loop}\n"
        f"  max_coverage_iterations: {cfg.budgets.max_coverage_iterations}\n"
        f"  max_cost_usd: {cfg.budgets.max_cost_usd}\n"
        f"  max_wall_clock_minutes: {cfg.budgets.max_wall_clock_minutes}\n"
    )
    return tmp_repo


def scaffold_feature(repo: Path, slug: str, *, checkout: bool = True) -> SimpleNamespace:
    """Scaffold a feature folder + spec-implement/<slug> branch + seeded state.json.

    Plain git/file plumbing — independent of the engine's `new`.
    """

    from spec_implement_contracts import (
        FeatureState,
        HistoryEntry,
        Phase,
        asdict_state,
    )

    branch = f"spec-implement/{slug}"
    feature_dir = repo / ".shepherd" / "features" / slug
    design_dir = feature_dir / "design"
    requirements_dir = feature_dir / "requirements"
    spec_implement_dir = feature_dir / ".spec-implement"
    design_dir.mkdir(parents=True)
    requirements_dir.mkdir(parents=True)
    (spec_implement_dir / "reports").mkdir(parents=True)
    (feature_dir / "task.md").write_text(f"Build the {slug} feature.\n")

    base_commit = _git(repo, "rev-parse", "HEAD").strip()
    if checkout:
        _git(repo, "checkout", "-b", branch)
    else:
        _git(repo, "branch", branch)

    now = datetime.now(timezone.utc).isoformat()
    state = FeatureState(
        slug=slug,
        branch=branch,
        base_commit=base_commit,
        phase=Phase.DRAFTING_REQUIREMENTS.value,
        history=[
            HistoryEntry(phase=Phase.DRAFTING_REQUIREMENTS.value, timestamp=now)
        ],
    )
    state_path = spec_implement_dir / "state.json"
    state_path.write_text(json.dumps(asdict_state(state), indent=2))

    return SimpleNamespace(
        repo=repo,
        slug=slug,
        branch=branch,
        feature_dir=feature_dir,
        design_dir=design_dir,
        requirements_dir=requirements_dir,
        spec_implement_dir=spec_implement_dir,
        state_path=state_path,
        base_commit=base_commit,
    )


@pytest.fixture
def feature(shepherd_repo: Path) -> SimpleNamespace:
    """A scaffolded 'user-auth' feature on branch spec-implement/user-auth (checked out)."""

    return scaffold_feature(shepherd_repo, "user-auth", checkout=True)


def run_cli(args, cwd, env_extra=None, stdin=None) -> subprocess.CompletedProcess:
    """Run [python, spec_implement.py, *args] in cwd; returns CompletedProcess."""

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SPEC_IMPLEMENT_PY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        input=stdin,
    )


@pytest.fixture(name="run_cli")
def run_cli_fixture():
    return run_cli
