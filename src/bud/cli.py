"""Command-line entry point for bud."""

import argparse
import asyncio
import webbrowser

import uvicorn
from pydantic_ai import Agent

from bud import __version__
from bud.config import get_settings
from bud.core.client import chat
from bud.evals.runner import evaluate
from bud.gateway import Ledger, build_gateway
from bud.world.store import World

HOST = "127.0.0.1"
PORT = 8420
DEFAULT_URL = f"http://{HOST}:{PORT}"


def ask(prompt: str, tier: str) -> None:
    agent = Agent(build_gateway().model(tier))
    print(agent.run_sync(prompt).output)


def costs() -> None:
    rows = Ledger(get_settings().data_dir / "bud.db").costs_by_day_and_model()
    if not rows:
        print("No requests recorded.")
        return
    print(
        f"{'Day':<11} {'Model':<28} {'Reqs':>5} {'Failed':>6} {'Input':>9} {'Output':>9} "
        f"{'USD':>10} {'Unpriced':>8}"
    )
    for row in rows:
        print(
            f"{row.day:<11} {row.model:<28} {row.requests:>5} {row.failed:>6} "
            f"{row.input_tokens:>9} {row.output_tokens:>9} {row.cost_usd:>10.6f} "
            f"{row.unknown_cost:>8}"
        )
    print(f"\nTotal: ${sum(row.cost_usd for row in rows):.6f}")


def world(action: str) -> None:
    store = World(get_settings().data_dir / "world.db")
    if action == "reset":
        store.reset()
        print("World reset to its seed.")
        return
    state = store.snapshot()
    print(f"Now: {store.now}. {len(state['messages'])} messages, {len(state['events'])} events.")
    print("\nSent or drafted by bud:")
    for message in state["messages"]:
        if message["id"].startswith(("t-", "draft-")):
            print(f"  [{message['folder']}] to {message['recipients']}: {message['body'][:70]}")
    print("\nCalendar:")
    for event in state["events"]:
        print(f"  {event['start']} to {event['end'][11:16]}  {event['title']}")


def connect(toolkit: str, no_browser: bool) -> None:
    from bud.composio.connection import ConnectionError, Connections

    try:
        url = Connections(get_settings()).connect(toolkit)
    except ConnectionError as exc:
        raise SystemExit(str(exc)) from None
    print(url)
    if not no_browser:
        webbrowser.open(url)
    print("Sign in at this URL, then run `bud connections` to check the connection.")


def connections() -> None:
    from bud.composio.connection import ConnectionError, Connections

    try:
        statuses = Connections(get_settings()).statuses()
    except ConnectionError as exc:
        raise SystemExit(str(exc)) from None
    for toolkit in ("gmail", "googlecalendar"):
        print(f"{toolkit}: {statuses.get(toolkit, 'NOT_CONNECTED')}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bud", description="bud, a personal AI teammate.")
    parser.add_argument("--version", action="version", version=f"bud {__version__}")
    commands = parser.add_subparsers(dest="command")

    ask_parser = commands.add_parser("ask", help="send one prompt through the gateway")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--tier", choices=["fast", "frontier"], default="fast")
    commands.add_parser("costs", help="print spend by day and model")
    world_parser = commands.add_parser("world", help="inspect or reset the simulated world")
    world_parser.add_argument("action", choices=["show", "reset"])
    connect_parser = commands.add_parser("connect", help="connect an account through Composio")
    connect_parser.add_argument("toolkit", choices=["gmail", "calendar"])
    connect_parser.add_argument(
        "--no-browser", action="store_true", help="only print the sign-in URL"
    )
    commands.add_parser("connections", help="show connection status metadata, not mailbox data")
    eval_parser = commands.add_parser("eval", help="run the task suite on the simulated world")
    eval_parser.add_argument("--trials", type=int, default=3)
    eval_parser.add_argument("--task", help="run one task by id")
    serve_parser = commands.add_parser("serve", help="run the core server on localhost")
    serve_parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the simulated inbox and calendar instead of real accounts",
    )
    chat_parser = commands.add_parser("chat", help="open or attach to a session")
    chat_parser.add_argument("session", nargs="?", help="session id to attach to")
    chat_parser.add_argument("--url", default=DEFAULT_URL)

    args = parser.parse_args(argv)
    if args.command == "ask":
        ask(args.prompt, args.tier)
    elif args.command == "costs":
        costs()
    elif args.command == "world":
        world(args.action)
    elif args.command == "connect":
        connect("googlecalendar" if args.toolkit == "calendar" else args.toolkit, args.no_browser)
    elif args.command == "connections":
        connections()
    elif args.command == "eval":
        evaluate(args.trials, args.task)
    elif args.command == "serve":
        from bud.core.server import create_app

        mode = "simulated tools" if args.simulate else "real accounts"
        print(f"Starting bud with {mode} at {DEFAULT_URL}.", flush=True)
        uvicorn.run(lambda: create_app(simulate=args.simulate), factory=True, host=HOST, port=PORT)
    elif args.command == "chat":
        asyncio.run(chat(args.url, args.session))
    else:
        parser.print_help()
