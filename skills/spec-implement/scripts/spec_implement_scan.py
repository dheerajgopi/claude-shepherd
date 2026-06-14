"""Deterministic convention scan for the spec-implement shepherd (§6 step 3, §9).

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

from spec_implement_contracts import matches_any_pattern

#: Conventional test directories probed when no config declares paths.
_DEFAULT_TEST_DIRS = ("tests", "test", "spec", "src/__tests__")

#: The test-file classifier (test.paths globs) per detected framework, used
#: when the project declares no explicit test paths. Folder-based for languages
#: that separate tests into a directory (pytest, JVM); filename-glob for those
#: that co-locate tests with source (Go's `_test.go`, JS/TS suffix tests).
#: Rust unit tests live INSIDE source files (`#[cfg(test)]`) and cannot be
#: classified by path — only integration tests under `tests/` are separable.
_FRAMEWORK_TEST_GLOBS = {
    "go": ["**/*_test.go"],
    "cargo": ["tests/**"],
    "maven": ["src/test/**"],
    "gradle": ["src/test/**"],
    "jest": ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"],
    "vitest": ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"],
    "mocha": ["**/*.test.*", "**/*.spec.*", "**/test/**"],
}

#: Directories never worth walking for an exemplar test file.
_EXEMPLAR_SKIP_DIRS = frozenset(
    {".git", ".shepherd", "node_modules", "target", "build", "dist", "vendor", ".venv"}
)

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

#: Repo-root convention docs the engine reads itself and surfaces to the agent.
#: Inner SDK sessions run with `setting_sources` unset (no CLAUDE.md auto-load,
#: full isolation — docs/sdk-notes.md §7), so rather than re-enable that bundle
#: (which would also pull in project settings/hooks and miss AGENTS.md entirely),
#: the engine reads these deterministically and injects them via the scan.
_CONVENTION_DOC_FILES = ("CLAUDE.md", "AGENTS.md")

#: Per-doc content cap when surfacing convention docs into the prompt.
_CONVENTION_DOC_CAP = 8_000


@dataclass
class ConventionScan:
    """Result of the deterministic convention scan."""

    test_command: Optional[str] = None
    test_paths: list[str] = field(default_factory=list)
    framework: Optional[str] = None
    exemplar_test: Optional[str] = None
    convention_docs: list[tuple[str, str]] = field(default_factory=list)
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
    """First test file (deterministic order) the classifier accepts.

    Walks test-named files anywhere under `repo_root` (pruning vendored dirs)
    and returns the first whose repo-relative path matches the `test_paths`
    classifier — so it works for directory-based and co-located (glob) layouts
    alike.
    """

    best: Optional[str] = None
    for pattern in _TEST_FILE_PATTERNS:
        for path in repo_root.rglob(pattern):
            parts = path.relative_to(repo_root).parts
            if any(seg in _EXEMPLAR_SKIP_DIRS for seg in parts):
                continue
            rel = "/".join(parts)
            if matches_any_pattern(rel, test_paths) and (best is None or rel < best):
                best = rel
    return best


def read_convention_docs(repo_root: Path) -> list[tuple[str, str]]:
    """Read repo-root convention docs (CLAUDE.md, AGENTS.md), capped.

    Deterministic order (`_CONVENTION_DOC_FILES`); each present file's content is
    truncated to `_CONVENTION_DOC_CAP`. Returns `(filename, content)` pairs so a
    loop can inject authored repo conventions into the agent's prompt — the
    engine reads them itself because inner sessions are fully isolated.
    """

    docs: list[tuple[str, str]] = []
    for name in _CONVENTION_DOC_FILES:
        path = repo_root / name
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            docs.append((name, content[:_CONVENTION_DOC_CAP]))
    return docs


def format_convention_docs(docs: list[tuple[str, str]]) -> str:
    """Render convention docs as a prompt section, or "" when there are none.

    These are human-authored repo conventions; the rendered block instructs the
    agent to honor them above inferred defaults (e.g. test directory layout).
    """

    if not docs:
        return ""
    parts = [
        "Repo convention docs the maintainers authored. Honor any conventions "
        "they state (test layout, naming, style) above inferred defaults; they "
        "do not, however, override the mechanical path-policy hook.",
    ]
    for name, content in docs:
        parts.append(f"### {name}\n\n```\n{content}\n```")
    return "\n\n".join(parts)


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

    if not scan.test_paths and scan.framework in _FRAMEWORK_TEST_GLOBS:
        scan.test_paths = list(_FRAMEWORK_TEST_GLOBS[scan.framework])
        scan.notes.append(
            f"test classifier from {scan.framework} convention: {scan.test_paths}"
        )
        if scan.framework == "cargo":
            scan.notes.append(
                "Rust unit tests live inside source files (#[cfg(test)]) and "
                "cannot be path-classified; only integration tests under tests/ "
                "are mechanically separable"
            )
    if not scan.test_paths:
        found = [d for d in _DEFAULT_TEST_DIRS if (repo_root / d).is_dir()]
        if found:
            scan.test_paths = found
            scan.notes.append(f"test paths from existing conventional dirs: {found}")
        else:
            scan.notes.append(
                "no test paths found; set test.paths in config.yaml manually"
            )
    # Canonical form: no trailing slash (the classifier strips it anyway, but
    # keep config tidy). Glob patterns like `**/*_test.go` are unaffected.
    scan.test_paths = [p.rstrip("/") for p in scan.test_paths]

    scan.exemplar_test = _find_exemplar(repo_root, scan.test_paths)
    if scan.exemplar_test is not None:
        scan.notes.append(f"exemplar test file: {scan.exemplar_test}")
    else:
        scan.notes.append("no exemplar test file found under detected paths")

    scan.convention_docs = read_convention_docs(repo_root)
    for name, content in scan.convention_docs:
        scan.notes.append(f"convention doc surfaced to agent: {name} ({len(content)} chars)")
    return scan
