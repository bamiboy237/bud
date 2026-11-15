---
name: checks
description: 'Run the checks CI enforces for Python, the Rust TUI, and the macOS companion. Use after editing src/, tests/, tui/, or mac/, or when asked to verify, lint, type-check, or run the tests.'
---
# Project checks

Run these from the repository root. They match `.github/workflows/ci.yml`.

## Python

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

- `--locked` fails when `uv.lock` is stale. Regenerate it with `uv lock`; do not drop the flag.
- ruff covers `src` and `tests`, with line length 100 and the rules E, F, I, B, UP, SIM
  from `pyproject.toml`. Apply fixes with `uv run ruff check --fix src tests` and
  `uv run ruff format src tests`.
- `ty` suppresses the diagnostics recorded in `.ty/baseline.json`. A new diagnostic fails the
  check. Fix the code. Only regenerate the baseline with `uv run ty check --update-baseline`
  when the user asks for it.
- `pytest` runs the offline behavior suite. Run one file while iterating:
  `uv run pytest tests/test_server.py -x`. The suite needs no keys and no network; a test that
  reaches a model or account API is a bug. Configuration lives in `[tool.pytest.ini_options]`:
  `asyncio_mode = "auto"`, a 120 s per-test timeout, and strict markers and config.
  Register a new marker in `pyproject.toml` before using it.
- CI adds `--junitxml=test-results/junit.xml` to `pytest` and uploads that file when a job
  fails. The test selection is the same as the command above.

## Rust (tui/)

CI runs these when `tui/Cargo.toml` exists. Run them when `cargo` is on `PATH`:

```sh
cargo fmt --check --manifest-path tui/Cargo.toml
cargo clippy --locked --all-targets --manifest-path tui/Cargo.toml -- -D warnings
cargo test --locked --manifest-path tui/Cargo.toml
```

When `cargo` is not installed, say that the Rust checks did not run and that CI covers them.

## macOS companion

On macOS, run the native tests and packaged-art checks in
[mac/Tests/README.md](../../../mac/Tests/README.md). These match the macOS CI job.

## Report

Name each command and whether it passed. Give the pytest pass/fail counts. Name any check you
did not run and why.
