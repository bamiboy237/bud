import builtins
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UserError

from bud.config import Settings
from bud.tracing import tracing


def test_tracing_without_token_does_not_import_sdk(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "logfire"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with tracing(Settings()):
        pass


def test_explicit_tracing_without_extra_gives_setup_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "logfire", None)
    with (
        pytest.raises(UserError, match="uv sync --locked --extra tracing"),
        tracing(Settings(logfire_token="test-only-token")),
    ):
        pytest.fail("Missing SDK must stop explicit tracing setup")


def test_tracing_omits_content_and_shuts_down_on_failure(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        configure=lambda **options: calls.append(("configure", options)),
        instrument_pydantic_ai=lambda **options: calls.append(("instrument", options)),
        shutdown=lambda **options: calls.append(("shutdown", options)),
    )
    monkeypatch.setitem(sys.modules, "logfire", fake)
    with (
        pytest.raises(RuntimeError, match="server failed"),
        tracing(Settings(logfire_token="test-only-token")),
    ):
        raise RuntimeError("server failed")
    assert calls == [
        (
            "configure",
            {
                "token": "test-only-token",
                "send_to_logfire": True,
                "service_name": "bud",
                "console": False,
                "inspect_arguments": False,
            },
        ),
        ("instrument", {"include_content": False, "include_binary_content": False}),
        ("shutdown", {"timeout_millis": 2000}),
    ]


async def test_real_traces_export_usage_without_prompt_tool_or_reply_content(tmp_path, monkeypatch):
    import logfire
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent, Tool
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    exporter = InMemorySpanExporter()
    configure = logfire.configure

    def local_configuration(**options):
        return configure(
            send_to_logfire=False,
            console=False,
            inspect_arguments=False,
            data_dir=tmp_path / "tracing",
            metrics=False,
            additional_span_processors=[SimpleSpanProcessor(exporter)],
        )

    monkeypatch.setattr(logfire, "configure", local_configuration)
    marker = "private-content-must-not-be-exported"

    def respond(messages, info):
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(
                parts=[TextPart(marker)], usage=RequestUsage(input_tokens=20, output_tokens=10)
            )
        return ModelResponse(
            parts=[ToolCallPart("lookup", {"query": marker}, tool_call_id="lookup-1")],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    def lookup(query: str) -> str:
        return marker + query

    with tracing(Settings(logfire_token="test-only-token")):
        agent = Agent(FunctionModel(respond), tools=[Tool(lookup)])
        assert (await agent.run(marker)).output == marker
    spans = exporter.get_finished_spans()
    assert any("gen_ai.usage.input_tokens" in (span.attributes or {}) for span in spans)
    assert any("lookup" in span.name for span in spans)
    assert marker not in json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [dict(event.attributes or {}) for event in span.events],
            }
            for span in spans
        ],
        default=str,
    )
