"""bud's sandboxed shell: writes stay in the workspace, secrets stay out, and the network
goes through a proxy that asks the decision model about unfamiliar sites."""

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import FunctionToolset

from bud import shell
from bud.config import Settings
from bud.core.server import allow_file_tools, private_folders
from bud.harness.actions import ActionGateway, Policy
from bud.harness.review import ReviewResult, ToolReviewer, Verdict
from bud.shell import NetworkGrants, Proxy, build_tools, judge_site, run
from tests.test_taint import call, ctx

macos = pytest.mark.skipif(not shell.available(), reason="needs the macOS sandbox")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    return root


async def never(host: str) -> bool:
    return False


@macos
async def test_a_command_writes_only_inside_the_workspace_and_sees_no_secrets(
    workspace, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    outside = Path("/private/tmp") / f"bud-shell-{uuid.uuid4().hex}"
    home = Path.home().resolve()
    command = (
        "echo made > made.txt; "
        f"echo escaped > {outside}; "
        f"ls {home} > listing.txt 2>&1; "
        'echo "key=${OPENAI_API_KEY:-none}"'
    )

    result = await run(workspace, command, 30, never)

    assert (workspace / "made.txt").read_text() == "made\n"
    assert not outside.exists()
    assert "not permitted" in (workspace / "listing.txt").read_text()
    assert "key=none" in result["stdout"]


@macos
async def test_a_slow_command_is_stopped(workspace):
    result = await run(workspace, "sleep 30", 1, never)
    assert result["timed_out"] == "Stopped after 1 seconds."
    assert result["exit_code"] != 0


@macos
async def test_a_child_a_finished_command_left_behind_is_stopped(workspace):
    # The child closes its output, so the command returns while the child still runs.
    result = await run(workspace, "sleep 30 >/dev/null 2>&1 & echo $!", 10, never)
    child = int(result["stdout"].strip())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


@macos
async def test_a_cancelled_command_is_stopped_and_reaped(workspace):
    task = asyncio.create_task(run(workspace, "sleep 30", 60, never))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def serve_once(body: bytes) -> tuple[asyncio.Server, int]:
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@macos
async def test_the_network_goes_through_the_proxy_and_its_judge(workspace):
    server, port = await serve_once(b"hello from the site")
    asked: list[str] = []

    async def allow(host: str) -> bool:
        asked.append(host)
        return True

    try:
        url = f"http://127.0.0.1:{port}/"
        allowed = await run(workspace, f"curl -sS -m 5 {url}", 30, allow)
        blocked = await run(workspace, f"curl -sS -m 5 {url}", 30, never)
        direct = await run(workspace, f"curl -sS -m 5 --noproxy '*' {url}", 30, allow)
    finally:
        server.close()

    assert allowed["stdout"] == "hello from the site"
    assert asked == ["127.0.0.1"]
    assert blocked["blocked_sites"] == ["127.0.0.1"]
    assert "hello" not in blocked["stdout"]
    # Without the proxy the sandbox has no network at all.
    assert "hello" not in direct["stdout"]
    assert direct["exit_code"] != 0


@macos
async def test_local_compute_does_not_ask_and_cannot_use_a_granted_site(workspace, tmp_path):
    grants = NetworkGrants(tmp_path / "network-grants.json")
    grants.add("127.0.0.1")
    policy = Policy(tools={})
    allow_file_tools(policy)
    tools = build_tools(workspace, grants, lambda: None)
    gate = ActionGateway(FunctionToolset(tools), policy=policy, record=lambda decision: None)
    server, port = await serve_once(b"network access")
    context = ctx("Read my screenshot", "The examples used CommandLine.arguments and 127.0.0.1.")
    command = f"printf '%s\\n' CommandLine.arguments; curl -sS -m 2 http://127.0.0.1:{port}/"
    try:
        with pytest.raises(ApprovalRequired):
            await call(gate, "run_command", {"command": command}, context)
        result = await call(gate, "run_local_command", {"command": command}, context)
    finally:
        server.close()

    assert result["stdout"] == "CommandLine.arguments\n"
    assert result["exit_code"] != 0
    assert "blocked_sites" not in result


async def test_registries_pass_and_each_other_site_is_judged_once():
    asked: list[str] = []

    async def judge(host: str) -> bool:
        asked.append(host)
        await asyncio.sleep(0)
        return host == "docs.example.com"

    proxy = Proxy(judge)
    assert await proxy.allowed("pypi.org")
    assert await proxy.allowed("files.pythonhosted.org")
    results = await asyncio.gather(*(proxy.allowed("docs.example.com") for _ in range(3)))
    assert results == [True, True, True]
    assert not await proxy.allowed("tracker.example.net")
    assert asked == ["docs.example.com", "tracker.example.net"]
    assert proxy.blocked == {"tracker.example.net"}


class FakeReviewer:
    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        self.requests = []

    async def review(self, request):
        self.requests.append(request)
        return ReviewResult(verdict=self.verdict, reason="")


def reviewer(verdict: Verdict) -> ToolReviewer:
    return cast(ToolReviewer, FakeReviewer(verdict))


async def test_the_decision_model_sees_the_site_command_and_request():
    fake = FakeReviewer(Verdict.ALLOW)
    assert await judge_site(
        cast(ToolReviewer, fake), "api.weather.gov", "curl api.weather.gov", "Weather?"
    )
    [request] = fake.requests
    assert request.tool == "network_access"
    assert request.args == {"host": "api.weather.gov", "command": "curl api.weather.gov"}
    assert request.owner_request == "Weather?"

    assert not await judge_site(reviewer(Verdict.PROMPT), "x.example", "curl x", "Hi")
    assert not await judge_site(reviewer(Verdict.ERROR), "x.example", "curl x", "Hi")
    assert not await judge_site(None, "x.example", "curl x", "Hi")
    # No owner words to judge against: a background brief is not the owner speaking.
    assert not await judge_site(reviewer(Verdict.ALLOW), "x.example", "curl x", "")


@macos
async def test_a_site_the_owner_allowed_skips_the_judge(workspace, tmp_path):
    grants = NetworkGrants(tmp_path / "network-grants.json")
    fake = FakeReviewer(Verdict.PROMPT)
    tools: dict[str, Any] = {
        tool.name: tool.function
        for tool in build_tools(workspace, grants, lambda: cast(ToolReviewer, fake))
    }
    run_command, allow_network = tools["run_command"], tools["allow_network"]
    assert allow_network("127.0.0.1", "The local test site") == (
        "Shell commands may now reach 127.0.0.1."
    )
    assert "Give only the host name" in allow_network("http://x.example/a", "no")
    server, port = await serve_once(b"granted")
    ctx = SimpleNamespace(metadata={"bud_request": "Fetch the local page"})
    try:
        result = await run_command(ctx, f"curl -sS -m 5 http://127.0.0.1:{port}/", 30)
    finally:
        server.close()
    assert result["stdout"] == "granted"
    assert fake.requests == []
    assert os.stat(grants.path).st_size > 0


@pytest.mark.parametrize(
    "authority, expected",
    [
        ("example.com:443", ("example.com", 443)),
        ("Example.COM.", ("example.com", 443)),
        ("[::1]:8080", ("::1", 8080)),
        ("127.0.0.1", ("127.0.0.1", 443)),
    ],
)
def test_site_reads_host_and_port(authority, expected):
    assert shell.site(authority, 443) == expected


@pytest.mark.parametrize(
    "authority",
    [
        "user:pass@example.com",
        "example.com?q=1",
        "example.com/x",
        "evil.com#api.weather.gov",
        "example.com:0",
        "a b",
        ":443",
        "x:99999",
    ],
)
def test_site_refuses_anything_but_a_plain_host(authority):
    with pytest.raises(ValueError):
        shell.site(authority, 443)


async def test_a_plain_http_connection_carries_one_checked_request():
    seen: list[bytes] = []

    async def answer(reader, writer):
        seen.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    asked: list[str] = []

    async def judge(host: str) -> bool:
        asked.append(host)
        return host == "127.0.0.1"

    proxy = Proxy(judge)
    proxy_port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"GET http://127.0.0.1:{port}/a?b=1 HTTP/1.1\r\nHost: x\r\nConnection: keep-alive"
            "\r\n\r\nGET http://other.example/ HTTP/1.1\r\nHost: other.example\r\n\r\n".encode()
        )
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 5)
        writer.close()
    finally:
        await proxy.close()
        upstream.close()

    assert reply.endswith(b"ok")
    [request] = seen
    assert request.startswith(b"GET /a?b=1 HTTP/1.1\r\n")
    assert b"Connection: close" in request
    assert b"keep-alive" not in request
    assert b"other.example" not in request
    assert asked == ["127.0.0.1"]


