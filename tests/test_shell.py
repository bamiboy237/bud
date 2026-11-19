"""bud's sandboxed shell: writes stay in the workspace, secrets stay out, and the network
goes through a proxy that asks the decision model about unfamiliar sites."""

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bud import shell
from bud.harness.review import ReviewResult, ToolReviewer, Verdict
from bud.shell import NetworkGrants, Proxy, build_tools, judge_site, run

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
