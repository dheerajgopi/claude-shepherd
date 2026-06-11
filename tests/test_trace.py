"""Tests for tdd_trace — traceability matrix parse/validate/bump/report (§9-§10)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

tdd_trace = pytest.importorskip("tdd_trace")  # parallel track (T1-CORE)

from tdd_contracts import (  # noqa: E402
    COVERAGE_COVERED,
    COVERAGE_MISSING,
    COVERAGE_PARTIAL,
    ScenarioTrace,
    TraceabilityMatrix,
    asdict_state,
)
from tdd_trace import (  # noqa: E402
    bump_revisions,
    gap_report,
    load_matrix,
    matrix_fully_covered,
    matrix_validates,
    parse_verifier_matrix,
    save_matrix,
)


def _scn(sid: str, status: str, tests=None, revision: int = 1) -> ScenarioTrace:
    return ScenarioTrace(
        scenario_id=sid,
        feature_file="auth.feature",
        revision=revision,
        tests=list(tests or []),
        status=status,
    )


def _matrix(*scenarios: ScenarioTrace) -> TraceabilityMatrix:
    return TraceabilityMatrix(slug="user-auth", scenarios=list(scenarios))


CLEAN_JSON = json.dumps(
    {
        "scenarios": [
            {
                "scenario_id": "auth:Successful login",
                "feature_file": "auth.feature",
                "tests": ["tests/test_auth.py::test_login"],
                "status": "covered",
            },
            {
                "scenario_id": "auth:Lockout after retries",
                "feature_file": "auth.feature",
                "tests": [],
                "status": "missing",
                "notes": "no test found",
            },
        ]
    }
)


class TestParseVerifierMatrix:
    def test_clean_json(self) -> None:
        scenarios = parse_verifier_matrix(CLEAN_JSON)
        assert len(scenarios) == 2
        first = scenarios[0]
        assert first.scenario_id == "auth:Successful login"
        assert first.feature_file == "auth.feature"
        assert first.tests == ["tests/test_auth.py::test_login"]
        assert first.status == COVERAGE_COVERED
        assert scenarios[1].status == COVERAGE_MISSING

    def test_fenced_json_with_surrounding_prose(self) -> None:
        text = (
            "Here is the traceability matrix you asked for.\n\n"
            "```json\n" + CLEAN_JSON + "\n```\n\n"
            "Let me know if anything needs adjusting."
        )
        scenarios = parse_verifier_matrix(text)
        assert len(scenarios) == 2
        assert scenarios[0].scenario_id == "auth:Successful login"

    def test_garbage_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            parse_verifier_matrix("I could not produce a matrix, sorry!")
        assert str(excinfo.value)  # says what failed

    def test_invalid_status_raises(self) -> None:
        bad = json.dumps(
            {
                "scenarios": [
                    {
                        "scenario_id": "auth:Login",
                        "feature_file": "auth.feature",
                        "tests": [],
                        "status": "kinda-covered",
                    }
                ]
            }
        )
        with pytest.raises(ValueError):
            parse_verifier_matrix(bad)


class TestMatrixFullyCovered:
    def test_true_when_all_covered_with_tests(self) -> None:
        m = _matrix(
            _scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"]),
            _scn("auth:b", COVERAGE_COVERED, ["tests/t.py::test_b1", "tests/t.py::test_b2"]),
        )
        assert matrix_fully_covered(m) is True

    def test_false_with_partial(self) -> None:
        m = _matrix(
            _scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"]),
            _scn("auth:b", COVERAGE_PARTIAL, ["tests/t.py::test_b"]),
        )
        assert matrix_fully_covered(m) is False

    def test_false_with_missing(self) -> None:
        m = _matrix(
            _scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"]),
            _scn("auth:b", COVERAGE_MISSING),
        )
        assert matrix_fully_covered(m) is False

    def test_false_when_covered_but_no_tests_listed(self) -> None:
        m = _matrix(_scn("auth:a", COVERAGE_COVERED, []))
        assert matrix_fully_covered(m) is False


class TestMatrixValidates:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_auth.py").write_text(
            "def test_login():\n    assert True\n\n"
            "def test_logout():\n    assert True\n"
        )
        return tmp_path

    def test_valid_matrix(self, repo: Path) -> None:
        m = _matrix(
            _scn("auth:login", COVERAGE_COVERED, ["tests/test_auth.py::test_login"])
        )
        ok, msg = matrix_validates(repo, m)
        assert ok, msg

    def test_fails_when_file_missing(self, repo: Path) -> None:
        m = _matrix(
            _scn("auth:gone", COVERAGE_COVERED, ["tests/test_missing.py::test_x"])
        )
        ok, msg = matrix_validates(repo, m)
        assert not ok
        assert msg

    def test_fails_when_function_absent(self, repo: Path) -> None:
        m = _matrix(
            _scn("auth:gone", COVERAGE_COVERED, ["tests/test_auth.py::test_deleted"])
        )
        ok, msg = matrix_validates(repo, m)
        assert not ok
        assert msg


class TestBumpRevisions:
    def test_increments_only_targeted_scenarios(self) -> None:
        m = _matrix(
            _scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"], revision=1),
            _scn("auth:b", COVERAGE_COVERED, ["tests/t.py::test_b"], revision=1),
        )

        bump_revisions(m, ["auth:a"], "escalation_approved", "weakened assertion approved")

        by_id = {s.scenario_id: s for s in m.scenarios}
        assert by_id["auth:a"].revision == 2
        assert by_id["auth:b"].revision == 1

    def test_appends_trace_revision(self) -> None:
        m = _matrix(_scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"]))

        bump_revisions(m, ["auth:a"], "auto_applied_minor", "fixed import path")

        assert len(m.revisions) == 1
        rev = m.revisions[-1]
        assert rev.kind == "auto_applied_minor"
        assert rev.scenario_ids == ["auth:a"]
        assert rev.description == "fixed import path"
        # timestamp parseable ISO-8601
        datetime.fromisoformat(rev.timestamp.replace("Z", "+00:00"))


class TestGapReport:
    def test_lists_partial_and_missing_only(self) -> None:
        m = _matrix(
            _scn("auth:alpha-covered", COVERAGE_COVERED, ["tests/t.py::test_a"]),
            _scn("auth:beta-partial", COVERAGE_PARTIAL, ["tests/t.py::test_b"]),
            _scn("auth:gamma-missing", COVERAGE_MISSING),
        )

        report = gap_report(m)

        assert isinstance(report, str)
        assert "beta-partial" in report
        assert "gamma-missing" in report
        assert "alpha-covered" not in report


class TestLoadSaveMatrix:
    def test_load_returns_none_when_absent(self, tmp_path: Path) -> None:
        feature_dir = tmp_path / "user-auth"
        (feature_dir / ".tdd").mkdir(parents=True)
        assert load_matrix(feature_dir) is None

    def test_round_trip(self, tmp_path: Path) -> None:
        feature_dir = tmp_path / "user-auth"
        (feature_dir / ".tdd").mkdir(parents=True)
        m = _matrix(
            _scn("auth:a", COVERAGE_COVERED, ["tests/t.py::test_a"], revision=2)
        )

        save_matrix(feature_dir, m)
        loaded = load_matrix(feature_dir)

        assert loaded is not None
        assert asdict_state(loaded) == asdict_state(m)
        # Committed audit artifact lives at the pinned location (§5).
        assert (feature_dir / ".tdd" / "traceability.json").exists()
