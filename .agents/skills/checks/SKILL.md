---
name: checks
description: 'Run the checks CI enforces: uv sync --locked, ruff check, ruff format --check, ty check, and the tui/ Rust checks when cargo is installed. Use after editing src/ or tui/, or when asked to verify, lint, or type-check the project.'
---
# Project checks

Run these from the repository root. They match `.github/workflows/ci.yml`.

## Python

```sh
uv sync --locked
uv run ruff check src
uv run ruff format --check src
uv run ty check
```

- `--locked` fails when `uv.lock` is stale. Regenerate it with `uv lock`; do not drop the flag.
- ruff covers `src` only, with line length 100 and the rules E, F, I, B, UP, SIM from
  `pyproject.toml`. Apply fixes with `uv run ruff check --fix src` and `uv run ruff format src`.
- `ty` suppresses the diagnostics recorded in `.ty/baseline.json`. A new diagnostic fails the
  check. Fix the code. Only regenerate the baseline with `uv run ty check --update-baseline`
  when the user asks for it.

## Rust (tui/)

CI runs these when `tui/Cargo.toml` exists. Run them when `cargo` is on `PATH`:

```sh
cargo fmt --check --manifest-path tui/Cargo.toml
cargo clippy --locked --all-targets --manifest-path tui/Cargo.toml -- -D warnings
cargo test --locked --manifest-path tui/Cargo.toml
```

When `cargo` is not installed, say that the Rust checks did not run and that CI covers them.

## Report

Name each command and whether it passed. Name any check you did not run and why.
