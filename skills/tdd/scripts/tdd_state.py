"""State, configuration, and active-feature resolution for the TDD sluice.

Owns the machine-local runtime surface defined in requirement §5/§7/§14:
config.yaml parsing (`load_config`), the state.json store (`StateStore`),
the per-invocation feature context (`FeatureContext`, `resolve_feature`),
and slug derivation (`slugify`). Everything shared with other modules comes
from tdd_contracts; nothing here re-derives an exit code, phase, path
constant, or commit format.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tdd_contracts import (
    BRANCH_PREFIX,
    BudgetsConfig,
    BudgetsSpent,
    CONFIG_FILE,
    FEATURES_DIR,
    REQUIREMENTS_DIR,
    REPORTS_DIR,
    STATE_FILE,
    TDD_DIR,
    ExitCode,
    FeatureState,
    SluiceConfig,
    HistoryEntry,
    ModelsConfig,
    Phase,
    TestConfig,
    asdict_state,
    validate_transition,
)

_MAX_SLUG_LEN = 50


class SluiceError(Exception):
    """An expected failure carrying the exit code the CLI must end with."""

    def __init__(self, exit_code: ExitCode, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC timestamp (second precision)."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _filtered_kwargs(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of dataclass `cls` (unknown keys ignored)."""

    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in raw.items() if k in names}


def _config_section(cls: type, data: dict[str, Any], name: str) -> Any:
    """Build a config-section dataclass from `data[name]`, tolerating absence."""

    raw = data.get(name) or {}
    if not isinstance(raw, dict):
        raise SluiceError(
            ExitCode.INTERNAL_ERROR,
            f"{CONFIG_FILE}: section '{name}' must be a mapping, got {type(raw).__name__}",
        )
    return cls(**_filtered_kwargs(cls, raw))


