"""Windows-host → WSL-filesystem subprocess routing (stdlib only).

When the engine's Python runs on a Windows host but the target repo lives in
the WSL filesystem, the repo path is a UNC path of the form::

    \\\\wsl.localhost\\<distro>\\<linux path...>   (Windows 11)
    \\\\wsl$\\<distro>\\<linux path...>            (older builds)

Windows cannot use a UNC path as a process working directory, and CMD does not
carry WSL's PATH (so ``uv``/``pytest`` are "not found"). Every engine
subprocess that runs in the repo (the test command, the ``py_compile`` syntax
gate, git) therefore fails — burning checkpoint turns. These helpers detect
that exact situation and rewrite the call to run inside WSL via
``wsl.exe -d <distro>``.

The detection is pure and platform-parameterised (``platform`` defaults to
``sys.platform``; tests override it), so the Windows-only behaviour is fully
exercised from any host. On every other platform/path the helpers report "not
applicable" and callers run the command natively, exactly as before — config
stays portable (no machine-specific paths are baked anywhere).
"""

from __future__ import annotations

import shlex
import sys
from typing import Optional

#: UNC prefixes Windows uses to expose the WSL filesystem.
_UNC_PREFIXES = ("\\\\wsl.localhost\\", "\\\\wsl$\\")


def wsl_target(repo_root, platform: Optional[str] = None) -> Optional[tuple[str, str]]:
    """``(distro, linux_path)`` when ``repo_root`` is a WSL-UNC path on a
    Windows host, else ``None`` (meaning "run natively, unchanged").

    Pure — parses the string form of ``repo_root`` and never touches the
    filesystem, so it behaves identically on any OS for a given
    ``(repo_root, platform)`` pair. ``platform`` defaults to ``sys.platform``.
    """

    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return None
    raw = str(repo_root)
    for prefix in _UNC_PREFIXES:
        if raw.startswith(prefix):
            rest = raw[len(prefix):]
            break
    else:
        return None
    # The distro name is the first segment; everything after it is the Linux
    # path. Normalise either separator and drop empties from a trailing slash.
    segments = [s for s in rest.replace("/", "\\").split("\\") if s]
    if not segments:
        return None
    distro, *tail = segments
    linux_path = "/" + "/".join(tail)
    return distro, linux_path


def to_unc(linux_path: str, reference_unc) -> Optional[str]:
    """Map an absolute ``linux_path`` to the Windows UNC path for it, reusing
    the host + distro of ``reference_unc`` (an existing WSL-UNC path).

    The inverse of the ``linux_path`` ``wsl_target`` extracts. Used to keep a
    repo root expressed as UNC after asking WSL-side git for its toplevel (git
    answers with a Linux path, which would otherwise mis-resolve on Windows
    and defeat detection downstream). Returns ``None`` if ``reference_unc`` is
    not a WSL-UNC path.
    """

    raw = str(reference_unc)
    for prefix in _UNC_PREFIXES:
        if raw.startswith(prefix):
            distro = raw[len(prefix):].replace("/", "\\").split("\\")[0]
            return prefix + distro + linux_path.replace("/", "\\")
    return None


def shell_argv(command: str, distro: str, linux_path: str) -> list[str]:
    """argv running a shell *command line* inside WSL with ``linux_path`` as
    cwd, through a login shell so the user's PATH (uv, pytest, …) is present.

    Used for the configured test command — an opaque shell string the engine
    must not parse.
    """

    inner = f"cd {shlex.quote(linux_path)} && {command}"
    return ["wsl.exe", "-d", distro, "--", "bash", "-lc", inner]


def exec_argv(distro: str, inner_argv: list[str]) -> list[str]:
    """argv running an explicit ``inner_argv`` inside WSL through a login shell.
    Used for git (``git -C <linux_path> …``) and the ``py_compile`` syntax
    gate, which carry their working directory as an argument rather than via
    cwd.

    The login shell (``bash -lc``) is essential, not cosmetic: ``wsl.exe -- git``
    runs with the bare interop PATH (default dirs + appended Windows ``Path``),
    so a Windows ``git``/``python`` shadows the WSL one and mangles ``/home/…``
    into ``C:\\…\\home\\…``. Sourcing the profile puts WSL's own binaries first.
    Each token is ``shlex.quote``d before being joined into the command line.
    """

    inner = " ".join(shlex.quote(token) for token in inner_argv)
    return ["wsl.exe", "-d", distro, "--", "bash", "-lc", inner]
