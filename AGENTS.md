# AGENTS.md

Bud is a local-first personal AI assistant. The Python package is in `src/bud`, the
Rust terminal client in `tui/`, and the macOS companion in `mac/`.

## Setup

```sh
uv sync --locked
```

One command installs the runtime and dev dependencies. `--locked` fails when
`uv.lock` is stale; change dependencies with `uv add` and commit the new lock.
`.agents/prepare` runs this same command in each new managed checkout.

Requires `uv` and Python >= 3.12. `.python-version` pins 3.14 for local work.

## Checks

These match `.github/workflows/ci.yml`. Run them from the repository root.

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

CI runs `pytest` on Python 3.12 and 3.14, and a package job that builds the wheel
and sdist and installs each into a fresh venv without the dev group. The Rust and
macOS jobs run when `tui/Cargo.toml` and `mac/` exist.

Procedure, the Rust and macOS commands, and the `ty` baseline policy are in
`.agents/skills/checks/SKILL.md`.

## Tests

Behavior tests live in `tests/` and run offline: no model, account, or network
calls. Real-model evals are manual and cost money; they never run in CI.

Isolate each test: point `DATA_DIR` at a temp directory and bind port 0. `bud
serve` binds a fixed `127.0.0.1:8420`, so tests start their own server instead.

Strategy, isolation details, and the manual eval commands are in
[docs/testing.md](docs/testing.md).

## Conventions

- ruff: line length 100, rules E, F, I, B, UP, SIM, target Python 3.12.
- Keep secrets out of committed files. `.env` is gitignored and linked into
  managed checkouts; `.env.example` lists the variables a developer supplies.
- Personal notes, course material, and local data stay out of Git.

## Docs

- [Testing](docs/testing.md)
- [Architecture and durability](docs/architecture.md)
- [Security and approvals](docs/security.md)
- [Jobs](docs/jobs.md)
- [Skills](docs/skills.md)
