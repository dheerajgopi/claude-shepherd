"""Tests for tdd_state: slugify, StateStore round-trip, transitions."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

tdd_state = pytest.importorskip("tdd_state")  # parallel track (T1-CORE)

from tdd_contracts import (  # noqa: E402
    STATE_FILE,
    BudgetsSpent,
    ExitCode,
    FeatureState,
    HistoryEntry,
    Phase,
    asdict_state,
)
from tdd_state import SluiceError, StateStore, slugify  # noqa: E402

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _get(entry, key):
    """Field access tolerant of dataclass or plain-dict history entries."""

    return entry[key] if isinstance(entry, dict) else getattr(entry, key)


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class TestSlugify:
    def test_basic_kebab(self) -> None:
        assert slugify("Add user auth!") == "add-user-auth"

    def test_whitespace_collapsed(self) -> None:
        assert slugify("  Payment   Retry  ") == "payment-retry"

    def test_unicode_yields_valid_kebab(self) -> None:
        slug = slugify("Café crème brûlée handling")
        assert slug
        assert KEBAB.fullmatch(slug), slug

    def test_long_title_truncated_to_50(self) -> None:
        title = " ".join(["word"] * 20)  # ~99 chars
        slug = slugify(title)
        assert len(slug) <= 50
        assert KEBAB.fullmatch(slug), slug

    @pytest.mark.parametrize("title", ["", "!!!", "???", "   "])
    def test_empty_result_raises_value_error(self, title: str) -> None:
        with pytest.raises(ValueError):
            slugify(title)


@pytest.fixture
def feature_dir(tmp_path: Path) -> Path:
    d = tmp_path / "user-auth"
    (d / ".tdd").mkdir(parents=True)
    return d


def _full_state() -> FeatureState:
    return FeatureState(
        slug="user-auth",
        branch="tdd/user-auth",
        base_commit="a" * 40,
        phase=Phase.AWAITING_APPROVAL.value,
        session_ids={"loop1": "sess-1", "loop2": None, "loop3": None},
        history=[
            HistoryEntry(
                phase=Phase.DRAFTING_REQUIREMENTS.value,
                timestamp="2026-06-11T00:00:00+00:00",
            ),
            HistoryEntry(
                phase=Phase.AWAITING_APPROVAL.value,
                timestamp="2026-06-11T00:05:00+00:00",
                session_id="sess-1",
            ),
        ],
        budgets_spent=BudgetsSpent(
            cost_usd=1.25,
            turns_loop1=7,
            turns_loop2=0,
            turns_loop3=0,
            started_at="2026-06-11T00:00:00+00:00",
        ),
        red_commit_count=2,
        overrides={"models": {"implement": "claude-opus-4-8"}},
    )


class TestStateStore:
    def test_round_trip_preserves_all_fields(self, feature_dir: Path) -> None:
        store = StateStore(feature_dir)
        state = _full_state()

        store.save(state)
        loaded = store.load()

        assert asdict_state(loaded) == asdict_state(state)

    def test_save_produces_valid_json(self, feature_dir: Path) -> None:
        store = StateStore(feature_dir)
        state = _full_state()
        state_path = feature_dir / Path(STATE_FILE)

        store.save(state)
        first = json.loads(state_path.read_text())
        assert first["slug"] == "user-auth"

        state.red_commit_count = 3
        store.save(state)
        second = json.loads(state_path.read_text())  # still valid JSON after rewrite
        assert second["red_commit_count"] == 3

    def test_transition_appends_history_with_utc_timestamp(
        self, feature_dir: Path
    ) -> None:
        store = StateStore(feature_dir)
        state = FeatureState(
            slug="user-auth",
            branch="tdd/user-auth",
            base_commit="a" * 40,
            phase=Phase.DRAFTING_REQUIREMENTS.value,
        )

        store.transition(state, Phase.AWAITING_APPROVAL, session_id="sess-9")

        assert Phase(state.phase) is Phase.AWAITING_APPROVAL
        assert len(state.history) >= 1
        last = state.history[-1]
        assert Phase(_get(last, "phase")) is Phase.AWAITING_APPROVAL
        assert _get(last, "session_id") == "sess-9"

        ts = _get(last, "timestamp")
        dt = _parse_utc(ts)  # must parse as ISO-8601
        assert dt.utcoffset() == timedelta(0)
        # Catches local-time-stamped-as-UTC bugs:
        assert abs(datetime.now(timezone.utc) - dt) < timedelta(minutes=2)

    def test_transition_rejects_illegal_move(self, feature_dir: Path) -> None:
        store = StateStore(feature_dir)
        state = FeatureState(
            slug="user-auth",
            branch="tdd/user-auth",
            base_commit="a" * 40,
            phase=Phase.DRAFTING_REQUIREMENTS.value,
        )

        with pytest.raises(SluiceError) as excinfo:
            store.transition(state, Phase.IMPLEMENTING)
        assert excinfo.value.exit_code == ExitCode.INTERNAL_ERROR

    def test_transition_to_failed_allowed_from_anywhere(
        self, feature_dir: Path
    ) -> None:
        store = StateStore(feature_dir)
        state = FeatureState(
            slug="user-auth",
            branch="tdd/user-auth",
            base_commit="a" * 40,
            phase=Phase.GENERATING_TESTS.value,
        )

        store.transition(state, Phase.FAILED, reason="budget blown")

        assert Phase(state.phase) is Phase.FAILED
        assert _get(state.history[-1], "reason") == "budget blown"
