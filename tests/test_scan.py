"""Tests for tdd_scan — deterministic convention pre-scan (§9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

tdd_scan = pytest.importorskip("tdd_scan")  # parallel track (T1-CORE)

from tdd_scan import scan_conventions  # noqa: E402


@pytest.fixture
def pytest_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n"
    )
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    return repo


@pytest.fixture
def jest_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "jsrepo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "^29.0.0"},
            },
            indent=2,
        )
    )
    (repo / "__tests__").mkdir()
    (repo / "__tests__" / "example.test.js").write_text(
        "test('adds', () => { expect(1 + 1).toBe(2); });\n"
    )
    return repo


class TestPytestDetection:
    def test_command_and_paths(self, pytest_repo: Path) -> None:
        scan = scan_conventions(pytest_repo)
        assert scan.test_command is not None
        assert "pytest" in scan.test_command
        assert list(scan.test_paths) == ["tests"]

    def test_framework_identified(self, pytest_repo: Path) -> None:
        scan = scan_conventions(pytest_repo)
        assert scan.framework
        assert "pytest" in scan.framework.lower()

    def test_exemplar_found(self, pytest_repo: Path) -> None:
        scan = scan_conventions(pytest_repo)
        assert scan.exemplar_test
        assert "test_example.py" in str(scan.exemplar_test)


class TestJestDetection:
    def test_jest_detected(self, jest_repo: Path) -> None:
        scan = scan_conventions(jest_repo)
        assert scan.test_command is not None
        blob = " ".join(
            str(part) for part in (scan.test_command, scan.framework) if part
        ).lower()
        assert "jest" in blob or "npm" in blob


class TestBareRepo:
    def test_nothing_detected_with_note(self, tmp_path: Path) -> None:
        repo = tmp_path / "bare"
        repo.mkdir()
        (repo / "data.txt").write_text("nothing testable here\n")

        scan = scan_conventions(repo)

        assert scan.test_command is None
        assert scan.notes  # a note explaining that detection found nothing
