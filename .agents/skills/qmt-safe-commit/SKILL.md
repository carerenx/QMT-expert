---
name: qmt-safe-commit
description: Safely review, verify, stage, and commit changes in the QMT-expert repository. Use when asked to commit QMT strategies, MiniQMT infrastructure, backtest code, tests, analysis, or documentation. Do not use for stash creation or restoration unless explicitly requested.
---

# QMT Safe Commit

Create a small, attributable commit without sweeping in unrelated work.

## Rules

- Commit only when the user explicitly asks. Never push without explicit authorization.
- Read the root `AGENTS.md`. Treat QMT-injected globals as valid; do not add stubs merely to silence IDE warnings.
- Preserve unrelated work. Never delete or revert unknown changes. Exclude generated files, logs, outputs, secrets, and local settings.
- Shared MiniQMT infrastructure can affect many strategies; report that scope.

## Workflow

1. Inspect status, staged/unstaged diffs, untracked files, and recent commit style. Attribute every file to the request.
2. Verify in proportion to risk. For SDK behavior prefer `C:\QMT\bin.x64\python.exe`; test order routing with fakes that cannot place live orders. Report unavailable dependencies instead of installing them without permission.
3. Stage explicit paths when the worktree is mixed. Inspect the staged diff, then run `git diff --cached --check` and `git diff --cached --stat`.
4. Make focused conventional commits (`fix:`, `feat:`, `test:`, `docs:`, `chore:`).
5. Report the commit hash, verification, and remaining working-tree changes.
