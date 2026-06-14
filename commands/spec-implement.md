---
description: Run the shepherd plugin's strict, spec-driven, test-first (red-green) workflow for a feature (design sketch → EARS requirements spec → failing tests → implementation), handling every checkpoint exit code.
argument-hint: [task description or feature slug]
---

# /shepherd:spec-implement — spec-implement outer loop

This command is a thin entry point: all instructions live in the **spec-implement
skill**. It carries no protocol of its own — do not handle exit codes or
invoke `spec_implement.py` from memory.

1. Invoke the `spec-implement` skill (if the Skill tool can't resolve it, read
   `${CLAUDE_PLUGIN_ROOT}/skills/spec-implement/SKILL.md` directly) and follow it
   exactly, including its exit-code playbook reference.
2. Treat `$ARGUMENTS` as the user's request and route it per the skill's
   **Routing the request** section (task description, feature slug, or
   empty).
