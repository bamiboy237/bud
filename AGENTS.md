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
If Cargo is missing from a non-login shell, add `~/.cargo/bin` to `PATH`.

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
and sdist, installs each with the locked runtime dependencies, and loads the
packaged resources. The Rust job runs when `tui/Cargo.toml` exists; the macOS job
runs on every run.

Procedure, the Rust and macOS commands, and the `ty` baseline policy are in
`.agents/skills/checks/SKILL.md`.

## Tests

Behavior tests live in `tests/` and run offline: no real model or account calls,
and only loopback network access. Real-model evals are manual and cost money;
they never run in CI.

Isolate each test: point `DATA_DIR` at a temp directory and bind port 0. `bud serve`
binds a fixed `127.0.0.1:8420`, so tests start their own server instead.

To see a change in the Mac app, use `scripts/dev-stack`. Do not write your own
build, serve, and `pkill` steps.

```sh
scripts/dev-stack up                 # build the app, private server with your real accounts
scripts/dev-stack up --simulate      # simulated inbox and calendar
scripts/dev-stack shot approval-card --rect 1020,280,420,620
scripts/dev-stack down               # stops only this stack, deletes its data
```

Each stack gets its own data folder and a free port, so it runs beside the live
server on 8420. Real mode copies your Composio bindings but not the ChatGPT login,
so it uses the API keys in `.env`.

Test strategy, isolation details, manual app checks, and real-model eval commands
are in [docs/testing.md](docs/testing.md).

## Conventions

- ruff: line length 100, rules E, F, I, B, UP, SIM, target Python 3.12.
- Keep secrets out of committed files. `.env` is gitignored and linked into
  managed checkouts; `.env.example` lists the variables a developer supplies.
- Personal notes, course material, and local data stay out of Git.

## Docs

- [Architecture and durability](docs/architecture.md)
- [Security and approvals](docs/security.md)
- [Jobs](docs/jobs.md)
- [Skills](docs/skills.md)
