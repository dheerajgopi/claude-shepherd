"""Deterministic convention scan for the TDD shepherd (§6 step 3, §9).

Pure-Python file inspection — no agent calls, no guessing. Detects the test
command, framework, test paths, and an exemplar test file from well-known
project markers, in a fixed priority order, and records every finding (and
every blank) in `notes`. When nothing is detected the corresponding field is
left None/empty and noted; a wrong boundary would undermine the Loop 2/3
enforcement hooks, so init prints the result with a review instruction.
"""

from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Conventional test directories probed when no config declares paths.
_DEFAULT_TEST_DIRS = ("tests", "test", "spec", "src/__tests__")

#: Filename globs that identify a test file, across supported ecosystems.
_TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.js",
    "*_spec.rb",
    "*Test.java",
    "*Tests.java",
)

#: JS test frameworks recognized in package.json dependencies.
_JS_FRAMEWORKS = ("jest", "vitest", "mocha")


@dataclass
class ConventionScan:
    """Result of the deterministic convention scan."""

    test_command: Optional[str] = None
    test_paths: list[str] = field(default_factory=list)
    framework: Optional[str] = None
    exemplar_test: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _as_path_list(value: object) -> list[str]:
    """Normalize a testpaths-style value (string or list) to a list of strings."""

    if isinstance(value, str):
        return [p for p in re.split(r"[,\s]+", value) if p]
    if isinstance(value, list):
        return [str(p) for p in value]
    return []


def _detect_pyproject(repo_root: Path, scan: ConventionScan) -> bool:
    """pyproject.toml with [tool.pytest.ini_options] → pytest (+ testpaths)."""

    path = repo_root / "pyproject.toml"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    testpaths: list[str] = []
    try:
        import tomllib

        data = tomllib.loads(text)
        ini = data.get("tool", {}).get("pytest", {}).get("ini_options")
        if ini is None:
            return False
        testpaths = _as_path_list(ini.get("testpaths"))
    except ImportError:  # Python 3.10: fall back to a textual check
        if "[tool.pytest.ini_options]" not in text:
            return False
        match = re.search(r"testpaths\s*=\s*\[(.*?)\]", text, re.DOTALL)
        if match:
            testpaths = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
    scan.framework = "pytest"
    scan.test_command = "pytest"
    scan.notes.append("pytest detected from [tool.pytest.ini_options] in pyproject.toml")
    if testpaths:
        scan.test_paths = testpaths
        scan.notes.append(f"test paths from pyproject.toml testpaths: {testpaths}")
    return True


def _detect_pytest_ini_family(repo_root: Path, scan: ConventionScan) -> bool:
    """pytest.ini / setup.cfg [tool:pytest] / tox.ini [pytest] → pytest."""

    candidates = (
        ("pytest.ini", "pytest", False),
        ("setup.cfg", "tool:pytest", True),
        ("tox.ini", "pytest", True),
    )
    for filename, section, section_required in candidates:
        path = repo_root / filename
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
        except configparser.Error:
            continue
        if section_required and not parser.has_section(section):
            continue
        scan.framework = "pytest"
        scan.test_command = "pytest"
        scan.notes.append(f"pytest detected from {filename}")
        if parser.has_option(section, "testpaths"):
            testpaths = _as_path_list(parser.get(section, "testpaths"))
            if testpaths:
                scan.test_paths = testpaths
                scan.notes.append(f"test paths from {filename} testpaths: {testpaths}")
        return True
    return False


