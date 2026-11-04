"""Terminal client for a running bud server.
It streams a session's events over server-sent events, prints them, and sends each line the
user types as a message.
"""

import asyncio
import json
from uuid import uuid4

import httpx


def render(event: dict, seen_requests: set[str]) -> None:
    data = event["data"]
    match event["type"]:
        case "user.message":
            print(f"\nyou: {data['text']}")
        case "run.started":
            print("bud: ", end="", flush=True)
        case "run.recovered":
            print("\n[recovering interrupted work]", flush=True)
        case "text.reset":
            request = data["request"]
            if request in seen_requests:
                print(
                    "\n[discard the partial response above; generating its replacement]\nbud: ",
                    end="",
                    flush=True,
                )
            seen_requests.add(request)
        case "text.delta":
            print(data["text"], end="", flush=True)
        case "tool.call":
            print(f"\n[tool] {data['tool']}({data['args']})", flush=True)
        case "tool.result":
            print(f"[result] {data['result'][:200]}", flush=True)
        case "approval.requested":
            print(f"\n[approval needed] {data['tool']}({data['args']})", flush=True)
            print("Type /approve or /deny.", flush=True)
        case "approval.granted":
            print("[approved]", flush=True)
        case "approval.denied":
            print("[denied]", flush=True)
        case "run.waiting" | "run.completed":
            print(flush=True)
        case "run.failed":
            print(f"\n[run failed] {data['reason']}", flush=True)


async def follow(client: httpx.AsyncClient, session_id: str, pending: set[str]) -> None:
    last_seq = 0
    seen_requests: set[str] = set()
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
                        render(event, seen_requests)
                        if event["type"] == "approval.requested":
                            pending.add(event["data"]["approval_id"])
                        elif event["type"] in ("approval.granted", "approval.denied"):
                            pending.discard(event["data"]["approval_id"])
        except httpx.TransportError:
            print("\n[reconnecting]", flush=True)
            await asyncio.sleep(1)


async def resolve(
    client: httpx.AsyncClient, session_id: str, pending: set[str], approved: bool
) -> None:
    if not pending:
        print("[nothing is waiting for approval]")
    for approval_id in list(pending):
        response = await client.post(
            f"/sessions/{session_id}/approvals/{approval_id}", json={"approved": approved}
        )
        response.raise_for_status()


async def chat(base_url: str, session_id: str | None) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        if session_id is None:
            response = await client.post("/sessions")
            response.raise_for_status()
            session_id = response.json()["session_id"]
        print(f"session {session_id}\nCtrl-D to leave.")

        pending: set[str] = set()
        follower = asyncio.create_task(follow(client, session_id, pending))
        try:
            while True:
                text = await asyncio.to_thread(input)
                if not text.strip():
                    continue
                if text.strip() in ("/approve", "/deny"):
                    await resolve(client, session_id, pending, text.strip() == "/approve")
                    continue
                response = await client.post(
                    f"/sessions/{session_id}/messages",
                    json={"text": text, "message_id": uuid4().hex},
                )
                if response.status_code == 409:
                    print(f"[{response.json()['detail']}]")
                else:
                    response.raise_for_status()
        except EOFError:
            pass
        finally:
            follower.cancel()
