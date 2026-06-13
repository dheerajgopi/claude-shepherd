"""Pure-function tests for tdd_wsl — the Windows-host → WSL routing decision.

The behaviour is Windows-only in production, but every function is platform
parameterised, so the win32 paths are exercised from any host.
"""

from __future__ import annotations

import pytest

tdd_wsl = pytest.importorskip("tdd_wsl")

from tdd_wsl import exec_argv, shell_argv, to_unc, wsl_target  # noqa: E402

UNC = r"\\wsl.localhost\Ubuntu\home\dev\users-api"
UNC_LEGACY = r"\\wsl$\Debian\srv\app"


class TestWslTarget:
    def test_non_windows_never_routes(self) -> None:
        # A UNC-looking path on Linux/mac still runs natively.
        assert wsl_target(UNC, platform="linux") is None
        assert wsl_target(UNC, platform="darwin") is None

    def test_windows_unc_localhost(self) -> None:
        assert wsl_target(UNC, platform="win32") == ("Ubuntu", "/home/dev/users-api")

    def test_windows_unc_legacy_wsl_dollar(self) -> None:
        assert wsl_target(UNC_LEGACY, platform="win32") == ("Debian", "/srv/app")

    def test_windows_native_drive_path_runs_natively(self) -> None:
        assert wsl_target(r"C:\Users\dev\proj", platform="win32") is None

    def test_distro_root_only(self) -> None:
        assert wsl_target(r"\\wsl.localhost\Ubuntu", platform="win32") == ("Ubuntu", "/")

    def test_trailing_separator_dropped(self) -> None:
        assert wsl_target(UNC + "\\", platform="win32") == (
            "Ubuntu",
            "/home/dev/users-api",
        )

    def test_forward_slashes_normalised(self) -> None:
        assert wsl_target(
            "//wsl.localhost/Ubuntu/home/dev/users-api", platform="win32"
        ) is None  # only backslash prefixes are WSL-UNC; forward-slash form isn't


class TestToUnc:
    def test_round_trips_localhost(self) -> None:
        distro, linux_path = wsl_target(UNC, platform="win32")
        assert to_unc(linux_path, UNC) == UNC

    def test_round_trips_legacy(self) -> None:
        _, linux_path = wsl_target(UNC_LEGACY, platform="win32")
        assert to_unc(linux_path, UNC_LEGACY) == UNC_LEGACY

    def test_preserves_host_and_distro_for_subpath(self) -> None:
        # A toplevel that differs from the reference still reuses host+distro.
        assert to_unc("/home/dev/other", UNC) == r"\\wsl.localhost\Ubuntu\home\dev\other"

    def test_non_unc_reference_returns_none(self) -> None:
        assert to_unc("/home/dev/x", r"C:\Users\dev") is None


class TestArgvBuilders:
    def test_shell_argv_uses_login_shell_and_cd(self) -> None:
        argv = shell_argv("uv run pytest", "Ubuntu", "/home/dev/users-api")
        assert argv == [
            "wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc",
            "cd /home/dev/users-api && uv run pytest",
        ]

    def test_shell_argv_quotes_paths_with_spaces(self) -> None:
        argv = shell_argv("pytest", "Ubuntu", "/home/dev/my project")
        assert argv[-1] == "cd '/home/dev/my project' && pytest"

    def test_exec_argv_uses_login_shell(self) -> None:
        # The login shell is what puts WSL's git/python ahead of the Windows
        # ones on PATH; a bare `wsl.exe -- git` picks up C:\...\git and mangles
        # Linux paths.
        assert exec_argv("Ubuntu", ["git", "-C", "/home/dev/x", "status"]) == [
            "wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc",
            "git -C /home/dev/x status",
        ]

    def test_exec_argv_quotes_paths_with_spaces(self) -> None:
        argv = exec_argv("Ubuntu", ["python3", "-m", "py_compile", "/home/dev/my project/t.py"])
        assert argv[-1] == "python3 -m py_compile '/home/dev/my project/t.py'"
