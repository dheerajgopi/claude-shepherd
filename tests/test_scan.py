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


class TestColocatedLayouts:
    """Languages where tests are classified by filename, not directory."""

    def test_go_uses_test_suffix_glob(self, tmp_path: Path) -> None:
        repo = tmp_path / "gorepo"
        repo.mkdir()
        (repo / "go.mod").write_text("module demo\n\ngo 1.22\n")
        (repo / "svc.go").write_text("package demo\n")
        (repo / "svc_test.go").write_text(
            "package demo\nfunc TestSvc(t *testing.T) {}\n"
        )

        scan = scan_conventions(repo)

        assert scan.framework == "go"
        assert scan.test_paths == ["**/*_test.go"]
        # The classifier finds the co-located test as an exemplar.
        assert scan.exemplar_test == "svc_test.go"

    def test_jvm_uses_test_tree(self, tmp_path: Path) -> None:
        repo = tmp_path / "javarepo"
        repo.mkdir()
        (repo / "pom.xml").write_text("<project></project>\n")

        scan = scan_conventions(repo)

        assert scan.framework == "maven"
        assert scan.test_paths == ["src/test/**"]

    def test_cargo_notes_unit_test_limit(self, tmp_path: Path) -> None:
        repo = tmp_path / "rustrepo"
        repo.mkdir()
        (repo / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")

        scan = scan_conventions(repo)

        assert scan.framework == "cargo"
        assert scan.test_paths == ["tests/**"]
        assert any("#[cfg(test)]" in note for note in scan.notes)


class TestBareRepo:
    def test_nothing_detected_with_note(self, tmp_path: Path) -> None:
        repo = tmp_path / "bare"
        repo.mkdir()
        (repo / "data.txt").write_text("nothing testable here\n")

        scan = scan_conventions(repo)

        assert scan.test_command is None
        assert scan.notes  # a note explaining that detection found nothing


class TestConventionDocs:
    def test_none_present(self, pytest_repo: Path) -> None:
        scan = scan_conventions(pytest_repo)
        assert scan.convention_docs == []

    def test_claude_md_surfaced(self, pytest_repo: Path) -> None:
        (pytest_repo / "CLAUDE.md").write_text(
            "Put router tests under tests/routers/.\n"
        )
        scan = scan_conventions(pytest_repo)
        names = [name for name, _ in scan.convention_docs]
        assert names == ["CLAUDE.md"]
        assert "tests/routers/" in scan.convention_docs[0][1]
        assert any("CLAUDE.md" in note for note in scan.notes)

    def test_both_docs_in_deterministic_order(self, pytest_repo: Path) -> None:
        (pytest_repo / "AGENTS.md").write_text("agents guidance\n")
        (pytest_repo / "CLAUDE.md").write_text("claude guidance\n")
        scan = scan_conventions(pytest_repo)
        names = [name for name, _ in scan.convention_docs]
        assert names == ["CLAUDE.md", "AGENTS.md"]

    def test_content_capped(self, pytest_repo: Path) -> None:
        from tdd_scan import _CONVENTION_DOC_CAP

        (pytest_repo / "CLAUDE.md").write_text("x" * (_CONVENTION_DOC_CAP + 500))
        scan = scan_conventions(pytest_repo)
        assert len(scan.convention_docs[0][1]) == _CONVENTION_DOC_CAP


class TestFormatConventionDocs:
    def test_empty_is_blank(self) -> None:
        from tdd_scan import format_convention_docs

        assert format_convention_docs([]) == ""

    def test_renders_each_doc(self) -> None:
        from tdd_scan import format_convention_docs

        out = format_convention_docs([("CLAUDE.md", "layout rule")])
        assert "### CLAUDE.md" in out
        assert "layout rule" in out
