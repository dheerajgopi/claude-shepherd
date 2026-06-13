#!/usr/bin/env bash
# setup.sh — per-project installer for the shepherd plugin (docs/tdd-skill-requirements.md §4).
#
# Run once from a TARGET project's root:
#   /path/to/shepherd/bin/setup.sh           # install (default)
#   /path/to/shepherd/bin/setup.sh update    # report upstream vs local drift (no writes)
#
# Idempotent. Preflight runs before any repo mutation, so a failure never
# leaves a half-installed workspace (its one side effect is installing the
# Python deps, which is itself idempotent). Workspace logic lives in
# `tdd.py init`; this script is plumbing: preflight -> settings merge ->
# init -> manifest.

set -euo pipefail

# --- roots -------------------------------------------------------------------

SHEPHERD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ROOT="$(pwd)"

TDD_PY="$SHEPHERD_ROOT/skills/tdd/scripts/tdd.py"
SETTINGS_FILE="$TARGET_ROOT/.claude/settings.json"
MANIFEST_FILE="$TARGET_ROOT/.shepherd/manifest.json"   # tdd_contracts.MANIFEST_FILE
CONFIG_FILE="$TARGET_ROOT/.shepherd/config.yaml"       # tdd_contracts.CONFIG_FILE

# --- helpers -----------------------------------------------------------------

die() {
    printf 'setup.sh: error: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf 'setup.sh: warning: %s\n' "$*" >&2
}

info() {
    printf '%s\n' "$*"
}

# A dependency-install command that needs NO sudo: it targets the ambient
# python3's per-user site-packages (~/.local/...), which is exactly where the
# bare `python3` that runs the engine looks — no venv to create or activate.
# uv pip has no per-user install mode, so we use pip's --user here even when uv
# is present; on a PEP-668 "externally managed" python, append
# --break-system-packages (or use a venv).
install_hint() {
    if python3 -m pip --version >/dev/null 2>&1; then
        printf 'python3 -m pip install --user claude-agent-sdk pyyaml'
    else
        printf 'python3 -m ensurepip --user && python3 -m pip install --user claude-agent-sdk pyyaml'
    fi
}

# True iff both runtime deps import under the ambient python3 (the interpreter
# the engine runs under).
deps_importable() {
    python3 -c 'import claude_agent_sdk, yaml' >/dev/null 2>&1
}

# --- preflight (auto-installs missing deps; else fails loudly with fixes) ------