def _detect_package_json(repo_root: Path, scan: ConventionScan) -> bool:
    """package.json scripts.test → npm test; jest/vitest/mocha in deps → framework."""

    path = repo_root / "package.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        scan.notes.append("package.json present but unparseable; skipped")
        return False
    if not isinstance(data, dict):
        return False
    deps: dict[str, object] = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    for fw in _JS_FRAMEWORKS:
        if fw in deps:
            scan.framework = fw
            scan.notes.append(f"{fw} found in package.json dependencies")
            break
    script = (data.get("scripts") or {}).get("test") if isinstance(data.get("scripts"), dict) else None
    if isinstance(script, str) and script and "no test specified" not in script:
        scan.test_command = "npm test"
        scan.notes.append(f"test command from package.json scripts.test: {script!r}")
        return True
    if scan.framework is not None:
        scan.notes.append(
            "package.json has no usable scripts.test; no command detected from it"
        )
    return False


def _detect_go(repo_root: Path, scan: ConventionScan) -> bool:
    """go.mod → `go test ./...`."""

    if not (repo_root / "go.mod").is_file():
        return False
    scan.framework = "go"
    scan.test_command = "go test ./..."
    scan.notes.append("go module detected from go.mod")
    return True


def _detect_cargo(repo_root: Path, scan: ConventionScan) -> bool:
    """Cargo.toml → `cargo test`."""

    if not (repo_root / "Cargo.toml").is_file():
        return False
    scan.framework = "cargo"
    scan.test_command = "cargo test"
    scan.notes.append("Rust crate detected from Cargo.toml")
    return True


def _detect_jvm(repo_root: Path, scan: ConventionScan) -> bool:
    """pom.xml → `mvn -q test`; build.gradle(.kts) → `gradle test`."""

    if (repo_root / "pom.xml").is_file():
        scan.framework = "maven"
        scan.test_command = "mvn -q test"
        scan.notes.append("Maven project detected from pom.xml")
        return True
    for gradle_file in ("build.gradle", "build.gradle.kts"):
        if (repo_root / gradle_file).is_file():
            scan.framework = "gradle"
            scan.test_command = "gradle test"
            scan.notes.append(f"Gradle project detected from {gradle_file}")
            return True
    return False


def _find_exemplar(repo_root: Path, test_paths: list[str]) -> Optional[str]:
    """First test file (deterministic order) under the detected test paths."""

    for raw in test_paths:
        base = repo_root / raw
        if not base.is_dir():
            continue
        matches: list[Path] = []
        for pattern in _TEST_FILE_PATTERNS:
            matches.extend(base.rglob(pattern))
        if matches:
            best = sorted(matches)[0]
            return best.relative_to(repo_root).as_posix()
    return None


def scan_conventions(repo_root: Path) -> ConventionScan:
    """Scan `repo_root` for test conventions; never guess, always note.

    Detection priority: pyproject.toml, pytest.ini/setup.cfg/tox.ini,
    package.json, go.mod, Cargo.toml, pom.xml/build.gradle. The first
    detector that yields a test command wins; test paths come from config
    keys when declared, otherwise from existing conventional directories.
    """

    scan = ConventionScan()
    detectors = (
        _detect_pyproject,
        _detect_pytest_ini_family,
        _detect_package_json,
        _detect_go,
        _detect_cargo,
        _detect_jvm,
    )
    for detector in detectors:
        if detector(repo_root, scan):
            break
    if scan.test_command is None:
        scan.notes.append(
            "no test command detected; set test.command in config.yaml manually"
        )

    if not scan.test_paths:
        found = [d for d in _DEFAULT_TEST_DIRS if (repo_root / d).is_dir()]
        if found:
            scan.test_paths = found
            scan.notes.append(f"test paths from existing conventional dirs: {found}")
        else:
            scan.notes.append(
                "no test directories found; set test.paths in config.yaml manually"
            )
    # Canonical form: no trailing slash (hooks and config compare via Path).
    scan.test_paths = [p.rstrip("/") for p in scan.test_paths]

    scan.exemplar_test = _find_exemplar(repo_root, scan.test_paths)
    if scan.exemplar_test is not None:
        scan.notes.append(f"exemplar test file: {scan.exemplar_test}")
    else:
        scan.notes.append("no exemplar test file found under detected paths")
    return scan