@macos
async def test_private_folders_and_launching_programs_are_refused(workspace, tmp_path):
    private = tmp_path / "data"
    private.mkdir()
    (private / ".env").write_text("OPENAI_API_KEY=sk-secret")
    marker = tmp_path / "launched"
    label = f"bud.test.{uuid.uuid4().hex}"
    command = (
        f"cat {private}/.env; "
        f"launchctl submit -l {label} -- /usr/bin/touch {marker}; "
        "cp /usr/bin/open ./o && ./o -g /System/Applications/Calculator.app; echo open=$?"
    )
    try:
        result = await run(workspace, command, 30, never, [private])
        await asyncio.sleep(1)
    finally:
        os.system(f"launchctl remove {label} 2>/dev/null")
    assert "sk-secret" not in result["stdout"]
    assert not marker.exists()
    assert "open=0" not in result["stdout"]


@macos
async def test_a_cancelled_command_is_stopped(workspace):
    task = asyncio.ensure_future(run(workspace, "sleep 2; touch finished", 30, never))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)
    assert not (workspace / "finished").exists()


@macos
async def test_https_goes_through_connect_and_an_allowed_site_is_remembered(workspace, tmp_path):
    server, port = await serve_once(b"tunnelled")
    fake = FakeReviewer(Verdict.ALLOW)
    tools: dict[str, Any] = {
        tool.name: tool.function
        for tool in build_tools(
            workspace, NetworkGrants(tmp_path / "grants.json"), lambda: cast(ToolReviewer, fake)
        )
    }
    ctx = SimpleNamespace(metadata={"bud_request": "Fetch the local page twice"})
    url = f"http://127.0.0.1:{port}/"
    try:
        # -p sends CONNECT through the proxy, the way HTTPS does.
        first = await tools["run_command"](ctx, f"curl -sS -m 5 -p {url}", 30)
        server.close()
        server, port = await serve_once(b"again")
        second = await tools["run_command"](ctx, f"curl -sS -m 5 -p http://127.0.0.1:{port}/", 30)
    finally:
        server.close()
    assert first["stdout"] == "tunnelled"
    assert second["stdout"] == "again"
    assert len(fake.requests) == 1


