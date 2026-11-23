"""Automatic Bud wakeup on capability setup completion or cancellation."""

import asyncio
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.config import Settings
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import SETUP_WAKEUP
from bud.core.runner import Runner
from bud.core.server import _create_app
from bud.core.setup import (
    SetupEntry,
    SetupInitiation,
    SetupJournal,
    SetupRegistry,
    SetupStatus,
    build_setup_toolset,
    check_setup,
)
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from bud.harness.actions import Policy
from bud.harness.agent import Harness
from bud.harness.review import ToolReviewer
from bud.harness.taint import owner_text, read_text
from bud.workspace import READ_TOOLS, WRITE_TOOLS, build_tools
from tests.runner_support import reply, scripted_model


def _last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


async def _settle(runner: Runner) -> None:
    async with asyncio.timeout(10):
        while runner.tasks:
            await asyncio.gather(*list(runner.tasks.values()))
            await asyncio.sleep(0)


@pytest.fixture
def setup_wakeup_api(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    found = Mock(spec=Connections)
    found.settings = settings
    found.folder = tmp_path / "composio"
    found.user_id = "owner"
    found.capture_binding.return_value = AccountBinding("owner", 1, ())
    found.binding = found.capture_binding.return_value
    found.stored.side_effect = lambda: found.binding

    def adopt(expected):
        found.binding = expected

    found.adopt.side_effect = adopt
    found.accounts.return_value = {}
    found.restart_required.return_value = False
    found.store = Mock()
    found.store.session_scope.return_value = "scope"
    found.store.cancel_connect.return_value = True
    found.connect.return_value = "https://example.test/private-sign-in"
    found.pending_url.return_value = "https://example.test/existing-sign-in"
    found.statuses.return_value = {"gmail": "INITIATED", "linear": "INITIATED"}
    monkeypatch.setattr("bud.core.server.Connections", lambda _: found)

    remote = [FunctionToolset[None]([])]
    monkeypatch.setattr("bud.composio.tools.build_toolset", lambda *args: remote[0])

    prompts: list[str] = []
    tools_seen: list[set[str]] = []
    mode_ref = {"handler": None}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        handler = mode_ref["handler"]
        assert handler is not None
        return handler(messages, info)

    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    gateway = Gateway(
        Registry(models=[spec]),
        Ledger(tmp_path / "ledger.db"),
        lambda _: scripted_model(model),
    )
    monkeypatch.setattr("bud.core.server.build_gateway", lambda *args, **kwargs: gateway)

    created: list[Runner] = []

    def make_runner(*args, **kwargs):
        kwargs["durable"] = False
        instance = Runner(*args, **kwargs)
        instance.learner = None
        created.append(instance)
        return instance

    monkeypatch.setattr("bud.core.server.Runner", make_runner)
    token = "offline-setup-wakeup-" + "x" * 41
    app = _create_app(settings, SecretStr(token), None, simulate=False, model_id=None)
    runner = created[0]
    log = runner.hub.log
    try:
        yield app, token, found, log, runner, remote, mode_ref, prompts, tools_seen
    finally:
        gateway.close()


async def test_check_connected_wakes_bud_once_and_continues_task(setup_wakeup_api):
    app, token, found, log, runner, remote, mode_ref, prompts, tools_seen = setup_wakeup_api
    runner._loop = asyncio.get_running_loop()
    instructions_seen: list[str] = []

    def read_emails() -> str:
        return "2 unread emails: Flight confirmation, Weekly digest"

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        prompts.append(prompt)
        instructions_seen.append(info.instructions or "")
        tools_seen.append({tool.name for tool in info.function_tools})
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            part.part_kind == "tool-return" for part in last.parts
        ):
            if any(
                "Flight confirmation" in str(part.content)
                for part in last.parts
                if part.part_kind == "tool-return"
            ):
                return reply("You have 2 unread emails: Flight confirmation and Weekly digest.")
            return reply("I added a Gmail setup card. Click Connect to sign in.")
        if prompt.startswith("Check my email"):
            return ModelResponse(
                parts=[ToolCallPart("setup_capability", {"capability": "gmail"}, tool_call_id="s1")]
            )
        if prompt.startswith(SETUP_WAKEUP):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_code",
                        {"code": "await GMAIL_FETCH_EMAILS()"},
                        tool_call_id="g1",
                    )
                ]
            )
        return reply()

    mode_ref["handler"] = handler

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        created = await client.post("/sessions")
        assert created.status_code == 201
        session_id = created.json()["session_id"]

        posted = await client.post(
            f"/sessions/{session_id}/messages",
            json={
                "text": "Check my email",
                "message_id": "m1",
                "origin": {"channel": "desktop", "client_id": "mac"},
            },
        )
        assert posted.status_code == 202
        await _settle(runner)

        assert runner.snapshot(session_id).pending_setup[0].capability == "gmail"

        # User authorizes in browser -> Mac app polls /check.
        runner.harness.policy.tools["GMAIL_FETCH_EMAILS"] = "allow"
        remote[0] = FunctionToolset([Tool(read_emails, name="GMAIL_FETCH_EMAILS")])
        found.accounts.return_value = {"gmail": "acc-gmail"}
        found.binding = AccountBinding("owner", 2, (("gmail", "acc-gmail"),))
        found.statuses.return_value = {"gmail": "ACTIVE"}

        checked = await client.post(f"/sessions/{session_id}/setup/gmail/check")
        assert checked.status_code == 200
        assert checked.json()["status"] == "connected"

        # Allow scheduled wakeup to run and finish without any manual follow-up.
        await asyncio.sleep(0)
        await _settle(runner)

        # Repeated polls after terminal state fail cleanly with 404 and trigger no duplicate runs.
        again = await client.post(f"/sessions/{session_id}/setup/gmail/check")
        assert again.status_code == 404
        await asyncio.sleep(0)
        await _settle(runner)

    await runner.stop()

    events = log.read(session_id)
    queued = [e.data for e in events if e.type == "run.queued"]
    assert [run["trigger"] for run in queued] == ["message", "setup"]
    assert queued[-1]["origin"] == {"channel": "desktop", "client_id": "mac"}
    assert queued[-1]["request"] is None
    assert "<surface>desktop</surface>" in prompts[-1]
    assert "<setup_updates>" in prompts[-1]
    assert '"gmail": "connected"' in prompts[-1]
    assert "Connected apps: gmail" in instructions_seen[-1]
    assert [e.data["text"] for e in events if e.type == "user.message"] == ["Check my email"]
    transcript = [turn.text for turn in runner.snapshot(session_id).transcript]
    assert transcript[-1] == "You have 2 unread emails: Flight confirmation and Weekly digest."


