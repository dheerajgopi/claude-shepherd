"""Tests for tdd_state.resolve_feature — active-feature resolution (§7)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

tdd_state = pytest.importorskip("tdd_state")  # parallel track (T1-CORE)

from tdd_contracts import ExitCode  # noqa: E402
from tdd_state import HarnessError, resolve_feature  # noqa: E402

from conftest import scaffold_feature  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


class TestExplicitArgument:
    def test_explicit_matching_branch_resolves(self, feature) -> None:
        ctx = resolve_feature(feature.repo, "user-auth", False)
        assert ctx.slug == "user-auth"

    def test_explicit_wins_over_branch_convention(self, feature) -> None:
        # A second feature whose branch is checked out; explicit arg must still
        # select user-auth (force bypasses the resulting branch mismatch).
        scaffold_feature(feature.repo, "other-feat", checkout=True)
        assert _git(feature.repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == (
            "tdd/other-feat"
        )

        ctx = resolve_feature(feature.repo, "user-auth", True)
        assert ctx.slug == "user-auth"

    def test_unknown_slug_lists_existing_features(self, feature) -> None:
        with pytest.raises(HarnessError) as excinfo:
            resolve_feature(feature.repo, "no-such-feature", False)
        assert excinfo.value.exit_code == ExitCode.NO_FEATURE_RESOLVED
        assert "user-auth" in str(excinfo.value.message)


class TestBranchConvention:
    def test_tdd_branch_with_folder_resolves(self, feature) -> None:
        ctx = resolve_feature(feature.repo, None, False)
        assert ctx.slug == "user-auth"

    def test_context_fields_populated(self, feature) -> None:
        ctx = resolve_feature(feature.repo, None, False)
        assert Path(ctx.repo_root).resolve() == feature.repo.resolve()
        assert Path(ctx.feature_dir).resolve() == feature.feature_dir.resolve()
        assert Path(ctx.gherkin_dir).resolve() == feature.gherkin_dir.resolve()
        assert Path(ctx.tdd_dir).resolve() == feature.tdd_dir.resolve()
        assert ctx.state.slug == "user-auth"
        assert ctx.state.branch == "tdd/user-auth"
        assert ctx.config.test.paths == ["tests"]
        assert ctx.store is not None
        assert ctx.reports_dir is not None

    def test_main_branch_without_arg_fails(self, feature) -> None:
        _git(feature.repo, "checkout", "main")

        with pytest.raises(HarnessError) as excinfo:
            resolve_feature(feature.repo, None, False)
        assert excinfo.value.exit_code == ExitCode.NO_FEATURE_RESOLVED


class TestBranchMismatch:
    def test_recorded_branch_differs_from_current(self, feature) -> None:
        _git(feature.repo, "checkout", "main")

        with pytest.raises(HarnessError) as excinfo:
            resolve_feature(feature.repo, "user-auth", False)
        assert excinfo.value.exit_code == ExitCode.BRANCH_MISMATCH

    def test_force_overrides_mismatch(self, feature) -> None:
        _git(feature.repo, "checkout", "main")

        ctx = resolve_feature(feature.repo, "user-auth", True)
        assert ctx.slug == "user-auth"
