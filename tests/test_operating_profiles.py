"""Offline tests for immutable operating profiles, attribution, rollback, and recovery."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from bud.config import get_settings
from bud.core import composition
from bud.core.auth import owner_token
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.runner import Runner
from bud.core.server import _create_app
from bud.evals.runner import report as write_eval_report
from bud.evals.runner import run_attempt
from bud.evals.tasks import Task
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from bud.harness.actions import Action, Policy
from bud.harness.agent import Harness
from bud.harness.operating_profile import (
    BASELINE_PROFILE,
    ContextBudgetConfig,
    ExecutionLimitsConfig,
    HarnessCompositionConfig,
    ModelProfileConfig,
    OperatingProfile,
    OperatingProfileStore,
    ToolResultConfig,
    resolve_operating_profile,
)
from bud.workspace import READ_TOOLS, WRITE_TOOLS, build_tools, read_capabilities
from tests.runner_support import create_note, reply, scripted_model


def _queued_responses(*items: Any):
    pending = deque(items)

    def respond(messages, info):
        assert pending, "Bud made an unexpected model request"
        item = pending.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    return respond


@asynccontextmanager
async def _profile_bud(
    directory: Path,
    function,
    *,
    profile: OperatingProfile = BASELINE_PROFILE,
    specs: list[ModelSpec] | None = None,
    failures: dict[str, Exception] | None = None,
    jobs: bool = False,
    extra_tools: list[Any] | None = None,
):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "workspace"
    root.mkdir(exist_ok=True)
    log = EventLog(directory / "events.db")
    claims = directory / "dispatches.jsonl"
    model_specs = specs or [
        ModelSpec(provider="openai", name="scripted", tier="fast", input=1, output=2, cached=0.5)
    ]
    registry = Registry(models=model_specs)
    failures = failures or {}

    def build_model(spec: ModelSpec):
        def respond(messages, info):
            if spec.id in failures:
                raise failures[spec.id]
            resp = function(messages, info)
            if isinstance(resp, ModelResponse) and resp.usage.total_tokens == 0:
                resp.usage = RequestUsage(input_tokens=100, output_tokens=20)
            return resp

        async def stream(messages, info):
            resp = respond(messages, info)
            for index, part in enumerate(resp.parts):
                if isinstance(part, TextPart):
                    yield part.content
                elif isinstance(part, ToolCallPart):
                    yield {
                        index: DeltaToolCall(
                            name=part.tool_name,
                            json_args=json.dumps(part.args_as_dict()),
                            tool_call_id=part.tool_call_id,
                        )
                    }

        return FunctionModel(respond, stream_function=stream, model_name=spec.name)

    gateway = Gateway(registry, Ledger(directory / "costs.db"), build_model)
    policy: dict[str, Action] = dict.fromkeys(READ_TOOLS, "allow")
    policy.update(dict.fromkeys(WRITE_TOOLS, "ask"))
    tools: list[Any] = [tool.function for tool in build_tools(root, claims)]
    for fn in extra_tools or ():
        policy[fn.__name__] = "allow"
        tools.append(fn)
    harness = Harness(
        FunctionToolset[None](tools),
        Policy(tools=policy),
        registry,
        "A synthetic workspace for operating-profile tests.",
        state_dir=directory,
        result_path=log.path,
        read_tools=READ_TOOLS,
        capabilities=[*read_capabilities(root)],
        operating_profile=profile,
    )
    harness.clock = lambda: datetime(2026, 11, 22, 12, tzinfo=UTC)
    runner = Runner(
        Hub(log),
        gateway,
        harness,
        jobs=jobs,
        workspace=root,
        operating_profile=profile,
    )
    try:
        yield runner, log, gateway, root, claims
    finally:
        await runner.stop()
        gateway.close()


def test_baseline_profile_is_immutable_and_has_deterministic_digest():
    assert BASELINE_PROFILE.id == "baseline"
    assert BASELINE_PROFILE.version == "1"
    assert len(BASELINE_PROFILE.digest) == 64

    with pytest.raises(ValidationError):
        BASELINE_PROFILE.version = "2"  # ty: ignore[invalid-assignment]
    with pytest.raises(ValidationError):
        BASELINE_PROFILE.context.max_fraction = 0.5  # ty: ignore[invalid-assignment]

    reconstructed = OperatingProfile(id="baseline", version="1")
    assert reconstructed.digest == BASELINE_PROFILE.digest

    candidate = BASELINE_PROFILE.derive(
        version="2",
        context=ContextBudgetConfig(max_fraction=0.5, fallback_context_window=32_000),
    )
    assert candidate.previous_version == "1"
    assert candidate.digest != BASELINE_PROFILE.digest


async def test_baseline_vs_candidate_attribution_and_behavior(tmp_path):
    candidate = BASELINE_PROFILE.derive(
        id="compact-local",
        version="2",
        harness=HarnessCompositionConfig(
            instruction_style="terse",
            instruction_addendum="Keep replies concise.",
        ),
        tool_results=ToolResultConfig(
            spill_chars=40,
            preview_chars=10,
            truncate_max_chars=20,
            history_spill_chars=40,
        ),
        limits=ExecutionLimitsConfig(request_limit=4, retries=2),
    )

    seen_prompts: dict[str, list[str]] = {}
    big_content = "alpha " * 30  # > 40 chars, < 10_000 chars

    for label, profile in (("baseline", BASELINE_PROFILE), ("candidate", candidate)):
        sub = tmp_path / label
        (sub / "workspace").mkdir(parents=True)
        note_path = sub / "workspace" / "notes.txt"
        note_path.write_text(big_content)

        def read_memo(path: Path = note_path) -> str:
            return path.read_text()

        def make_responder(tag: str):
            calls = 0

            def respond(messages, info):
                nonlocal calls
                calls += 1
                seen_prompts.setdefault(tag, []).append(
                    "\n".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
                )
                if calls == 1:
                    return ModelResponse(
                        parts=[ToolCallPart("read_memo", {}, tool_call_id="r1")],
                        usage=RequestUsage(input_tokens=50, output_tokens=10),
                    )
                return ModelResponse(
                    parts=[TextPart(f"done:{tag}")],
                    usage=RequestUsage(input_tokens=60, output_tokens=15),
                )

            return respond

        async with _profile_bud(
            sub, make_responder(label), profile=profile, extra_tools=[read_memo]
        ) as (
            runner,
            log,
            gateway,
            _root,
            _claims,
        ):
            log.append("owner", "session.created")
            runner.start("owner", "Read notes.txt", "m1")
            while runner.tasks:
                await list(runner.tasks.values())[0]

            events = log.read("owner")
            queued = next(e.data for e in events if e.type == "run.queued")
            started = next(e.data for e in events if e.type == "run.started")
            assert queued["model"] == "openai:scripted"
            assert queued["profile"] == profile.attribution()
            assert queued["prompt_version"] == runner.harness.prompt_version
            assert started["model"] == "openai:scripted"
            assert started["profile"] == profile.attribution()
            assert started["prompt_version"] == runner.harness.prompt_version

            view = runner.snapshot("owner").recent_runs[-1]
            assert view.status == "completed"
            assert view.model == "openai:scripted"
            assert view.profile == profile.attribution()
            assert view.prompt_version == runner.harness.prompt_version

            ledger_rows = gateway.ledger.run_requests(queued["run_id"])
            assert len(ledger_rows) == 2
            for row in ledger_rows:
                assert row["status"] == "ok"
                assert row["model"] == "openai:scripted"
                assert row["profile_id"] == profile.id
                assert row["profile_version"] == profile.version
                assert row["profile_digest"] == profile.digest
                assert row["prompt_version"] == runner.harness.prompt_version

    # Candidate spilled the 180-char result (>40 chars), while baseline kept it inline (<10_000).
    assert "alpha alpha" not in seen_prompts["candidate"][1]
    assert big_content in seen_prompts["baseline"][1]
    assert "Tool output too large" in seen_prompts["candidate"][1]
    assert "stored to handle" in seen_prompts["candidate"][1]


async def test_fallback_model_attribution_records_actual_model_and_profile(tmp_path):
    specs = [
        ModelSpec(provider="openai", name="primary", tier="fast", input=1, output=2, cached=0.5),
        ModelSpec(provider="openai", name="backup", tier="fast", input=2, output=4, cached=1.0),
    ]
    profile = BASELINE_PROFILE.derive(version="2", model=ModelProfileConfig(tier="fast"))
    async with _profile_bud(
        tmp_path,
        _queued_responses(reply("from backup")),
        profile=profile,
        specs=specs,
        failures={"openai:primary": ModelHTTPError(503, "openai:primary")},
    ) as (runner, log, gateway, _root, _claims):
        log.append("owner", "session.created")
        runner.start("owner", "Hello", "m1")
        while runner.tasks:
            await list(runner.tasks.values())[0]

        run_id = next(e.data["run_id"] for e in log.read("owner") if e.type == "run.started")
        entries = gateway.ledger.run_requests(run_id)
        assert [(e["model"], e["status"]) for e in entries] == [
            ("openai:primary", "failed"),
            ("openai:backup", "ok"),
        ]
        assert all(
            e["profile_id"] == profile.id
            and e["profile_version"] == profile.version
            and e["profile_digest"] == profile.digest
            and e["prompt_version"] == runner.harness.prompt_version
            for e in entries
        )


async def test_candidate_rollback_restores_exact_previous_version_and_digest(tmp_path):
    store = OperatingProfileStore(tmp_path / "operating-profile.json")
    assert store.load() == BASELINE_PROFILE

    candidate = BASELINE_PROFILE.derive(
        version="2",
        harness=HarnessCompositionConfig(instruction_addendum="Candidate behavior."),
        context=ContextBudgetConfig(max_fraction=0.5),
    )
    store.activate(candidate)
    assert store.load().digest == candidate.digest
    assert resolve_operating_profile(store=store).digest == candidate.digest

    rolled_back = store.rollback()
    assert rolled_back.id == BASELINE_PROFILE.id
    assert rolled_back.version == BASELINE_PROFILE.version
    assert rolled_back.digest == BASELINE_PROFILE.digest
    assert store.load().digest == BASELINE_PROFILE.digest

    # Verify runs after rollback carry baseline attribution and prompt_version.
    async with _profile_bud(
        tmp_path / "run", _queued_responses(reply("ok")), profile=store.load()
    ) as (
        runner,
        log,
        gateway,
        _root,
        _claims,
    ):
        log.append("owner", "session.created")
        runner.start("owner", "Hi", "m1")
        while runner.tasks:
            await list(runner.tasks.values())[0]
        started = next(e.data for e in log.read("owner") if e.type == "run.started")
        assert started["profile"] == BASELINE_PROFILE.attribution()
        rows = gateway.ledger.run_requests(started["run_id"])
        assert rows[0]["profile_digest"] == BASELINE_PROFILE.digest


async def test_recovery_rejects_profile_digest_mismatch_before_tools(tmp_path, monkeypatch):
    async with _profile_bud(tmp_path, _queued_responses(), profile=BASELINE_PROFILE) as (
        runner,
        log,
        _gateway,
        _root,
        _claims,
    ):
        log.append("owner", "session.created")
        with monkeypatch.context() as patch:
            patch.setattr(runner, "_launch", lambda run: None)
            runner.start("owner", "Create note.txt", "message-1")
        assert len(log.pending_runs()) == 1

    candidate = BASELINE_PROFILE.derive(
        version="2",
        context=ContextBudgetConfig(keep_tokens=8_000),
    )
    async with _profile_bud(tmp_path, _queued_responses(create_note()), profile=candidate) as (
        runner,
        _log,
        _gateway,
        root,
        claims,
    ):
        await runner.recover()
        while runner.tasks:
            await list(runner.tasks.values())[0]
        state = runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        assert state.recent_runs[-1].reason is not None
        assert "configuration changed" in state.recent_runs[-1].reason.lower()
        assert not (root / "note.txt").exists()
        assert not claims.exists()


async def test_permission_and_side_effect_gates_are_invariant_across_profiles(tmp_path):
    candidate = BASELINE_PROFILE.derive(
        id="aggressive",
        version="3",
        harness=HarnessCompositionConfig(
            instruction_addendum="Act quickly.",
            code_mode_enabled=False,
            caching_enabled=False,
        ),
        context=ContextBudgetConfig(max_fraction=0.5, keep_tokens=4_000),
        limits=ExecutionLimitsConfig(request_limit=5, retries=0),
    )

    for label, profile in (("baseline", BASELINE_PROFILE), ("candidate", candidate)):
        sub = tmp_path / label
        async with _profile_bud(
            sub,
            _queued_responses(create_note(f"content-{label}"), reply(f"saved-{label}")),
            profile=profile,
        ) as (runner, log, _gateway, root, claims):
            log.append("owner", "session.created")
            runner.start("owner", "Create note.txt", "m1")
            while runner.tasks:
                await list(runner.tasks.values())[0]

            # Write must be held for approval regardless of operating profile.
            pending = runner.snapshot("owner").pending_approvals
            assert len(pending) == 1
            assert pending[0].tool == "workspace_create"
            assert not (root / "note.txt").exists()
            assert not claims.exists()

            assert runner.decide("owner", pending[0].approval_id, True)
            while runner.tasks:
                await list(runner.tasks.values())[0]
            assert (root / "note.txt").read_text() == f"content-{label}"
            assert len(claims.read_text().splitlines()) == 1


async def test_eval_and_server_share_composition_path_and_attribute_profiles(tmp_path, monkeypatch):
    # Patch composition hooks to observe that both paths invoke the exact same
    # composition pipeline (`compose_world_harness` -> `compose_runner` -> `Runner`).
    called_compose_world: list[str] = []
    called_compose_runner: list[str] = []
    orig_world = composition.compose_world_harness
    orig_runner = composition.compose_runner

    def spy_world(*args, **kwargs):
        called_compose_world.append(kwargs.get("operating_profile", BASELINE_PROFILE).digest)
        return orig_world(*args, **kwargs)

    def spy_runner(*args, **kwargs):
        called_compose_runner.append(args[2].operating_profile.digest)
        return orig_runner(*args, **kwargs)

    monkeypatch.setattr("bud.core.server.compose_world_harness", spy_world)
    monkeypatch.setattr("bud.core.server.compose_runner", spy_runner)
    monkeypatch.setattr("bud.evals.runner.compose_world_harness", spy_world)
    monkeypatch.setattr("bud.evals.runner.compose_runner", spy_runner)

    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=1, output=2, cached=0.5)
    model = scripted_model(
        lambda messages, info: ModelResponse(
            parts=[TextPart("Weekly briefing: calm week ahead.")],
            usage=RequestUsage(input_tokens=30, output_tokens=10),
        )
    )
    gateway = Gateway(Registry(models=[spec]), Ledger(tmp_path / "eval-ledger.db"), lambda _: model)
    monkeypatch.setattr("bud.core.server.build_gateway", lambda *a, **kw: gateway)

    candidate = BASELINE_PROFILE.derive(
        id="briefing-test",
        version="2",
        harness=HarnessCompositionConfig(instruction_addendum="Weekly briefing check."),
    )

    try:
        # 1. Server simulated composition path
        settings = get_settings()
        app = _create_app(
            settings,
            owner_token(settings),
            None,
            simulate=True,
            model_id=None,
            operating_profile=candidate,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.headers["Authorization"] = f"Bearer {owner_token(settings).get_secret_value()}"
            runtime = (await client.get("/runtime")).json()
            assert runtime["operating_profile"] == candidate.attribution()

        # 2. Eval runner composition path (offline FunctionModel, weekly briefing prompt)
        briefing_task = Task(
            id="weekly_briefing_offline",
            instruction="Give me my weekly briefing across Slack, email, and calendar.",
            check=lambda out: [] if "Weekly briefing" in out.answer else ["missing briefing"],
        )
        attempt = await run_attempt(briefing_task, gateway, mode="strict", profile=candidate)
        assert attempt.passed
        assert attempt.profile == candidate.attribution()
        assert attempt.models == [{"model": "openai:scripted", "billing": "api"}]

        report_path = write_eval_report(
            [attempt],
            trials=1,
            prompt_version=attempt.prompt_version,
            mode="strict",
            profile=attempt.profile,
        )
        saved = json.loads(report_path.read_text())
        assert saved["profile"] == candidate.attribution()
        assert saved["attempts"][0]["profile"] == candidate.attribution()
    finally:
        gateway.close()

    assert called_compose_world == [candidate.digest, candidate.digest]
    assert called_compose_runner == [candidate.digest, candidate.digest]


def test_ledger_migrates_existing_database_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE model_requests (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                session_id TEXT,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                latency_ms REAL NOT NULL,
                cost_usd REAL,
                error TEXT
            )"""
        )
    ledger = Ledger(db_path)
    assert ledger.run_requests("missing") == []
    with sqlite3.connect(db_path) as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(model_requests)")}
    assert {
        "billing",
        "run_id",
        "profile_id",
        "profile_version",
        "profile_digest",
        "prompt_version",
    } <= cols


async def test_execution_request_limit_is_enforced_from_profile(tmp_path):
    limited = BASELINE_PROFILE.derive(
        version="2",
        limits=ExecutionLimitsConfig(request_limit=1),
    )

    def two_step(messages, info):
        return ModelResponse(
            parts=[ToolCallPart("list_directory", {}, tool_call_id="l1")],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    async with _profile_bud(tmp_path, two_step, profile=limited) as (
        runner,
        log,
        _gateway,
        _root,
        _claims,
    ):
        log.append("owner", "session.created")
        runner.start("owner", "List files", "m1")
        while runner.tasks:
            await list(runner.tasks.values())[0]
        state = runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        failed = next(e for e in log.read("owner") if e.type == "run.failed")
        assert failed.data["reason"]
