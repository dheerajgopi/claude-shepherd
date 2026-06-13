"""Contract pinning tests for tdd_contracts (depends only on the contracts module)."""

from __future__ import annotations

import pytest

from tdd_contracts import (
    COMMIT_BOOTSTRAP,
    COMMIT_GREEN,
    COMMIT_RED,
    COMMIT_RED_AMENDED,
    GITIGNORE_ENTRIES,
    PHASE_TRANSITIONS,
    RESUMABLE_PHASES,
    ExitCode,
    Phase,
    validate_transition,
)


class TestExitCodes:
    """Exit code values must match requirement §13 exactly."""

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("DONE", 0),
            ("AWAITING_APPROVAL", 10),
            ("COVERAGE_GAP", 11),
            ("ESCALATED", 12),
            ("BUDGET_EXCEEDED", 13),
            ("NEEDS_INPUT", 14),
            ("AWAITING_DESIGN_APPROVAL", 15),
            ("AWAITING_FRAMEWORK_APPROVAL", 16),
            ("NO_FEATURE_RESOLVED", 20),
            ("BRANCH_MISMATCH", 21),
            ("SHEPHERD_NOT_INITIALIZED", 22),
            ("INTERNAL_ERROR", 1),
        ],
    )
    def test_value(self, name: str, value: int) -> None:
        assert ExitCode[name] == value

    def test_no_extra_codes(self) -> None:
        assert len(ExitCode) == 12


class TestPhaseTransitions:
    @pytest.mark.parametrize(
        ("current", "new"),
        [
            (Phase.SKETCHING_DESIGN, Phase.AWAITING_DESIGN_APPROVAL),
            (Phase.AWAITING_DESIGN_APPROVAL, Phase.SKETCHING_DESIGN),  # corrections cycle
            (Phase.AWAITING_DESIGN_APPROVAL, Phase.DESIGN_APPROVED),
            (Phase.DESIGN_APPROVED, Phase.DRAFTING_REQUIREMENTS),
            (Phase.DRAFTING_REQUIREMENTS, Phase.AWAITING_APPROVAL),
            (Phase.AWAITING_APPROVAL, Phase.DRAFTING_REQUIREMENTS),  # corrections cycle
            (Phase.AWAITING_APPROVAL, Phase.REQUIREMENTS_APPROVED),
            (Phase.REQUIREMENTS_APPROVED, Phase.GENERATING_TESTS),
            (Phase.REQUIREMENTS_APPROVED, Phase.PROPOSING_FRAMEWORK),  # bootstrap fork
            (Phase.PROPOSING_FRAMEWORK, Phase.AWAITING_FRAMEWORK_APPROVAL),
            (Phase.AWAITING_FRAMEWORK_APPROVAL, Phase.PROPOSING_FRAMEWORK),  # corrections
            (Phase.AWAITING_FRAMEWORK_APPROVAL, Phase.INSTALLING_FRAMEWORK),  # approval
            (Phase.INSTALLING_FRAMEWORK, Phase.GENERATING_TESTS),
            (Phase.GENERATING_TESTS, Phase.VERIFYING_COVERAGE),
            (Phase.VERIFYING_COVERAGE, Phase.GENERATING_TESTS),  # gap iteration
            (Phase.VERIFYING_COVERAGE, Phase.RED_COMMITTED),
            (Phase.RED_COMMITTED, Phase.IMPLEMENTING),
            (Phase.IMPLEMENTING, Phase.ESCALATED),
            (Phase.IMPLEMENTING, Phase.BLOCKED),        # request_human_input
            (Phase.IMPLEMENTING, Phase.GREEN),
            (Phase.ESCALATED, Phase.AMENDING_REQUIREMENTS),  # approval
            (Phase.ESCALATED, Phase.IMPLEMENTING),      # rejection
            (Phase.BLOCKED, Phase.IMPLEMENTING),        # human answered
            (Phase.AMENDING_REQUIREMENTS, Phase.RED_COMMITTED),
            (Phase.GREEN, Phase.DONE),
        ],
    )
    def test_legal(self, current: Phase, new: Phase) -> None:
        assert new in PHASE_TRANSITIONS[current]
        assert validate_transition(current, new)

    @pytest.mark.parametrize(
        ("current", "new"),
        [
            (Phase.SKETCHING_DESIGN, Phase.DESIGN_APPROVED),  # cannot skip approval
            (Phase.SKETCHING_DESIGN, Phase.DRAFTING_REQUIREMENTS),  # cannot skip approval
            (Phase.DESIGN_APPROVED, Phase.AWAITING_APPROVAL),  # must draft requirements first
            (Phase.AWAITING_DESIGN_APPROVAL, Phase.DRAFTING_REQUIREMENTS),
            (Phase.DRAFTING_REQUIREMENTS, Phase.IMPLEMENTING),
            (Phase.DRAFTING_REQUIREMENTS, Phase.REQUIREMENTS_APPROVED),  # cannot skip approval
            (Phase.REQUIREMENTS_APPROVED, Phase.RED_COMMITTED),     # cannot skip testgen
            (Phase.PROPOSING_FRAMEWORK, Phase.GENERATING_TESTS),    # cannot skip approval
            (Phase.AWAITING_FRAMEWORK_APPROVAL, Phase.GENERATING_TESTS),  # must install first
            (Phase.IMPLEMENTING, Phase.DONE),                  # must pass through GREEN
            (Phase.GREEN, Phase.IMPLEMENTING),
            (Phase.DONE, Phase.DRAFTING_REQUIREMENTS),              # DONE is terminal
            (Phase.FAILED, Phase.DRAFTING_REQUIREMENTS),            # FAILED is terminal
            (Phase.ESCALATED, Phase.GREEN),
            (Phase.BLOCKED, Phase.GREEN),               # must pass back through IMPLEMENTING
        ],
    )
    def test_illegal(self, current: Phase, new: Phase) -> None:
        assert new not in PHASE_TRANSITIONS[current]
        assert not validate_transition(current, new)

    def test_failed_reachable_from_anywhere(self) -> None:
        for phase in Phase:
            assert validate_transition(phase, Phase.FAILED), phase

    def test_terminals_have_no_outgoing_transitions(self) -> None:
        assert PHASE_TRANSITIONS[Phase.DONE] == ()
        assert PHASE_TRANSITIONS[Phase.FAILED] == ()

    def test_every_phase_has_a_transition_entry(self) -> None:
        assert set(PHASE_TRANSITIONS) == set(Phase)


