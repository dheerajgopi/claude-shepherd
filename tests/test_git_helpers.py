"""Tests for tdd_git — git plumbing helpers (§16 dirty-tree policy included)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

tdd_git = pytest.importorskip("tdd_git")  # parallel track (T1-CORE)

from tdd_git import (  # noqa: E402
    branch_exists,
    commit_paths,
    create_branch,
    current_branch,
    git,
    head_sha,
    is_dirty,
    repo_root,
)


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class TestPlumbing:
    def test_git_returns_stdout(self, tmp_repo: Path) -> None:
        out = git(["rev-parse", "--abbrev-ref", "HEAD"], tmp_repo)
        assert out.strip() == "main"

    def test_repo_root_from_subdirectory(self, tmp_repo: Path) -> None:
        root = repo_root(tmp_repo / "src")
        assert root is not None
        assert Path(root).resolve() == tmp_repo.resolve()

    def test_repo_root_none_outside_repo(self, tmp_path_factory) -> None:
        outside = tmp_path_factory.mktemp("not-a-repo")
        assert repo_root(outside) is None

    def test_current_branch(self, tmp_repo: Path) -> None:
        assert current_branch(tmp_repo) == "main"

    def test_head_sha_is_full_hex(self, tmp_repo: Path) -> None:
        sha = head_sha(tmp_repo)
        assert re.fullmatch(r"[0-9a-f]{40}", sha), sha


class TestIsDirty:
    """Dirty-tree policy (§16): untracked `.sluice` files are excepted."""

    def test_clean_tree(self, tmp_repo: Path) -> None:
        assert is_dirty(tmp_repo) is False

    def test_untracked_under_sluice_is_clean(self, tmp_repo: Path) -> None:
        sluice = tmp_repo / ".sluice" / "features" / "x"
        sluice.mkdir(parents=True)
        (sluice / "task.md").write_text("untracked sluice file\n")
        (tmp_repo / ".sluice" / "config.yaml").write_text("test: {}\n")

        assert is_dirty(tmp_repo) is False

    def test_untracked_elsewhere_is_dirty(self, tmp_repo: Path) -> None:
        (tmp_repo / "stray.txt").write_text("untracked\n")
        assert is_dirty(tmp_repo) is True

    def test_modified_tracked_file_is_dirty(self, tmp_repo: Path) -> None:
        readme = tmp_repo / "README.md"
        readme.write_text(readme.read_text() + "\nlocal edit\n")
        assert is_dirty(tmp_repo) is True

    def test_modified_tracked_file_under_sluice_still_counts(
        self, tmp_repo: Path
    ) -> None:
        # The exception is for UNTRACKED .sluice files only; a tracked,
        # modified file under .sluice must count as dirty.
        cfg = tmp_repo / ".sluice" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("a: 1\n")
        subprocess.run(
            ["git", "add", str(cfg)], cwd=tmp_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "track config"],
            cwd=tmp_repo,
            check=True,
            capture_output=True,
        )
        cfg.write_text("a: 2\n")

        assert is_dirty(tmp_repo) is True


class TestBranches:
    def test_create_and_exists(self, tmp_repo: Path) -> None:
        assert branch_exists(tmp_repo, "tdd/user-auth") is False
        create_branch(tmp_repo, "tdd/user-auth")
        assert branch_exists(tmp_repo, "tdd/user-auth") is True

    def test_exists_false_for_unknown(self, tmp_repo: Path) -> None:
        assert branch_exists(tmp_repo, "tdd/nope") is False


class TestCommitPaths:
    def test_commits_only_given_paths(self, tmp_repo: Path) -> None:
        readme = tmp_repo / "README.md"
        app = tmp_repo / "src" / "app.py"
        readme.write_text(readme.read_text() + "\nchange one\n")
        app.write_text(app.read_text() + "\n# change two\n")

        commit_paths(tmp_repo, "tdd(user-auth): spec — gherkin scenarios", ["README.md"])

        # The other modified file must remain uncommitted.
        status = _status(tmp_repo)
        assert "src/app.py" in status
        assert "README.md" not in status

        # HEAD contains exactly the given path with the given message.
        show = subprocess.run(
            ["git", "show", "--name-only", "--format=%s"],
            cwd=tmp_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert show.splitlines()[0] == "tdd(user-auth): spec — gherkin scenarios"
        assert "README.md" in show
        assert "src/app.py" not in show

    def test_commits_new_files(self, tmp_repo: Path) -> None:
        new = tmp_repo / "tests" / "test_new.py"
        new.write_text("def test_new(): pass\n")

        commit_paths(tmp_repo, "tdd(user-auth): red — failing tests", ["tests/test_new.py"])

        assert "tests/test_new.py" not in _status(tmp_repo)
        assert is_dirty(tmp_repo) is False
