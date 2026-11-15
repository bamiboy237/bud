"""Offline entry point for the real HTTP server used by test_server."""

import argparse
import os
import socket
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import uvicorn
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from bud.config import Settings, get_settings
from bud.core import server
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from tests.offline import offline_requests

DRAFT = "Draft for approval.\n"
REVISED = "Revised and approved.\n"
FILENAME = "report.md"


def scripted_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    parts = [part for message in messages for part in message.parts]
    tools = {tool.name for tool in info.function_tools}
    outputs = {tool.name for tool in info.output_tools}
    if not tools and not outputs:
        return ModelResponse(parts=[TextPart('{"lessons": []}')])
    prompt_index, prompt = next(
        (
            (index, part.content)
            for index, part in reversed(list(enumerate(parts)))
            if isinstance(part, UserPromptPart)
            and isinstance(part.content, str)
            and not part.content.startswith("<system>")
        ),
        (-1, ""),
    )
    current = parts[prompt_index + 1 :]
    returns = [part for part in current if isinstance(part, ToolReturnPart)]

    def call(name: str, args: dict) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(name, args, tool_call_id=f"script-{name}-{len(parts)}")]
        )

    if "finish_job" in outputs:
        written = next(
            (part for part in reversed(returns) if part.tool_name == "workspace_create"), None
        )
        if written is not None or "workspace_create" not in tools:
            result = written.content if written is not None else {}
            success = isinstance(result, dict) and result.get("ok") is True
            return call(
                "finish_job",
                {
                    "summary": "Saved report.md." if success else "No file was saved.",
                    "missing": [] if success else ["The write was denied or replaced."],
                },
            )
        return call(
            "workspace_create",
            {"path": FILENAME, "content": REVISED if "revised" in prompt.lower() else DRAFT},
        )
    if returns:
        return ModelResponse(parts=[TextPart("Job 1 is ready for your review.")])
    if prompt.startswith("Save"):
        return call(
            "start_task",
            {
                "title": "Save report",
                "brief": {
                    "goal": prompt,
                    "inputs": [],
                    "expected_result": "A report.md workspace file.",
                    "expected_items": ["The requested report text"],
                    "deliver_file": True,
                },
            },
        )
    return ModelResponse(parts=[TextPart("Offline reply.")])


async def scripted_stream(messages: list[ModelMessage], info: AgentInfo):
    response = scripted_response(messages, info)
    for index, part in enumerate(response.parts):
        if isinstance(part, ToolCallPart):
            yield {
                index: DeltaToolCall(
                    name=part.tool_name,
                    json_args=part.args_as_json_str(),
                    tool_call_id=part.tool_call_id,
                )
            }
        elif isinstance(part, TextPart):
            yield part.content


def offline_gateway(
    settings: Settings | None = None,
    *,
    providers: set[str] | None = None,
    model_id: str | None = None,
) -> Gateway:
    settings = settings or get_settings()
    spec = ModelSpec(
        provider="offline",
        name="script",
        tier="fast",
        billing="local",
        base_url="http://127.0.0.1:1",
        protocol="chat",
        supports_tools=True,
    )
    model = FunctionModel(scripted_response, stream_function=scripted_stream, model_name=spec.id)
    return Gateway(
        Registry(models=[spec]),
        Ledger(settings.data_dir / "bud.db"),
        lambda _: model,
        fallback=False,
    )


def checkpoint_dispatch(stack: ExitStack, data_dir: Path, checkpoint: str) -> None:
    """Block at a real workspace boundary until the test kills this process."""
    from bud import workspace

    def reached() -> None:
        (data_dir / "checkpoint").write_text(checkpoint)
        threading.Event().wait()

    if checkpoint == "before_dispatch":
        directory = workspace._directory

        @contextmanager
        def before_directory(path):
            if path == data_dir:
                reached()
            with directory(path) as fd:
                yield fd

        stack.enter_context(patch.object(workspace, "_directory", before_directory))
    elif checkpoint == "after_claim":
        fsync = os.fsync
        ledger = data_dir / "workspace-dispatches.jsonl"

        def after_fsync(fd):
            fsync(fd)
            if ledger.exists():
                actual, expected = os.fstat(fd), ledger.stat()
                if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                    reached()

        stack.enter_context(patch.object(workspace.os, "fsync", after_fsync))
    elif checkpoint == "after_write":
        saved = workspace._saved

        def after_saved(*args):
            reached()
            return saved(*args)

        stack.enter_context(patch.object(workspace, "_saved", after_saved))
    else:
        raise ValueError(checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--checkpoint", choices=("before_dispatch", "after_claim", "after_write"))
    args = parser.parse_args()
    with ExitStack() as stack:
        stack.enter_context(offline_requests())
        # The child also refuses dotenv input when run outside pytest.
        Settings.model_config["env_file"] = None
        get_settings.cache_clear()
        stack.enter_context(patch.object(server, "build_gateway", offline_gateway))
        app = server.create_app(simulate=True)
        if args.checkpoint:
            checkpoint_dispatch(stack, get_settings().data_dir, args.checkpoint)
        with socket.socket(fileno=args.fd) as listener:
            uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(
                sockets=[listener]
            )


if __name__ == "__main__":
    main()
