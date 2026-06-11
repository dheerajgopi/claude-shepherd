"""Traceability matrix persistence, parsing, and validation (§9, §10).

The matrix (scenario → test mapping with revisions) is the committed audit
artifact at .tdd/traceability.json. This module loads/saves it atomically,
parses the verifier model's (possibly noisy) JSON output against the pinned
schema shape, and answers the two questions the loops ask: is every scenario
covered, and do the mapped tests still exist on disk?
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from tdd_contracts import (
    COVERAGE_COVERED,
    COVERAGE_STATUSES,
    TRACE_FILE,
    ScenarioTrace,
    TraceabilityMatrix,
    TraceRevision,
    asdict_state,
)
from tdd_state import utc_now_iso

_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _filtered_kwargs(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of dataclass `cls` (unknown keys ignored)."""

    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in raw.items() if k in names}


def load_matrix(feature_dir: Path) -> Optional[TraceabilityMatrix]:
    """Read .tdd/traceability.json; None if absent, ValueError if corrupt."""

    path = feature_dir / TRACE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scenarios = [
            ScenarioTrace(**_filtered_kwargs(ScenarioTrace, s))
            for s in data.get("scenarios", [])
        ]
        revisions = [
            TraceRevision(**_filtered_kwargs(TraceRevision, r))
            for r in data.get("revisions", [])
        ]
        kwargs = _filtered_kwargs(TraceabilityMatrix, data)
        kwargs["scenarios"] = scenarios
        kwargs["revisions"] = revisions
        return TraceabilityMatrix(**kwargs)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"traceability matrix corrupt: {path}: {exc}") from exc


def save_matrix(feature_dir: Path, matrix: TraceabilityMatrix) -> None:
    """Atomically write .tdd/traceability.json (tmp file + os.replace)."""

    path = feature_dir / TRACE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict_state(matrix), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _extract_first_json_object(text: str) -> str:
    """First JSON object in noisy model text: ```json fence or balanced braces."""

    fence = _JSON_FENCE_RE.search(text)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in verifier output")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced braces: no complete JSON object in verifier output")


def parse_verifier_matrix(text: str) -> list[ScenarioTrace]:
    """Parse the verifier model's coverage output into ScenarioTrace entries.

    Extracts the FIRST JSON object from possibly-noisy text and validates it
    manually against the VERIFIER_MATRIX_JSON_SCHEMA shape (no jsonschema
    dependency). Raises ValueError with specifics on any violation. Parsed
    scenarios carry revision=0; Loop 2 merges revisions when re-syncing.
    """

    raw = _extract_first_json_object(text)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"verifier output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"verifier output must be a JSON object, got {type(data).__name__}")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("verifier output missing required 'scenarios' array")

    errors: list[str] = []
    traces: list[ScenarioTrace] = []
    for idx, item in enumerate(scenarios):
        where = f"scenarios[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: not an object")
            continue
        missing = [
            k for k in ("scenario_id", "feature_file", "tests", "status") if k not in item
        ]
        if missing:
            errors.append(f"{where}: missing required key(s) {missing}")
            continue
        if not isinstance(item["scenario_id"], str):
            errors.append(f"{where}: scenario_id must be a string")
            continue
        if not isinstance(item["feature_file"], str):
            errors.append(f"{where}: feature_file must be a string")
            continue
        tests = item["tests"]
        if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
            errors.append(f"{where}: tests must be an array of strings")
            continue
        if item["status"] not in COVERAGE_STATUSES:
            errors.append(
                f"{where}: status {item['status']!r} not one of {list(COVERAGE_STATUSES)}"
            )
            continue
        notes = item.get("notes")
        if notes is not None and not isinstance(notes, str):
            errors.append(f"{where}: notes must be a string when present")
            continue
        traces.append(
            ScenarioTrace(
                scenario_id=item["scenario_id"],
                feature_file=item["feature_file"],
                revision=0,
                tests=list(tests),
                status=item["status"],
                notes=notes,
            )
        )
    if errors:
        raise ValueError("verifier matrix shape invalid:\n" + "\n".join(errors))
    return traces


def matrix_fully_covered(matrix: TraceabilityMatrix) -> bool:
    """True if every scenario is status=covered with at least one mapped test.

    An empty matrix is NOT fully covered — coverage cannot be faked by
    reporting no scenarios.
    """

    if not matrix.scenarios:
        return False
    return all(
        s.status == COVERAGE_COVERED and len(s.tests) >= 1 for s in matrix.scenarios
    )


def matrix_validates(repo_root: Path, matrix: TraceabilityMatrix) -> tuple[bool, str]:
    """Check that every mapped test still exists (§10 completion gate).

    For each `path::test_function` reference: the file must exist under
    `repo_root` AND the test function name must appear in the file (plain
    string search). Returns (ok, detail) — detail lists every failure.
    """

    problems: list[str] = []
    for scenario in matrix.scenarios:
        for ref in scenario.tests:
            if "::" not in ref:
                problems.append(
                    f"{scenario.scenario_id}: malformed test reference {ref!r} "
                    "(expected path::test_function)"
                )
                continue
            file_part, *qualifiers = ref.split("::")
            test_file = repo_root / file_part
            if not test_file.is_file():
                problems.append(
                    f"{scenario.scenario_id}: test file missing: {file_part}"
                )
                continue
            func_name = qualifiers[-1]
            if func_name not in test_file.read_text(encoding="utf-8", errors="replace"):
                problems.append(
                    f"{scenario.scenario_id}: test {func_name!r} not found in {file_part}"
                )
    if problems:
        return False, "\n".join(problems)
    return True, "all mapped tests present"


def bump_revisions(
    matrix: TraceabilityMatrix,
    scenario_ids: list[str],
    kind: str,
    description: str,
) -> None:
    """Bump revision on matching scenarios and append an audit TraceRevision.

    `kind` is one of "auto_applied_minor" | "escalation_approved" | "resync"
    (see TraceRevision). Mutates `matrix` in place.
    """

    targets = set(scenario_ids)
    for scenario in matrix.scenarios:
        if scenario.scenario_id in targets:
            scenario.revision += 1
    matrix.revisions.append(
        TraceRevision(
            timestamp=utc_now_iso(),
            kind=kind,
            scenario_ids=list(scenario_ids),
            description=description,
        )
    )


def gap_report(matrix: TraceabilityMatrix) -> str:
    """Markdown gap report: scenarios that are partial/missing (or testless).

    Written to .tdd/reports/ when Loop 2 exhausts its coverage iterations
    (exit COVERAGE_GAP). Covered-but-testless scenarios are included because
    they also block completion (matrix_fully_covered rejects them).
    """

    gaps = [
        s
        for s in matrix.scenarios
        if s.status != COVERAGE_COVERED or not s.tests
    ]
    lines = [f"# Coverage gap report — {matrix.slug}", ""]
    if not gaps:
        lines.append("All scenarios are covered with at least one test.")
        return "\n".join(lines) + "\n"
    lines.append(f"{len(gaps)} of {len(matrix.scenarios)} scenario(s) not fully covered:")
    lines.append("")
    for scenario in gaps:
        lines.append(f"## {scenario.scenario_id} — {scenario.status}")
        lines.append(f"- feature file: `{scenario.feature_file}`")
        if scenario.tests:
            lines.append("- mapped tests: " + ", ".join(f"`{t}`" for t in scenario.tests))
        else:
            lines.append("- mapped tests: (none)")
        if scenario.notes:
            lines.append(f"- notes: {scenario.notes}")
        lines.append("")
    return "\n".join(lines)
