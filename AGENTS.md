# AGENTS.md

Bud is a local-first personal AI assistant: Python backend in `src/bud`, Rust terminal client in `tui/`, and macOS companion in `mac/`.

## Setup & Checks

Requires `uv` and Python >= 3.12 (`.python-version` pins 3.14; add `~/.cargo/bin` to `PATH` if Cargo is missing):

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

Change dependencies with `uv add` and commit `uv.lock`. Rust, macOS CLT (`@StateObject` instead of `@State`), and `ty` baseline details live in `.agents/skills/checks/SKILL.md`.

## Isolation & Dev Stack

- Behavior tests in `tests/` run offline with temp `DATA_DIR`, temp `WORKSPACE_DIR`, and port `0`. Never point tests at `~/.bud` or `127.0.0.1:8420`.
- Preview Mac app changes with `scripts/dev-stack` instead of custom build/`pkill` scripts:

```sh
scripts/dev-stack up              # isolated Bud Dev (app.bud.companion.dev), free port, temp workspace, snapshots live Composio bindings
scripts/dev-stack up --simulate   # simulated inbox and calendar
scripts/dev-stack shot card --rect 1020,280,420,620
scripts/dev-stack down            # stop only this dev stack and remove its temp state
```

## Branches & Conventions

- Do not commit on `master` (`.githooks` enforces PRs; enable with `git config core.hooksPath .githooks` if needed).
- `ruff`: line length 100, rules E, F, I, B, UP, SIM, Python 3.12 target.
- Keep secrets (`.env`), personal notes, and local `data/` out of Git.

## Docs

- [Setup & updates](docs/setup.md)
- [Architecture & jobs](docs/architecture.md)
- [Security & approvals](docs/security.md)
- [Testing & CI](docs/testing.md)
