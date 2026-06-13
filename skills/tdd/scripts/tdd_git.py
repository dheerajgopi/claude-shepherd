"""Thin git subprocess helpers for the TDD shepherd (stdlib only).

Every helper shells out to `git` and converts failures into
ShepherdError(INTERNAL_ERROR) carrying git's stderr, so the CLI surfaces them
verbatim. The dirty-tree rule implements requirement §16: untracked files
under .shepherd/ are excepted; any other change counts as dirty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import tdd_wsl
from tdd_contracts import SHEPHERD_DIR, ExitCode, matches_any_pattern
from tdd_state import ShepherdError


def git(args: list[str], cwd: Path, strip: bool = True) -> str:
    """Run `git <args>` in `cwd`; return stdout (stripped unless `strip=False`).

    `strip=False` is required for `status --porcelain`, whose leading status
    column is a space (` M path`) that a global strip would eat off the first
    line, corrupting its path. Raises ShepherdError(INTERNAL_ERROR) with git's
    stderr on any failure.

    When `cwd` is a WSL-filesystem UNC path on a Windows host, git runs inside
    WSL (`git -C <linux_path>`) so the repo is touched by its own git config —
    avoiding Windows git's dubious-ownership / line-ending surprises on a Linux
    checkout (see tdd_wsl).
    """

    target = tdd_wsl.wsl_target(cwd)
    if target is None:
        argv, run_cwd = ["git", *args], cwd
    else:
        distro, linux_path = target
        argv, run_cwd = tdd_wsl.exec_argv(distro, ["git", "-C", linux_path, *args]), None
    try:
        proc = subprocess.run(
            argv,
            cwd=run_cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR, "git executable not found on PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ShepherdError(
            ExitCode.INTERNAL_ERROR,
            f"git {' '.join(args)} failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip()}",
        ) from exc
    return proc.stdout.strip() if strip else proc.stdout


def repo_root(cwd: Path) -> Optional[Path]:
    """Toplevel of the git repo containing `cwd`, or None if not in a repo.

    On a Windows host driving a WSL repo, WSL-side git answers with a Linux
    path; it is re-expressed as a UNC path so it stays usable on Windows and
    keeps tripping WSL detection on later calls.
    """

    try:
        toplevel = git(["rev-parse", "--show-toplevel"], cwd)
    except ShepherdError:
        return None
    if tdd_wsl.wsl_target(cwd) is not None:
        unc = tdd_wsl.to_unc(toplevel, cwd)
        if unc is not None:
            return Path(unc)
    return Path(toplevel).resolve()


def current_branch(repo: Path) -> str:
    """Name of the current branch ('HEAD' when detached)."""

    return git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def head_sha(repo: Path) -> str:
    """Full SHA of HEAD."""

    return git(["rev-parse", "HEAD"], repo)


def is_dirty(repo: Path, excepted_paths: tuple[str, ...] = ()) -> bool:
    """True if the working tree has changes, per the §16 dirty-tree rule.

    Untracked files under .shepherd/ are ignored; modified tracked files
    anywhere (including under .shepherd/) count as dirty. `excepted_paths`
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
            path == SHEPHERD_DIR
            or path == f"{SHEPHERD_DIR}/"
            or path.startswith(f"{SHEPHERD_DIR}/")
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
    except ShepherdError:
        return False


def _unquote_status_path(path: str) -> str:
    """Strip git's C-style quoting from a porcelain/ls-files path (minimal).

    Only surrounding quotes are removed (matching `is_dirty`); the unescaped
    inner bytes are good enough for glob classification of ASCII paths.
    """

    if path.startswith('"') and path.endswith('"'):
        return path[1:-1]
    return path


def changed_files(repo: Path) -> list[str]:
    """Repo-relative posix paths of every changed entry (`git status --porcelain`).

    Modified, added, renamed (the new name), and untracked-not-ignored files —
    so `.shepherd/` (gitignored) never appears. `-u` expands untracked
    directories to individual files (porcelain otherwise collapses a new dir to
    `pkg/`, hiding the test files inside it). The caller filters these through
    the test classifier to decide what a red commit stages.
    """

    out: list[str] = []
    for line in git(
        ["status", "--porcelain", "-u"], repo, strip=False
    ).splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:               # rename/copy: keep the destination
            path = path.split(" -> ", 1)[1]
        out.append(_unquote_status_path(path))
    return out


def changed_files_matching(repo: Path, patterns: list[str]) -> list[str]:
    """Changed files whose repo-relative path matches the classifier `patterns`.

    What a red / red(n) commit stages: the writable set in Loop 2 (ALLOW_ONLY
    the classifier) and the committable set are then identical, so the red
    anchor stays self-contained even for co-located or scaffolding tests.
    """

    return [p for p in changed_files(repo) if matches_any_pattern(p, patterns)]


def list_files(repo: Path) -> list[str]:
    """Repo-relative posix paths of tracked + untracked-not-ignored files.

    `git ls-files --cached --others --exclude-standard` — the on-disk file set
    the test classifier scans to assemble the verifier's view of the tests.
    """

    out = git(["ls-files", "--cached", "--others", "--exclude-standard"], repo)
    return [_unquote_status_path(line) for line in out.splitlines() if line.strip()]


def commit_paths(repo: Path, message: str, paths: list[str]) -> None:
    """Stage and commit ONLY the given paths with `message`.

    `git add -A -- <paths>` picks up additions/edits/deletions under the
    paths; the pathspec on `git commit` guarantees nothing else staged by
    accident is swept into the commit.
    """

    git(["add", "-A", "--", *paths], repo)
    git(["commit", "-m", message, "--", *paths], repo)