async def test_cancel_wakes_bud_once_with_cancellation_outcome(setup_wakeup_api):
    app, token, _, log, runner, _, mode_ref, prompts, _ = setup_wakeup_api
    runner._loop = asyncio.get_running_loop()

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        if prompt.startswith("<memory>"):
            return reply("none")
        prompts.append(prompt)
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            part.part_kind == "tool-return" for part in last.parts
        ):
            return reply("Connect Linear on the card to continue.")
        if prompt.startswith("Check my Linear"):
            return ModelResponse(
                parts=[
                    ToolCallPart("setup_capability", {"capability": "linear"}, tool_call_id="s1")
                ]
            )
        if prompt.startswith(SETUP_WAKEUP):
            return reply("Got it, I won't connect Linear. Ask if you want another option.")
        return reply()

    mode_ref["handler"] = handler

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        created = await client.post("/sessions")
        assert created.status_code == 201
        session_id = created.json()["session_id"]

        await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Check my Linear issues", "message_id": "m1"},
        )
        await _settle(runner)

        cancelled = await client.post(f"/sessions/{session_id}/setup/linear/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        await asyncio.sleep(0)
        await _settle(runner)

        # Repeated cancels are idempotent and must not trigger duplicate wakeups.
        repeat = await client.post(f"/sessions/{session_id}/setup/linear/cancel")
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "cancelled"
        await asyncio.sleep(0)
        await _settle(runner)

    await runner.stop()

    events = log.read(session_id)
    queued = [e.data for e in events if e.type == "run.queued"]
    assert [run["trigger"] for run in queued] == ["message", "setup"]
    assert '"linear": "cancelled"' in prompts[-1]
    assert "do not reopen it" in prompts[-1]
    assert runner.snapshot(session_id).pending_setup == []
    assert (
        runner.snapshot(session_id).transcript[-1].text
        == "Got it, I won't connect Linear. Ask if you want another option."
    )


async def test_setup_wakeup_when_run_already_active_queues_and_delivers_after(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    prompts: list[str] = []
    directory = (tmp_path / "active").resolve()
    directory.mkdir(parents=True)
    root = directory / "workspace"
    root.mkdir()
    log = EventLog(directory / "events.db")
    hub = Hub(log)
    journal = SetupJournal()
    state = {"connected": False}
    registry = SetupRegistry()
    registry.register(
        "gmail",
        SetupEntry(
            label="Gmail",
            kind="url",
            initiate=lambda: SetupInitiation(kind="url", url="https://example.test/gmail"),
            status=lambda: SetupStatus(state="connected" if state["connected"] else "waiting"),
        ),
    )

    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    model_registry = Registry(models=[spec])
    policy = Policy(tools={**dict.fromkeys(READ_TOOLS, "allow"), "setup_capability": "allow"})
    tools = [tool.function for tool in build_tools(root, directory / "claims.jsonl")]
    harness = Harness(
        FunctionToolset([*tools, *build_setup_toolset(registry, journal).tools.values()]),
        policy,
        model_registry,
        "Synthetic test harness.",
        state_dir=directory,
        result_path=log.path,
        read_tools=READ_TOOLS,
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        prompts.append(prompt)
        if prompt.startswith("First request"):
            return ModelResponse(
                parts=[ToolCallPart("setup_capability", {"capability": "gmail"}, tool_call_id="s1")]
            )
        if prompt.startswith("Second request"):
            return reply("Handled second request.")
        if prompt.startswith(SETUP_WAKEUP):
            return reply("Gmail is ready — continuing first request.")
        return reply("Added Gmail card.")

    gateway = Gateway(
        model_registry, Ledger(directory / "costs.db"), lambda _: scripted_model(respond)
    )
    runner = Runner(hub, gateway, harness, workspace=root)
    journal.bind(hub, on_wakeup=runner.wake_setup)
    try:
        log.append("chat", "session.created")
        runner.start("chat", "First request", "m1")
        await _settle(runner)

        # Hold the second run AFTER it writes run.started so the setup completes mid-turn.
        base_finish = runner._finish

        async def held_finish(*args, **kwargs):
            return base_finish(*args, **kwargs)

        base_agent_run = runner.agent.run

        async def held_agent_run(user_prompt, *args, **kwargs):
            if isinstance(user_prompt, str) and user_prompt.startswith("Second request"):
                entered.set()
                await release.wait()
            return await base_agent_run(user_prompt, *args, **kwargs)

        runner.agent.run = held_agent_run
        runner.start("chat", "Second request", "m2")
        await entered.wait()

        # Setup completes while the second run is still active.
        state["connected"] = True
        entry = registry.entry("gmail")
        assert entry is not None
        result = check_setup(entry, journal, "chat", "gmail", key="poll-1")
        assert result["status"] == "connected"
        assert len(runner.tasks) == 1  # did not collide with active run

        release.set()
        await _settle(runner)

        triggers = [e.data["trigger"] for e in log.read("chat") if e.type == "run.queued"]
        assert triggers == ["message", "message", "setup"]
        assert '"gmail": "connected"' in prompts[-1]
    finally:
        await runner.stop()
        gateway.close()


async def test_manual_followup_consumes_setup_update_without_duplicate_wakeup(tmp_path):
    directory = (tmp_path / "followup").resolve()
    directory.mkdir(parents=True)
    root = directory / "workspace"
    root.mkdir()
    log = EventLog(directory / "events.db")
    hub = Hub(log)
    journal = SetupJournal()
    registry = SetupRegistry()
    registry.register(
        "gmail",
        SetupEntry(
            label="Gmail",
            kind="url",
            initiate=lambda: SetupInitiation(kind="url", url="https://example.test/gmail"),
            status=lambda: SetupStatus(state="connected"),
        ),
    )
    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    model_registry = Registry(models=[spec])
    policy = Policy(tools={**dict.fromkeys(READ_TOOLS, "allow"), "setup_capability": "allow"})
    harness = Harness(
        FunctionToolset([tool.function for tool in build_tools(root, directory / "claims.jsonl")]),
        policy,
        model_registry,
        "Synthetic test harness.",
        state_dir=directory,
        result_path=log.path,
        read_tools=READ_TOOLS,
    )
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(_last_prompt(messages))
        return reply("Checked.")

    gateway = Gateway(
        model_registry, Ledger(directory / "costs.db"), lambda _: scripted_model(respond)
    )
    runner = Runner(hub, gateway, harness, workspace=root)
    journal.bind(hub, on_wakeup=runner.wake_setup)
    try:
        log.append("chat", "session.created")
        runner.start("chat", "Check my email", "m1")
        await _settle(runner)

        # Pause queue, complete setup, then owner manually sends "check" before resuming.
        runner.pause_queue("chat", True)
        journal.publish(
            "chat",
            "capability.setup",
            {"setup_id": "s-1", "capability": "gmail", "label": "Gmail", "status": "waiting"},
            key="setup-1",
        )
        entry = registry.entry("gmail")
        assert entry is not None
        result = check_setup(entry, journal, "chat", "gmail", key="check-1")
        assert result["status"] == "connected"
        assert len(log.pending_setup_wakeups("chat")) == 1

        runner.start("chat", "check", "m2")
        runner.pause_queue("chat", False)
        await _settle(runner)

        triggers = [e.data["trigger"] for e in log.read("chat") if e.type == "run.queued"]
        assert triggers == ["message", "message"]
        assert prompts[-1].startswith("check")
        assert '"gmail": "connected"' in prompts[-1]
        assert log.pending_setup_wakeups("chat") == []
        assert log.setup_wakeup_sessions() == []
    finally:
        await runner.stop()
        gateway.close()


async def test_restart_recovery_does_not_repeat_completed_wakeup_and_recovers_unstarted(tmp_path):
    directory = (tmp_path / "restart").resolve()
    directory.mkdir(parents=True)
    root = directory / "workspace"
    root.mkdir()
    log_path = directory / "events.db"
    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    model_registry = Registry(models=[spec])
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(_last_prompt(messages))
        return reply("Handled.")

    def make_runner():
        log = EventLog(log_path)
        hub = Hub(log)
        policy = Policy(tools=dict.fromkeys(READ_TOOLS, "allow"))
        harness = Harness(
            FunctionToolset(
                [tool.function for tool in build_tools(root, directory / "claims.jsonl")]
            ),
            policy,
            model_registry,
            "Synthetic test harness.",
            state_dir=directory,
            result_path=log_path,
            read_tools=READ_TOOLS,
        )
        gateway = Gateway(
            model_registry, Ledger(directory / "costs.db"), lambda _: scripted_model(respond)
        )
        return log, hub, Runner(hub, gateway, harness, workspace=root), gateway

    # Process 1: terminal status is recorded while runner has no callback (crash before drain).
    log1, _, runner1, gw1 = make_runner()
    try:
        log1.append("chat", "session.created")
        log1.append(
            "chat",
            "capability.setup",
            {"setup_id": "s-1", "capability": "gmail", "label": "Gmail", "status": "waiting"},
        )
        version = log1.read("chat")[-1].id
        assert log1.update_setup_status(
            "chat",
            {"capability": "gmail", "label": "Gmail", "status": "connected"},
            version,
            key="c1",
        )
        # Duplicate insert with same setup_id + outcome is ignored.
        with log1._connect() as db:
            log1._record_setup_wakeup(
                db,
                session_id="chat",
                setup_id="s-1",
                capability="gmail",
                label="Gmail",
                outcome="connected",
                status_seq=2,
                origin=None,
            )
        assert len(log1.pending_setup_wakeups("chat")) == 1
    finally:
        await runner1.stop()
        gw1.close()

    # Process 2: recover() drains the pending setup wakeup exactly once.
    log2, _, runner2, gw2 = make_runner()
    try:
        await runner2.recover()
        await _settle(runner2)
        triggers = [e.data["trigger"] for e in log2.read("chat") if e.type == "run.queued"]
        assert triggers == ["setup"]
        assert len(prompts) == 1
    finally:
        await runner2.stop()
        gw2.close()

    # Process 3: another restart emits zero duplicate wakeups.
    log3, _, runner3, gw3 = make_runner()
    try:
        await runner3.recover()
        await _settle(runner3)
        triggers = [e.data["trigger"] for e in log3.read("chat") if e.type == "run.queued"]
        assert triggers == ["setup"]
        assert len(prompts) == 1
    finally:
        await runner3.stop()
        gw3.close()


async def test_reattempted_setup_after_cancel_gets_new_setup_id_and_wakes_again(setup_wakeup_api):
    app, token, found, log, runner, _, mode_ref, prompts, _ = setup_wakeup_api
    runner._loop = asyncio.get_running_loop()

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        if prompt.startswith("<memory>"):
            return reply("none")
        prompts.append(prompt)
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            part.part_kind == "tool-return" for part in last.parts
        ):
            return reply("Setup card shown.")
        if prompt.startswith("Connect Gmail"):
            return ModelResponse(
                parts=[ToolCallPart("setup_capability", {"capability": "gmail"}, tool_call_id="s1")]
            )
        if prompt.startswith("Try Gmail again"):
            return ModelResponse(
                parts=[ToolCallPart("setup_capability", {"capability": "gmail"}, tool_call_id="s2")]
            )
        if prompt.startswith(SETUP_WAKEUP):
            return reply("Handled setup update.")
        return reply()

    mode_ref["handler"] = handler

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]

        await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Connect Gmail", "message_id": "m1"},
        )
        await _settle(runner)
        await client.post(f"/sessions/{session_id}/setup/gmail/cancel")
        await asyncio.sleep(0)
        await _settle(runner)

        await client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Try Gmail again", "message_id": "m2"},
        )
        await _settle(runner)
        found.statuses.return_value = {"gmail": "ACTIVE"}
        await client.post(f"/sessions/{session_id}/setup/gmail/check")
        await asyncio.sleep(0)
        await _settle(runner)

    await runner.stop()

    events = log.read(session_id)
    setup_ids = [e.data["setup_id"] for e in events if e.type == "capability.setup"]
    assert len(setup_ids) == 2
    assert setup_ids[0] != setup_ids[1]
    triggers = [e.data["trigger"] for e in events if e.type == "run.queued"]
    assert triggers == ["message", "setup", "message", "setup"]
    setup_prompts = [p for p in prompts if p.startswith(SETUP_WAKEUP)]
    assert len(setup_prompts) == 2
    assert '"gmail": "cancelled"' in setup_prompts[0]
    assert '"gmail": "connected"' in setup_prompts[1]


