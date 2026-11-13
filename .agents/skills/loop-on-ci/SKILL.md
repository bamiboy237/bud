---
name: loop-on-ci
description: 'Watch the pull request checks and fix failures until they are green, using gh pr checks as the source of truth. Use when CI fails, when asked to fix CI, or after pushing a branch that needs a green run.'
---
# Loop on CI

`gh pr checks` is the source of truth. It covers every check attached to the pull request.

## Workflow

1.  Resolve the PR for the current branch.
    - `gh pr view --json number,url,headRefName`
2.  Inspect the checks before waiting.
    - `gh pr checks --json name,bucket,state,workflow,link`
3.  If a check already failed, diagnose that failure first.
    - `gh run view <run-id> --log-failed`
4.  If checks are pending, watch them.
    - `gh pr checks --watch --fail-fast`
5.  Apply the smallest fix for one failure cause, push, then re-read the full check set.
    The set can change after a push.

## What CI runs here

- `check`, on Python 3.12 and 3.14: `uv sync --locked`, `uv run ruff check src`,
  `uv run ruff format --check src`, `uv run ty check`.
- `tui`, only when `tui/Cargo.toml` exists: `cargo fmt --check`,
  `cargo clippy --locked --all-targets -- -D warnings`, `cargo test --locked`.

Reproduce a failure locally with the `checks` skill before pushing.

## Guardrails

- Fix one actionable failure at a time.
- Never bypass a check to make it pass: do not add to `.ty/baseline.json`, do not drop
  `--locked`, do not disable a ruff rule.
- Do not bypass hooks with `--no-verify`.
- If a failure is flaky, retry once and report the evidence for the flake.
- If a failure comes from `master` and not from this branch, merge `origin/master`
  instead of fixing unrelated code here.

## Output

- Current CI status
- Failure summary and the fixes applied, in iteration order
- The PR URL once green
