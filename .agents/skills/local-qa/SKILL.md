---
name: local-qa
description: Run local formatting, linting, type checking, tests, and workflow security checks after changes.
---

# Local QA

Run `scripts/qa.sh` relative to this skill directory after making changes.
The script operates from the repository root and can modify files.
Inspect `git status` afterwards and report modified files and any failures.

Use `uv` for Python and `pnpm` for Node tooling. If a required checker
is missing, install it rather than silently skipping a quality gate.
Fix failures and rerun; do not weaken checks to obtain a passing result.