async def test_setup_wakeup_preserves_taint_and_approval_boundaries(tmp_path):
    directory = (tmp_path / "security").resolve()
    directory.mkdir(parents=True)
    root = directory / "workspace"
    root.mkdir()
    log = EventLog(directory / "events.db")
    hub = Hub(log)
    journal = SetupJournal()
    registry = SetupRegistry()
    registry.register(
        "gmail",
        SetupEntry(
            label="Gmail",
            kind="url",
            initiate=lambda: SetupInitiation(kind="url", url="https://example.test/gmail"),
            status=lambda: SetupStatus(state="connected"),
        ),
    )

    reviewer_calls = 0

    class StrictStubReviewer(ToolReviewer):
        async def review(self, request):
            nonlocal reviewer_calls
            reviewer_calls += 1
            raise AssertionError("Reviewer should not auto-approve a system-initiated wakeup.")

    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    model_registry = Registry(models=[spec])
    policy = Policy(
        tools={
            **dict.fromkeys(READ_TOOLS, "allow"),
            **dict.fromkeys(WRITE_TOOLS, "review"),
            "setup_capability": "allow",
        }
    )
    harness = Harness(
        FunctionToolset(
            [
                *[tool.function for tool in build_tools(root, directory / "claims.jsonl")],
                *build_setup_toolset(registry, journal).tools.values(),
            ]
        ),
        policy,
        model_registry,
        "Synthetic test harness.",
        state_dir=directory,
        result_path=log.path,
        read_tools=READ_TOOLS,
    )
    harness.reviewer = StrictStubReviewer(api_key="offline")

    seen_messages: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_messages.append(messages)
        prompt = _last_prompt(messages)
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            part.part_kind == "tool-return" for part in last.parts
        ):
            return reply("Waiting on setup.")
        if prompt.startswith("Connect Gmail"):
            return ModelResponse(
                parts=[ToolCallPart("setup_capability", {"capability": "gmail"}, tool_call_id="s1")]
            )
        if prompt.startswith(SETUP_WAKEUP):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "workspace_create",
                        {"path": "from-wakeup.txt", "content": "ok"},
                        tool_call_id="w1",
                    )
                ]
            )
        return reply()

    gateway = Gateway(
        model_registry, Ledger(directory / "costs.db"), lambda _: scripted_model(respond)
    )
    runner = Runner(hub, gateway, harness, workspace=root)
    journal.bind(hub, on_wakeup=runner.wake_setup)
    try:
        log.append("chat", "session.created")
        runner.start("chat", "Connect Gmail", "m1")
        await _settle(runner)

        entry = registry.entry("gmail")
        assert entry is not None
        result = check_setup(entry, journal, "chat", "gmail", key="check-1")
        assert result["status"] == "connected"
        await _settle(runner)

        # Even in review/auto mode, ActionGateway holds the write for owner approval.
        snap = runner.snapshot("chat")
        assert snap.status == "awaiting_approval"
        assert len(snap.pending_approvals) == 1
        assert snap.pending_approvals[0].tool == "workspace_create"
        assert reviewer_calls == 0

        # Taint check: wakeup prompt & <setup_updates> are read_text, never owner_text.
        last_msgs = seen_messages[-1]
        owner = owner_text(last_msgs)
        read = read_text(last_msgs)
        assert "Connect Gmail" in owner
        assert SETUP_WAKEUP not in owner
        assert "<setup_updates>" not in owner
        assert SETUP_WAKEUP in read
        assert "<setup_updates>" in read
    finally:
        await runner.stop()
        gateway.close()