class TestResumablePhases:
    def test_covers_every_non_terminal_phase(self) -> None:
        non_terminal = set(Phase) - {Phase.DONE, Phase.FAILED}
        assert set(RESUMABLE_PHASES) == non_terminal

    def test_loop_numbers_are_valid(self) -> None:
        assert set(RESUMABLE_PHASES.values()) <= {0, 1, 2, 3}

    def test_loop_ownership_spot_checks(self) -> None:
        assert RESUMABLE_PHASES[Phase.SKETCHING_DESIGN] == 0
        assert RESUMABLE_PHASES[Phase.DESIGN_APPROVED] == 1  # Loop 1's entry from Loop 0
        assert RESUMABLE_PHASES[Phase.DRAFTING_REQUIREMENTS] == 1
        assert RESUMABLE_PHASES[Phase.GENERATING_TESTS] == 2
        assert RESUMABLE_PHASES[Phase.IMPLEMENTING] == 3


class TestCommitFormats:
    """Commit messages per §16, exact. No spec commit: .shepherd/ is gitignored."""

    def test_red(self) -> None:
        assert COMMIT_RED.format(slug="user-auth") == (
            "tdd(user-auth): red — failing tests"
        )

    def test_red_amended(self) -> None:
        assert COMMIT_RED_AMENDED.format(slug="user-auth", n=2) == (
            "tdd(user-auth): red(2) — amended requirements"
        )

    def test_green(self) -> None:
        assert COMMIT_GREEN.format(slug="user-auth") == (
            "tdd(user-auth): green — implementation"
        )

    def test_bootstrap(self) -> None:
        assert COMMIT_BOOTSTRAP.format(slug="user-auth", framework="pytest") == (
            "tdd(user-auth): chore — add pytest"
        )


class TestGitignorePolicy:
    """§5 version-control policy: the whole workspace is machine-local."""

    def test_whole_workspace_ignored(self) -> None:
        assert GITIGNORE_ENTRIES == [".shepherd/"]
