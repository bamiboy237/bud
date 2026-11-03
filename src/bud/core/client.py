"""Terminal client for a running bud server.
It streams a session's events over server-sent events, prints them, and sends each line the
user types as a message.
"""

import asyncio
import json

import httpx

TERMINAL = ("run.completed", "run.failed")


def render(event: dict) -> None:
    data = event["data"]
    match event["type"]:
        case "user.message":
            print(f"\nyou: {data['text']}")
        case "run.started":
            print("bud: ", end="", flush=True)
        case "text.delta":
            print(data["text"], end="", flush=True)
        case "tool.call":
            print(f"\n[tool] {data['tool']}({data['args']})", flush=True)
        case "tool.result":
            print(f"[result] {data['result'][:200]}", flush=True)
        case "run.completed":
            print(flush=True)
        case "run.failed":
            print(f"\n[run failed] {data['reason']}", flush=True)


async def follow(client: httpx.AsyncClient, session_id: str) -> None:
    last_seq = 0
    while True:
        try:
            async with client.stream(
                "GET",
                f"/sessions/{session_id}/events",
                headers={"Last-Event-ID": str(last_seq)},
                timeout=httpx.Timeout(5, read=None),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line.removeprefix("data: "))
                        last_seq = event["seq"]
                        render(event)
        except httpx.TransportError:
            print("\n[reconnecting]", flush=True)
            await asyncio.sleep(1)


async def chat(base_url: str, session_id: str | None) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        if session_id is None:
            response = await client.post("/sessions")
            response.raise_for_status()
            session_id = response.json()["session_id"]
        print(f"session {session_id}\nCtrl-D to leave.")

        follower = asyncio.create_task(follow(client, session_id))
        try:
            while True:
                text = await asyncio.to_thread(input)
                if not text.strip():
                    continue
                response = await client.post(
                    f"/sessions/{session_id}/messages", json={"text": text}
                )
                if response.status_code == 409:
                    print("[a run is already active]")
                else:
                    response.raise_for_status()
        except EOFError:
            pass
        finally:
            follower.cancel()
