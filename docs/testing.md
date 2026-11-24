# Testing

Bud separates offline behavior tests from paid model evals:

- **Behavior tests (`tests/`):** deterministic, offline, free. Run automatically on every PR and `master` push.
- **Real-model evals (`src/bud/evals/`):** call real models (`bud eval`), cost money, manual only.

## Local commands

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

Narrow runs: `uv run pytest tests/test_server.py -x`. Override default 120s per-test timeout with `--timeout=300`. Add runtime Tracing in production via `uv sync --locked --no-dev --extra tracing`.

## Suite coverage & crash checkpoints

| Area | Verified invariants |
| :--- | :--- |
| Runner & API | Auth, approvals, revisions, stale build fencing (`StaleApproval`), cancellation, SSE streaming |
| Release & updates | Consistent SQLite WAL backups, SHA-256 verification, tar path safety, atomic swap & rollback |
| Gateway & reviewer | Tier fallback, spend caps, malformed review refusals, attachment capability routing |
| Workspace & sandbox | Traversal/symlink blocks, hash conflicts, claim replay, Seatbelt & proxy target checks |
| Crash recovery | Multi-process `create_app` kill/restart across claim, file write, and return checkpoints |

## Isolation rules

- **State & workspace:** `tests/conftest.py` clears inherited env settings (`BUD_*`, API keys), `chdir`s into `tmp_path`, and sets isolated `DATA_DIR` and `WORKSPACE_DIR`.
- **Network & models:** `tests/offline.py` blocks external TCP/DNS/UDP sockets and model requests. Fake HTTP and `FunctionModel` stand in for APIs.
- **Ports & settings cache:** test servers bind port `0` (`bud serve` fixes `8420`). Always call `get_settings.cache_clear()` after env mutations and clean up child PIDs so `live-server.lock` never leaks.

## CI & Release workflows

- **CI (`.github/workflows/ci.yml`):** runs `lint`, `test` (Python 3.12 and 3.14), `package` (clean wheel + sdist venv smoke import), `tui` (`cargo fmt`, `clippy`, `test`), and `mac` (`swift test` + `./mac/build.sh` on `macos-26`). Every job passes with zero API keys.
- **Release (`.github/workflows/release.yml`):** on `v*` tag pushes, builds Python wheel, Rust `bud-tui`, and `Bud.app`, smoke-installs into an isolated temp `BUD_HOME`, and publishes `.tar.gz` + `SHA256SUMS.txt`.

## Manual evals

```sh
uv run bud eval --trials 1 --task move_dana
uv run bud eval --trials 3
uv run bud eval --probe --trials 5
```

There is no spend cap flag on `bud eval`; bound spend with `--trials` and `--task`, and check `uv run bud costs`. Requires real model credentials (`OPENAI_API_KEY` for `--mode auto` / `--probe`, `EXA_API_KEY` on web-cassette misses).
