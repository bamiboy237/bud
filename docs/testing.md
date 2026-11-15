# Testing

Bud has two kinds of tests, kept apart on purpose:

- **Behavior tests** in `tests/`. Offline, deterministic, and free. CI runs them on every pull
  request and on pushes to `master`.
- **Real-model evals** in `src/bud/evals/`. They call real models, cost money, and run only when
  a developer starts them by hand.

## Local commands

These are the commands CI runs. Run them from the repository root.

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

While iterating, run one file or one test:

```sh
uv run pytest tests/test_server.py -x
uv run pytest -x
```

`uv sync --locked` fails when `uv.lock` does not match `pyproject.toml`. Change dependencies
with `uv add` or `uv add --dev` and commit the updated lock.

## What the suite covers

| Area | What it proves |
| :--- | :--- |
| Runner and API | Events, projections, approvals, and HTTP routes behave as specified |
| Gateway and reviewer | Routing, fallback, and approval-review decisions |
| Workspace | Path rules, revisions, and claim replay |
| Recovery | A real server process restarts and resumes durable state |
| Packaging | The wheel and sdist install with locked deps and load their resources |

Recovery tests start a real `create_app` server in a child process, kill it, and start it again
against the same data directory. They use a real DBOS runtime and SQLite, not mocks.

## Isolation

Concurrent agents and concurrent tests must not share writable state or ports.

- **State.** `DATA_DIR` is the only writable root. A fixture points it at a per-test temporary
  directory before the app is built. The app creates `bud.db`, `runtime.db`, `bud.lock`,
  `world.db`, `world-recall.db`, `auth/`, `skills/`, and `workspace/` under it.
- **Resolve the path.** `create_app` refuses symlinked path components. On macOS `/tmp` and
  `/var` are symlinks, so pass `tmp_path.resolve()`; a raw temp path fails with
  `NotADirectoryError`.
- **Model config.** `create_app` needs a model for the `fast` tier and raises
  `NoModelAvailable` without one. Tests patch the gateway with a pydantic-ai `FunctionModel`, so
  no key is configured and no request leaves the process.
- **Owner token.** The app writes `DATA_DIR/auth/owner.token` (0600, in a 0700 directory).
  API tests send it as a bearer token, or set `OWNER_TOKEN` to their own base64url secret of
  43 to 256 characters.
- **Settings cache.** `get_settings()` is `lru_cache`d. Clear the cache after changing the
  environment, or the first settings object wins for the rest of the process.
- **Ports.** `bud serve` binds a fixed `127.0.0.1:8420`. Tests bind port 0 to get a free port from
  the OS, then pass the socket to uvicorn. Two agents can then run the suite at the same time.
- **Child processes.** Kill every child in a `finally` block. A leaked server holds
  `live-server.lock` in its data directory and blocks the next start.
- **Offline.** No test reaches a model, Composio, Photon, or Exa API. `ALLOW_MODEL_REQUESTS` is
  `False` for the suite and the approval reviewer stays off, so a test that tries to call a model
  fails instead of spending money.

pytest configuration lives in `[tool.pytest.ini_options]` in `pyproject.toml`:

- `asyncio_mode = "auto"`: plain `async def` tests run without a marker.
- `timeout = 120`: a hung test fails by name instead of holding the CI job. The default
  `signal` method raises inside the test, so `try/finally` cleanup still runs and child
  processes are killed. Override with `uv run pytest --timeout=300` for a slow local run.
- `--strict-markers` and `--strict-config`: a typo in a marker or an ini key fails the run.
  Register a new marker under `markers` in `pyproject.toml` before using it.

## CI

`.github/workflows/ci.yml` runs these jobs in parallel:

| Job | What it does |
| :--- | :--- |
| `lint` | ruff check and format on `src` and `tests`, then `ty check` |
| `test` | `uv run pytest` on Python 3.12 and 3.14 |
| `package` | Builds the wheel and sdist, installs each with the locked runtime dependencies, and loads the packaged resources |
| `tui` | Rust format, clippy, and tests, when `tui/Cargo.toml` exists |
| `mac` | Swift tests and the packaged app, on macOS |

The `package` job catches a missing package file, an undeclared runtime dependency, or a
resource that only exists in the checkout. It installs from `$RUNNER_TEMP`, outside the
checkout, with the versions `uv export` takes from the lock. Run it locally with:

```sh
uv build --out-dir dist
uv export --locked --no-dev --no-emit-project -q -o /tmp/constraints.txt
uv venv --python 3.12 /tmp/smoke
uv pip install --python /tmp/smoke/bin/python --constraint /tmp/constraints.txt dist/*.whl
cd /tmp && /tmp/smoke/bin/python -c "from bud.world.store import load_seed; assert load_seed()['world']; print('resources ok')"
```

CI has no model keys. Every job must pass without one. The `test` job adds
`--junitxml=test-results/junit.xml` and uploads that file when the job fails; the test
selection is the same as `uv run pytest`.

## Real-model evals

Evals check judgment and tool use against the simulated world. Model requests use the real
gateway, so they are billed to your key or subscription and recorded in `bud costs`. Web reads
replay from `data/evals/web-cassette.json`; a cache miss calls Exa live and needs
`EXA_API_KEY`.

```sh
uv run bud eval --trials 1 --task move_dana
uv run bud eval --trials 3
uv run bud eval --probe --trials 5
```

- `--trials` defaults to 3 over all 33 tasks, so one command can make 99 attempts.
- `--task <id>` limits the run to one task. `--mode strict` is the default; `--mode auto` also
  exercises the approval reviewer.
- `--probe` tests the approval reviewer on hand-written calls instead of the task suite.

**There is no spend-cap flag.** Bound the cost by choosing `--trials` and `--task`, and check
`uv run bud costs` before and after. Each attempt stops after its task timeout, 180 seconds by
default, and at most 4 attempts run at once. A large suite can still cost more than you expect.
Evals never run in CI, and no scheduled job runs them.

Evals need `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `MUSE_API_KEY`, `MISTRAL_API_KEY`, or a ChatGPT
login, plus `EXA_API_KEY` for a web-cache miss. `.env.example` lists `OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`, and `MISTRAL_API_KEY`; set `MUSE_API_KEY` and `EXA_API_KEY` in your own
`.env` when you use them. See `docs/accounts.md` for account setup.
