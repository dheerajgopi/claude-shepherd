"""Thin git subprocess helpers for the TDD harness (stdlib only).

Every helper shells out to `git` and converts failures into
HarnessError(INTERNAL_ERROR) carrying git's stderr, so the CLI surfaces them
verbatim. The dirty-tree rule implements requirement §16: untracked files
under .harness/ are excepted; any other change counts as dirty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from tdd_contracts import HARNESS_DIR, ExitCode
from tdd_state import HarnessError


def git(args: list[str], cwd: Path) -> str:
    """Run `git <args>` in `cwd`; return stripped stdout.

    Raises HarnessError(INTERNAL_ERROR) with git's stderr on any failure.
    """

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HarnessError(
            ExitCode.INTERNAL_ERROR, "git executable not found on PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise HarnessError(
            ExitCode.INTERNAL_ERROR,
            f"git {' '.join(args)} failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip()}",
        ) from exc
    return proc.stdout.strip()


def repo_root(cwd: Path) -> Optional[Path]:
    """Toplevel of the git repo containing `cwd`, or None if not in a repo."""

    try:
        return Path(git(["rev-parse", "--show-toplevel"], cwd)).resolve()
    except HarnessError:
        return None


def current_branch(repo: Path) -> str:
    """Name of the current branch ('HEAD' when detached)."""

    return git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def head_sha(repo: Path) -> str:
    """Full SHA of HEAD."""

    return git(["rev-parse", "HEAD"], repo)


def is_dirty(repo: Path, excepted_paths: tuple[str, ...] = ()) -> bool:
    """True if the working tree has changes, per the §16 dirty-tree rule.

    Untracked files under .harness/ are ignored; modified tracked files
    anywhere (including under .harness/) count as dirty. `excepted_paths`
    names exact extra paths to ignore in any status (the CLI passes
    ".gitignore", which `init` itself creates or appends to).
    """

    for line in git(["status", "--porcelain"], repo).splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path in excepted_paths:
            continue
        if status == "??" and (
            path == HARNESS_DIR
            or path == f"{HARNESS_DIR}/"
            or path.startswith(f"{HARNESS_DIR}/")
        ):
            continue
        return True
    return False


def create_branch(repo: Path, name: str) -> None:
    """Create and switch to branch `name` (git switch -c)."""

    git(["switch", "-c", name], repo)


def branch_exists(repo: Path, name: str) -> bool:
    """True if a local branch named `name` exists."""

    try:
        git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo)
        return True
    except HarnessError:
        return False


def commit_paths(repo: Path, message: str, paths: list[str]) -> None:
    """Stage and commit ONLY the given paths with `message`.

    `git add -A -- <paths>` picks up additions/edits/deletions under the
    paths; the pathspec on `git commit` guarantees nothing else staged by
    accident is swept into the commit.
    """

    git(["add", "-A", "--", *paths], repo)
    git(["commit", "-m", message, "--", *paths], repo)