def load_config(repo_root: Path) -> SluiceConfig:
    """Parse .sluice/config.yaml into a SluiceConfig.

    Missing file raises SluiceError(SLUICE_NOT_INITIALIZED). Unknown keys
    are ignored; missing keys fall back to the dataclass defaults. test.paths
    must be a list of relative path strings (it feeds the Loop 2/3 hooks).
    """

    try:
        import yaml
    except ImportError as exc:  # surfaced cleanly instead of a bare traceback
        raise SluiceError(
            ExitCode.INTERNAL_ERROR,
            "pyyaml is not installed in this interpreter; run `tdd.py init` "
            "preconditions or `pip install pyyaml`",
        ) from exc

    config_path = repo_root / CONFIG_FILE
    if not config_path.is_file():
        raise SluiceError(
            ExitCode.SLUICE_NOT_INITIALIZED,
            f"no {CONFIG_FILE} found under {repo_root}; run `tdd.py init` first",
        )
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SluiceError(
            ExitCode.INTERNAL_ERROR, f"{CONFIG_FILE} is not valid YAML: {exc}"
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SluiceError(
            ExitCode.INTERNAL_ERROR,
            f"{CONFIG_FILE} must contain a mapping at the top level",
        )

    config = SluiceConfig(
        models=_config_section(ModelsConfig, data, "models"),
        test=_config_section(TestConfig, data, "test"),
        budgets=_config_section(BudgetsConfig, data, "budgets"),
    )

    paths = config.test.paths
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise SluiceError(
            ExitCode.INTERNAL_ERROR,
            f"{CONFIG_FILE}: test.paths must be a list of strings, got {paths!r}",
        )
    for p in paths:
        if os.path.isabs(p):
            raise SluiceError(
                ExitCode.INTERNAL_ERROR,
                f"{CONFIG_FILE}: test.paths entries must be relative, got {p!r}",
            )
    return config


class StateStore:
    """Load/save/transition the gitignored state.json of one feature (§14)."""

    def __init__(self, feature_dir: Path) -> None:
        """Bind the store to `feature_dir` (.sluice/features/<slug>)."""

        self.feature_dir = feature_dir
        self.path = feature_dir / STATE_FILE

    def load(self) -> FeatureState:
        """Read state.json; SluiceError(INTERNAL_ERROR) if missing or corrupt."""

        if not self.path.is_file():
            raise SluiceError(
                ExitCode.INTERNAL_ERROR,
                f"state file missing: {self.path} (state.json is machine-local; "
                "if this feature was created elsewhere, start it fresh here)",
            )
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            history = [
                HistoryEntry(**_filtered_kwargs(HistoryEntry, e))
                for e in data.get("history", [])
            ]
            budgets = BudgetsSpent(
                **_filtered_kwargs(BudgetsSpent, data.get("budgets_spent", {}) or {})
            )
            kwargs = _filtered_kwargs(FeatureState, data)
            kwargs["history"] = history
            kwargs["budgets_spent"] = budgets
            return FeatureState(**kwargs)
        except (ValueError, TypeError, KeyError) as exc:
            raise SluiceError(
                ExitCode.INTERNAL_ERROR,
                f"state file corrupt: {self.path}: {exc}",
            ) from exc

    def save(self, state: FeatureState) -> None:
        """Atomically write state.json (tmp file + os.replace)."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict_state(state), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def transition(
        self,
        state: FeatureState,
        new_phase: Phase,
        session_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> FeatureState:
        """Validated phase transition: update phase, append history, save.

        Raises SluiceError(INTERNAL_ERROR) if `state.phase -> new_phase` is
        not legal per tdd_contracts.validate_transition.
        """

        try:
            current = Phase(state.phase)
        except ValueError as exc:
            raise SluiceError(
                ExitCode.INTERNAL_ERROR,
                f"state has unknown phase {state.phase!r}",
            ) from exc
        if not validate_transition(current, new_phase):
            raise SluiceError(
                ExitCode.INTERNAL_ERROR,
                f"illegal phase transition: {current.value} -> {new_phase.value}",
            )
        state.phase = new_phase.value
        state.history.append(
            HistoryEntry(
                phase=new_phase.value,
                timestamp=utc_now_iso(),
                session_id=session_id,
                reason=reason,
            )
        )
        self.save(state)
        return state


@dataclass
class FeatureContext:
    """Everything a loop needs about the active feature (the `ctx` seam)."""

    repo_root: Path
    slug: str
    feature_dir: Path
    requirements_dir: Path
    tdd_dir: Path
    reports_dir: Path
    config: SluiceConfig
    state: FeatureState
    store: StateStore


def list_features(repo_root: Path) -> list[tuple[str, str]]:
    """All (slug, phase) pairs under .sluice/features, phase 'UNKNOWN' if unreadable."""

    features_dir = repo_root / FEATURES_DIR
    out: list[tuple[str, str]] = []
    if not features_dir.is_dir():
        return out
    for entry in sorted(features_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            phase = StateStore(entry).load().phase
        except SluiceError:
            phase = "UNKNOWN"
        out.append((entry.name, phase))
    return out


def _feature_listing(repo_root: Path) -> str:
    """Human-readable feature list for NO_FEATURE_RESOLVED messages (§7)."""

    features = list_features(repo_root)
    if not features:
        return "  (no features yet — create one with `tdd.py new <title>`)"
    return "\n".join(f"  {slug}: {phase}" for slug, phase in features)


def resolve_feature(
    repo_root: Path, feature_arg: Optional[str], force: bool
) -> FeatureContext:
    """Resolve the active feature per §7: explicit arg, then branch convention.

    No pointer file, no inference. Anything else raises
    SluiceError(NO_FEATURE_RESOLVED) listing existing features and phases.
    The current branch must match state.branch unless `force`
    (else SluiceError(BRANCH_MISMATCH)).
    """

    import tdd_git  # local import: tdd_git imports SluiceError from this module

    config = load_config(repo_root)
    features_dir = repo_root / FEATURES_DIR
    branch = tdd_git.current_branch(repo_root)

    if feature_arg is not None:
        slug = feature_arg
        feature_dir = features_dir / slug
        if not feature_dir.is_dir():
            raise SluiceError(
                ExitCode.NO_FEATURE_RESOLVED,
                f"no feature folder for --feature {slug!r}. Existing features:\n"
                + _feature_listing(repo_root),
            )
    elif branch.startswith(BRANCH_PREFIX) and (
        features_dir / branch[len(BRANCH_PREFIX):]
    ).is_dir():
        slug = branch[len(BRANCH_PREFIX):]
        feature_dir = features_dir / slug
    else:
        raise SluiceError(
            ExitCode.NO_FEATURE_RESOLVED,
            f"no --feature given and current branch {branch!r} is not a "
            f"{BRANCH_PREFIX}<slug> branch with a matching feature folder. "
            "Existing features:\n" + _feature_listing(repo_root),
        )

    store = StateStore(feature_dir)
    state = store.load()
    if branch != state.branch and not force:
        raise SluiceError(
            ExitCode.BRANCH_MISMATCH,
            f"current branch {branch!r} does not match the branch recorded for "
            f"feature {slug!r} ({state.branch!r}); switch branches or pass "
            "--force if this is intended",
        )
    return FeatureContext(
        repo_root=repo_root,
        slug=slug,
        feature_dir=feature_dir,
        requirements_dir=feature_dir / REQUIREMENTS_DIR,
        tdd_dir=feature_dir / TDD_DIR,
        reports_dir=feature_dir / REPORTS_DIR,
        config=config,
        state=state,
        store=store,
    )


def slugify(title: str) -> str:
    """Kebab-case slug from a feature title (§5).

    Lowercase, non-alphanumeric runs collapse to a single hyphen, leading and
    trailing hyphens stripped, truncated to 50 characters. Raises ValueError
    if nothing remains.
    """

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:_MAX_SLUG_LEN].rstrip("-")
    if not slug:
        raise ValueError(f"title {title!r} produces an empty slug")
    return slug
