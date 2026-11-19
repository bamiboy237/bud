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

The dev group includes Logfire so CI also tests the optional tracing path. The tracing test
uses an in-memory exporter and checks that prompts, tool data, and replies are absent.
It does not send traces to Logfire cloud. Runtime-only installs need the `tracing` extra
to use `LOGFIRE_TOKEN`:

```sh
uv sync --locked --no-dev --extra tracing
```

## What the suite covers

| Area | What it proves |
| :--- | :--- |
| Runner and API | Denied, stale, wrong-session, and duplicate approvals; revisions; cancellation; authentication and input limits |
| Gateway and reviewer | Availability fallback, pinned and local routes, malformed reviews, refusals, and timeouts |
| Workspace | Traversal and symlink refusal, revision conflicts, and exact-argument claim replay |
| Recovery | Pending approvals survive restart; interrupted workspace writes resume or report failure without a second write |
| Packaging | The wheel and sdist install with locked deps and load their resources |

Recovery tests start a real `create_app` server in a child process, kill it, and start it again
against the same data directory. They use real DBOS, SQLite, and workspace tools with a
scripted model. The three crash checkpoints assert these outcomes:

| Crash point | Expected result after restart |
| :--- | :--- |
| Before the dispatch claim | The approved file is saved with one claim |
| After the claim is fsynced, before the file is saved | The file stays absent and the job reports not delivered |
| After the file is saved, before the tool returns | The saved file is recovered without changing its inode or modification time |

These checkpoints test workspace recovery. They do not establish delivery guarantees for
external email or calendar services.

## Isolation

Concurrent agents and concurrent tests must not share writable state or ports.

- **State.** Each server gets its own temporary `DATA_DIR`. Direct runner and tool tests also
  use temporary databases and workspaces. The fixture clears inherited settings and changes
  the working directory so the developer's `.env` is not loaded.
- **Workspace paths.** The fixture sets `WORKSPACE_DIR` to a temporary folder, and server
  tests set `HOME` to one, so no test writes to the real `~/.bud`. Evals and `dev-stack` keep
  their workspace in their own data folder. The workspace root and its contents reject
  symlinks.
- **Model config.** `create_app` needs a model for the `fast` tier and raises
  `NoModelAvailable` without one. Tests patch the gateway with a pydantic-ai `FunctionModel`, so
  no model-provider request leaves the test environment.
- **Owner token.** The app writes `DATA_DIR/auth/owner.token` (0600, in a 0700 directory).
  API tests send it as a bearer token, or set `OWNER_TOKEN` to their own base64url secret of
  43 to 256 characters.
- **Settings cache.** `get_settings()` is `lru_cache`d. Clear the cache after changing the
  environment, or the first settings object wins for the rest of the process.
- **Ports.** `bud serve` binds a fixed `127.0.0.1:8420`. Tests bind port 0 to get a free port from
  the OS, then pass the socket to uvicorn. Two agents can then run the suite at the same time.
- **Child processes.** Kill every child in a `finally` block. A leaked server holds
  `live-server.lock` in its data directory and blocks the next start.
- **Offline.** `tests/offline.py` disables paid model requests and blocks external TCP, DNS,
  and UDP traffic. Both pytest and child servers use this guard. Reviewer tests use fake HTTP
  responses. Other app tests disable the live reviewer.

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

The `package` job checks installation and loads the bundled prompt, policy, model registry,
and world seed. It runs outside the checkout with locked runtime dependencies. For a
wheel-only smoke check:

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

- `--trials` defaults to 3 attempts per task.
- `--task <id>` limits the run to one task. `--mode strict` is the default; `--mode auto` also
  exercises the approval reviewer.
- `--probe` tests the approval reviewer on hand-written calls instead of the task suite.

**There is no spend-cap flag.** Bound the cost by choosing `--trials` and `--task`, and check
`uv run bud costs` before and after. Each attempt stops after its task timeout, 180 seconds by
default, and at most 4 attempts run at once. A large suite can still cost more than you expect.
Evals never run in CI, and no scheduled job runs them.

Supply model credentials through environment variables, an ignored `.env`, or ChatGPT login.
Auto-mode evals and `--probe` also need `OPENAI_API_KEY` and approval review enabled.
Web-cache misses need `EXA_API_KEY`. See `docs/accounts.md` for account setup.