preflight() {
    info "==> Preflight checks"

    # 1. TARGET_ROOT must be the root of a git repository.
    local git_top
    git_top="$(git -C "$TARGET_ROOT" rev-parse --show-toplevel 2>/dev/null)" \
        || die "not inside a git repository.
  Fix: cd to your project's root (or run 'git init') and re-run setup.sh."
    if [ "$git_top" != "$TARGET_ROOT" ]; then
        die "must run from the git repository root.
  You are in:  $TARGET_ROOT
  Repo root:   $git_top
  Fix: cd \"$git_top\" && \"$SHEPHERD_ROOT/bin/setup.sh\""
    fi

    # 2. python3 >= 3.10.
    command -v python3 >/dev/null 2>&1 \
        || die "python3 not found on PATH.
  Fix: install Python 3.10+ (e.g. 'sudo apt install python3' or via pyenv/uv)."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "python3 is older than 3.10 (found: $(python3 --version 2>&1)).
  Fix: install Python 3.10+ and make it the 'python3' on PATH."

    # 3. uv or pip must be available.
    if ! command -v uv >/dev/null 2>&1 \
        && ! python3 -m pip --version >/dev/null 2>&1; then
        die "neither 'uv' nor 'pip' is available.
  Fix: install uv (https://docs.astral.sh/uv/) or 'python3 -m ensurepip --upgrade'."
    fi

    # 4. claude CLI — warn only; Shepherd's skill/command surface needs it,
    #    but setup itself does not.
    if ! command -v claude >/dev/null 2>&1; then
        warn "'claude' CLI not found on PATH.
  Shepherd is driven from Claude Code, so you'll need it to use /shepherd:tdd.
  Install: npm install -g @anthropic-ai/claude-code  (or see https://docs.claude.com/en/docs/claude-code)"
    fi

    # 5. Python deps importable. If missing, install them automatically WITHOUT
    #    sudo (into this python3's per-user site-packages, where the engine's
    #    bare `python3` finds them) and re-check. This is the one mutation
    #    preflight makes — it is idempotent and reversible, and it is what lets
    #    `tdd.py init` (run later, same interpreter) clear its own dep gate.
    if ! deps_importable; then
        info "    Python deps missing; installing without sudo:"
        info "      $(install_hint)"
        eval "$(install_hint)" || true
        if ! deps_importable; then
            die "claude-agent-sdk and/or pyyaml are still missing after install.
  This python3 is likely 'externally managed' (PEP 668). Install into a
  user-writable interpreter, then re-run setup.sh — e.g.:
    $(install_hint) --break-system-packages
  (or create and activate a venv that becomes the 'python3' on PATH)."
        fi
        info "    installed claude-agent-sdk + pyyaml (user site-packages)"
    fi

    info "    ok: git repo root, python3 >= 3.10, uv/pip, python deps"
}

# --- capability registration (§4 step 2) --------------------------------------

# Merges the shepherd plugin into TARGET's project-scoped .claude/settings.json.
# Embedded python (never jq/sed): the file may carry the team's existing config,
# so we deep-merge ONLY `enabledPlugins` and leave every other key untouched.
# A timestamped backup is taken iff the file exists and content will change.
#
# TODO(T3-LIVE): validate exact enabledPlugins key shape against a real
# marketplace install. "shepherd@local": true (object form) is unverified; if it
# turns out wrong, PLUGIN_KEY below is the one-line fix.
register_plugin() {
    info "==> Registering shepherd plugin in .claude/settings.json (project scope)"
    mkdir -p "$TARGET_ROOT/.claude"

    SETTINGS_FILE="$SETTINGS_FILE" python3 - <<'PY'
import json
import os
import sys
import time

settings_file = os.environ["SETTINGS_FILE"]
PLUGIN_KEY = "shepherd@local"  # TODO(T3-LIVE): see comment above register_plugin

if os.path.exists(settings_file):
    with open(settings_file, encoding="utf-8") as f:
        raw = f.read()
    try:
        settings = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"setup.sh: error: {settings_file} is not valid JSON ({exc}).\n"
            "  Fix: repair the file by hand, then re-run setup.sh. "
            "It was NOT modified.\n"
        )
        sys.exit(1)
    existed = True
else:
    settings = {}
    existed = False

if not isinstance(settings, dict):
    sys.stderr.write(
        f"setup.sh: error: {settings_file} top level is not a JSON object. "
        "Not modified.\n"
    )
    sys.exit(1)

plugins = settings.get("enabledPlugins")
if plugins is not None and not isinstance(plugins, dict):
    sys.stderr.write(
        f"setup.sh: error: 'enabledPlugins' in {settings_file} is not an "
        "object; refusing to guess. Not modified.\n"
    )
    sys.exit(1)

if isinstance(plugins, dict) and plugins.get(PLUGIN_KEY) is True:
    print(f"    already registered: enabledPlugins[{PLUGIN_KEY!r}]")
    sys.exit(0)

# Deep-merge ONLY enabledPlugins; every other key passes through untouched.
merged_plugins = dict(plugins or {})
merged_plugins[PLUGIN_KEY] = True
settings["enabledPlugins"] = merged_plugins

if existed:
    backup = f"{settings_file}.bak.{int(time.time())}"
    with open(backup, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"    backed up existing settings to {backup}")

with open(settings_file, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2, sort_keys=False)
    f.write("\n")
print(f"    registered enabledPlugins[{PLUGIN_KEY!r}] = true in {settings_file}")
PY
}

# --- workspace bootstrap (§4 step 3) -------------------------------------------

bootstrap() {
    info "==> Bootstrapping .shepherd workspace (tdd.py init)"
    [ -f "$TDD_PY" ] || die "engine script not found: $TDD_PY
  The Shepherd checkout looks incomplete. Fix: git -C \"$SHEPHERD_ROOT\" pull (or re-clone)."

    # init owns the logic (config detection, .gitignore policy, idempotence);
    # propagate its output and stop on any nonzero exit.
    if ! (cd "$TARGET_ROOT" && python3 "$TDD_PY" init); then
        die "'tdd.py init' failed (see output above). Nothing further was written.
  Fix the reported problem and re-run setup.sh."
    fi
}

# --- manifest (§4 step 4) -------------------------------------------------------

write_manifest() {
    info "==> Writing .shepherd/manifest.json"

    local shepherd_sha
    shepherd_sha="$(git -C "$SHEPHERD_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

    MANIFEST_FILE="$MANIFEST_FILE" \
    SETTINGS_FILE="$SETTINGS_FILE" \
    CONFIG_FILE="$CONFIG_FILE" \
    TARGET_ROOT="$TARGET_ROOT" \
    SHEPHERD_SHA="$shepherd_sha" \
    python3 - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone

target_root = os.environ["TARGET_ROOT"]

def sha256_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

# artifacts: target-root-relative path -> sha256 (ShepherdManifest schema).
artifacts = {}
for path in (os.environ["SETTINGS_FILE"], os.environ["CONFIG_FILE"]):
    if os.path.exists(path):
        artifacts[os.path.relpath(path, target_root)] = sha256_of(path)

manifest = {
    "shepherd_sha": os.environ["SHEPHERD_SHA"],
    "installed_at": datetime.now(timezone.utc).isoformat(),
    "artifacts": artifacts,
    "schema_version": 1,
}

manifest_file = os.environ["MANIFEST_FILE"]
os.makedirs(os.path.dirname(manifest_file), exist_ok=True)
with open(manifest_file, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=False)
    f.write("\n")
print(f"    wrote {manifest_file} (shepherd_sha {manifest['shepherd_sha'][:12]})")
PY
}

# --- summary (§4) ---------------------------------------------------------------

summary() {
    cat <<EOF

================================================================
Shepherd setup complete in: $TARGET_ROOT

What was done
  - Registered the shepherd plugin in .claude/settings.json
    (project-scoped; committed, so teammates get it on pull)
  - Bootstrapped the .shepherd/ workspace via 'tdd.py init'
  - Recorded install state in .shepherd/manifest.json

What to review (init's detection is a best guess)
  - .shepherd/config.yaml -> test.command  (must run your test suite)
  - .shepherd/config.yaml -> test.paths    (feeds the TDD edit-boundary hooks;
    a wrong boundary undermines the safety model)

How to use
  - Open Claude Code in this project and run:  /shepherd:tdd
  - Later, check for shepherd drift with:
      "$SHEPHERD_ROOT/bin/setup.sh" update
================================================================
EOF
}

# --- update (stub: report-only, never overwrites) -------------------------------

cmd_update() {
    info "==> shepherd update check (report-only; nothing is modified)"
    [ -f "$MANIFEST_FILE" ] || die "no manifest at $MANIFEST_FILE.
  Fix: run \"$SHEPHERD_ROOT/bin/setup.sh\" (install) first."

    local shepherd_sha
    shepherd_sha="$(git -C "$SHEPHERD_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

    MANIFEST_FILE="$MANIFEST_FILE" \
    TARGET_ROOT="$TARGET_ROOT" \
    SHEPHERD_SHA="$shepherd_sha" \
    python3 - <<'PY'
import hashlib
import json
import os

target_root = os.environ["TARGET_ROOT"]
current_sha = os.environ["SHEPHERD_SHA"]

with open(os.environ["MANIFEST_FILE"], encoding="utf-8") as f:
    manifest = json.load(f)

recorded_sha = manifest.get("shepherd_sha", "unknown")
print(f"    installed shepherd_sha: {recorded_sha}")
print(f"    current shepherd_sha:   {current_sha}")
if recorded_sha != current_sha:
    print("    -> upstream shepherd has changed since install")
else:
    print("    -> shepherd unchanged upstream")

locally_modified = []
for rel_path, recorded_hash in sorted(manifest.get("artifacts", {}).items()):
    path = os.path.join(target_root, rel_path)
    if not os.path.exists(path):
        print(f"    MISSING   {rel_path} (recorded at install, now gone)")
        locally_modified.append(rel_path)
        continue
    with open(path, "rb") as f:
        current_hash = hashlib.sha256(f.read()).hexdigest()
    if current_hash == recorded_hash:
        print(f"    unchanged {rel_path}")
    else:
        print(f"    MODIFIED  {rel_path} (local changes since install)")
        locally_modified.append(rel_path)

print()
if locally_modified:
    print("    Local modifications detected; refusing to overwrite:")
    for rel_path in locally_modified:
        print(f"      - {rel_path}")
    print("    Auto-update is not implemented yet. Review and merge by hand,")
    print("    or revert local changes and re-run setup.sh.")
else:
    print("    No local modifications. Auto-update is not implemented yet;")
    print("    re-run setup.sh to refresh the install.")
PY
}

# --- install (default) -----------------------------------------------------------

cmd_install() {
    preflight
    register_plugin
    bootstrap
    write_manifest
    summary
}

# --- main ------------------------------------------------------------------------

main() {
    case "${1:-install}" in
        install) cmd_install ;;
        update)  cmd_update ;;
        -h|--help|help)
            sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            ;;
        *)
            die "unknown subcommand: '$1' (expected: install | update)"
            ;;
    esac
}

main "$@"
