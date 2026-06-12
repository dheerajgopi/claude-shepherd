---
description: Run the sluice plugin's strict TDD workflow for a feature (Gherkin spec → failing tests → implementation), handling every checkpoint exit code.
argument-hint: [task description or feature slug]
---

# /sluice:tdd — TDD outer loop

This command is a thin entry point: all instructions live in the **tdd
skill**. It carries no protocol of its own — do not handle exit codes or
invoke `tdd.py` from memory.

1. Invoke the `tdd` skill (if the Skill tool can't resolve it, read
   `${CLAUDE_PLUGIN_ROOT}/skills/tdd/SKILL.md` directly) and follow it
   exactly, including its exit-code playbook reference.
2. Treat `$ARGUMENTS` as the user's request and route it per the skill's
   **Routing the request** section (task description, feature slug, or
   empty).
