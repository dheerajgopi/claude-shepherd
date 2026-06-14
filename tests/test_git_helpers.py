"""Tests for spec_implement_git — git plumbing helpers (§16 dirty-tree policy included)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

spec_implement_git = pytest.importorskip("spec_implement_git")  # parallel track (T1-CORE)

from spec_implement_git import (  # noqa: E402
    branch_exists,
    changed_files,
    changed_files_matching,
    commit_paths,
    create_branch,
    current_branch,
    git,
    head_sha,
    is_dirty,
    list_files,
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
    """Dirty-tree policy (§16): untracked `.shepherd` files are excepted."""

    def test_clean_tree(self, tmp_repo: Path) -> None:
        assert is_dirty(tmp_repo) is False

    def test_untracked_under_shepherd_is_clean(self, tmp_repo: Path) -> None:
        shepherd = tmp_repo / ".shepherd" / "features" / "x"
        shepherd.mkdir(parents=True)
        (shepherd / "task.md").write_text("untracked shepherd file\n")
        (tmp_repo / ".shepherd" / "config.yaml").write_text("test: {}\n")

        assert is_dirty(tmp_repo) is False

    def test_untracked_elsewhere_is_dirty(self, tmp_repo: Path) -> None:
        (tmp_repo / "stray.txt").write_text("untracked\n")
        assert is_dirty(tmp_repo) is True

    def test_modified_tracked_file_is_dirty(self, tmp_repo: Path) -> None:
        readme = tmp_repo / "README.md"
        readme.write_text(readme.read_text() + "\nlocal edit\n")
        assert is_dirty(tmp_repo) is True

    def test_modified_tracked_file_under_shepherd_still_counts(
        self, tmp_repo: Path
    ) -> None:
        # The exception is for UNTRACKED .shepherd files only; a tracked,
        # modified file under .shepherd must count as dirty.
        cfg = tmp_repo / ".shepherd" / "config.yaml"
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
        assert branch_exists(tmp_repo, "spec-implement/user-auth") is False
        create_branch(tmp_repo, "spec-implement/user-auth")
        assert branch_exists(tmp_repo, "spec-implement/user-auth") is True

    def test_exists_false_for_unknown(self, tmp_repo: Path) -> None:
        assert branch_exists(tmp_repo, "spec-implement/nope") is False


class TestCommitPaths:
    def test_commits_only_given_paths(self, tmp_repo: Path) -> None:
        readme = tmp_repo / "README.md"
        app = tmp_repo / "src" / "app.py"
        readme.write_text(readme.read_text() + "\nchange one\n")
        app.write_text(app.read_text() + "\n# change two\n")

        commit_paths(tmp_repo, "spec-implement(user-auth): red — failing tests", ["README.md"])

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
        assert show.splitlines()[0] == "spec-implement(user-auth): red — failing tests"
        assert "README.md" in show
        assert "src/app.py" not in show

    def test_commits_new_files(self, tmp_repo: Path) -> None:
        new = tmp_repo / "tests" / "test_new.py"
        new.write_text("def test_new(): pass\n")

        commit_paths(tmp_repo, "spec-implement(user-auth): red — failing tests", ["tests/test_new.py"])

        assert "tests/test_new.py" not in _status(tmp_repo)
        assert is_dirty(tmp_repo) is False


class TestChangedFiles:
    def test_lists_modified_added_and_untracked(self, tmp_repo: Path) -> None:
        (tmp_repo / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
        (tmp_repo / "tests" / "test_new.py").write_text("def test_new(): pass\n")

        changed = changed_files(tmp_repo)

        # The leading porcelain status column (" M") must not eat the path's
        # first character — that was a real bug.
        assert "src/app.py" in changed
        assert "tests/test_new.py" in changed
        assert all(not p.startswith(" ") for p in changed)

    def test_classifier_filters_to_test_files(self, tmp_repo: Path) -> None:
        (tmp_repo / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
        (tmp_repo / "tests" / "test_new.py").write_text("def test_new(): pass\n")

        matched = changed_files_matching(tmp_repo, ["tests"])

        assert matched == ["tests/test_new.py"]

    def test_classifier_handles_colocated_glob(self, tmp_repo: Path) -> None:
        (tmp_repo / "pkg").mkdir()
        (tmp_repo / "pkg" / "svc.go").write_text("package pkg\n")
        (tmp_repo / "pkg" / "svc_test.go").write_text("package pkg\n")

        matched = changed_files_matching(tmp_repo, ["**/*_test.go"])

        assert matched == ["pkg/svc_test.go"]


class TestListFiles:
    def test_includes_tracked_and_untracked_excludes_ignored(self, tmp_repo: Path) -> None:
        (tmp_repo / ".gitignore").write_text("ignored.txt\n")
        (tmp_repo / "ignored.txt").write_text("nope\n")
        (tmp_repo / "tests" / "test_new.py").write_text("def test_new(): pass\n")

        files = list_files(tmp_repo)

        assert "src/app.py" in files            # tracked
        assert "tests/test_new.py" in files      # untracked, not ignored
        assert "ignored.txt" not in files        # gitignored