def test_running_commands_never_asks_and_each_new_site_always_does():
    policy = Policy(tools={})
    allow_file_tools(policy)
    assert policy.decide("run_command") == "allow"
    assert policy.decide("allow_network") == "ask"
    assert not policy.grantable("allow_network")


async def proxy_reply(request: bytes) -> bytes:
    async def allow(host: str) -> bool:
        return True

    proxy = Proxy(allow)
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=1 << 20)
        writer.write(request)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return reply
    finally:
        await proxy.close()


@pytest.mark.parametrize(
    "headers",
    [
        "Transfer-Encoding: chunked\r\n",
        "Content-Length: 5\r\nContent-Length: 0\r\n",
        "Content-Length\r\n",
        "X-Big: " + "a" * 70_000 + "\r\n",
    ],
    ids=["chunked", "two-lengths", "no-colon", "oversized"],
)
async def test_malformed_plain_http_requests_are_refused(headers):
    request = f"POST http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode()
    assert (await proxy_reply(request)).startswith(b"HTTP/1.1 400")


def test_shell_commands_cannot_read_bud_data_or_the_folder_it_runs_from(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    assert private_folders(settings) == [tmp_path / "data", Path.cwd()]


def test_private_folders_omits_home_cwd_and_protects_bud_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "owner"
    fake_home.mkdir()
    bud_home = tmp_path / "bud-runtime"
    bud_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(fake_home)
    monkeypatch.setenv("BUD_HOME", str(bud_home))
    settings = Settings(data_dir=bud_home / "data")
    folders = private_folders(settings)
    assert settings.data_dir in folders
    assert fake_home not in folders
    assert bud_home in folders


@macos
async def test_a_background_command_reaches_only_allowed_sites(workspace, tmp_path):
    """build_background takes no reviewer: a site the owner allowed works, and any other site
    is blocked, since nobody is there to judge it hours later."""
    from bud.core.server import build_background

    server, port = await serve_once(b"hello from the site")
    background = build_background(workspace, tmp_path)
    assert background.run_shell is not None
    url = f"http://127.0.0.1:{port}/"
    try:
        blocked = await background.run_shell(f"curl -sS -m 5 {url}", 30, "fetch it")
        NetworkGrants(tmp_path / "network-grants.json").add("127.0.0.1")
        allowed = await background.run_shell(f"curl -sS -m 5 {url}", 30, "fetch it")
    finally:
        server.close()
    assert blocked["blocked_sites"] == ["127.0.0.1"]
    assert allowed["stdout"] == "hello from the site"


def test_starting_background_work_offers_no_always_allow():
    from bud.core.server import allow_file_tools
    from bud.harness.actions import Policy

    policy = Policy(tools={})
    allow_file_tools(policy)
    assert {"remind_me", "run_in_background"} <= policy.no_grants
