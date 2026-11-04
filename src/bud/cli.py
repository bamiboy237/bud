"""Command-line entry point for bud."""

import argparse
import asyncio
import webbrowser

import httpx
import uvicorn
from openai import OpenAIError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, UserError

from bud import __version__
from bud.config import get_settings
from bud.core.client import chat
from bud.evals.runner import evaluate
from bud.gateway import Ledger, NoModelAvailable, build_gateway
from bud.gateway.chatgpt import ChatGPTError, ChatGPTStore, login, refresh_models
from bud.gateway.errors import error_summary
from bud.gateway.registry import load_registry
from bud.world.store import World

HOST = "127.0.0.1"
PORT = 8420
DEFAULT_URL = f"http://{HOST}:{PORT}"


async def ask(prompt: str, tier: str, model_id: str | None) -> None:
    gateway = build_gateway(model_id=model_id)
    try:
        agent = Agent(gateway.model(tier))
        print((await agent.run(prompt)).output)
    finally:
        gateway.close()


async def models() -> None:
    settings = get_settings()
    store = ChatGPTStore(settings.data_dir)
    print(f"{'Model ID':<48} {'Billing':<14} Status")
    if store.connected:
        for model in await refresh_models(store):
            selected = model["slug"] == store.state().get("selected_model")
            print(
                f"{'openai-chatgpt:' + model['slug']:<48} {'subscription':<14} "
                f"{'default' if selected else 'available'}"
            )
    else:
        print("ChatGPT: not connected. Run `bud login chatgpt`.")
    keys = {
        "deepseek": settings.deepseek_api_key,
        "openai": settings.openai_api_key,
        "meta": settings.muse_api_key,
    }
    seen = set()
    for spec in load_registry().models:
        if spec.id not in seen:
            print(f"{spec.id:<48} {'API':<14} {'key set' if keys[spec.provider] else 'no key'}")
            seen.add(spec.id)


def costs() -> None:
    rows = Ledger(get_settings().data_dir / "bud.db").costs_by_day_and_model()
    if not rows:
        print("No requests recorded.")
        return
    print(
        f"{'Day':<11} {'Model':<38} {'Billing':<12} {'Reqs':>5} {'Failed':>6} "
        f"{'Input':>9} {'Output':>9} {'USD':>10} {'Unpriced':>8}"
    )
    for row in rows:
        usd = f"{row.cost_usd:.6f}" if row.billing == "api" else "plan"
        print(
            f"{row.day:<11} {row.model:<38} {row.billing:<12} {row.requests:>5} {row.failed:>6} "
            f"{row.input_tokens:>9} {row.output_tokens:>9} {usd:>10} "
            f"{row.unknown_cost:>8}"
        )
    print(f"\nMetered API total: ${sum(row.cost_usd for row in rows):.6f}")
    print("Subscription tokens use the ChatGPT plan; its subscription fee is not allocated here.")


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


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bud", description="bud, a personal AI teammate.")
    parser.add_argument("--version", action="version", version=f"bud {__version__}")
    commands = parser.add_subparsers(dest="command")

    ask_parser = commands.add_parser("ask", help="send one prompt through the gateway")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--tier", choices=["fast", "frontier"], default="fast")
    ask_parser.add_argument("--model", help="pin a model ID from `bud models`, without fallback")
    login_parser = commands.add_parser("login", help="sign in to a model provider")
    login_parser.add_argument("provider", choices=["chatgpt"])
    login_parser.add_argument("--no-browser", action="store_true", help="print a sign-in link")
    logout_parser = commands.add_parser("logout", help="remove local model-provider credentials")
    logout_parser.add_argument("provider", choices=["chatgpt"])
    commands.add_parser("models", help="list model IDs and refresh the ChatGPT account catalog")
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
    serve_parser.add_argument("--model", help="pin a model ID from `bud models`, without fallback")
    chat_parser = commands.add_parser("chat", help="open or attach to a session")
    chat_parser.add_argument("session", nargs="?", help="session id to attach to")
    chat_parser.add_argument("--url", default=DEFAULT_URL)

    args = parser.parse_args(argv)
    if args.command == "ask":
        asyncio.run(ask(args.prompt, args.tier, args.model))
    elif args.command == "login":
        asyncio.run(login(get_settings().data_dir, no_browser=args.no_browser))
    elif args.command == "logout":
        ChatGPTStore(get_settings().data_dir).logout()
        print("Local ChatGPT credentials removed.")
    elif args.command == "models":
        asyncio.run(models())
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
        app = create_app(simulate=args.simulate, model_id=args.model)
        uvicorn.run(app, host=HOST, port=PORT)
    elif args.command == "chat":
        asyncio.run(chat(args.url, args.session))
    else:
        parser.print_help()


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except (ChatGPTError, NoModelAvailable) as exc:
        raise SystemExit(str(exc)) from None
    except (ModelAPIError, UserError, OpenAIError, httpx.HTTPError, OSError, ExceptionGroup) as exc:
        raise SystemExit(error_summary(exc)) from None
