"""Wraps a Pydantic AI model so each request writes one ledger entry.
The entry is `ok`, `failed`, or `incomplete` when the stream ends before usage arrives.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from bud.gateway.errors import error_summary
from bud.gateway.ledger import Ledger, LedgerEntry
from bud.gateway.registry import ModelSpec


@dataclass(init=False)
class TrackedModel(WrapperModel):
    spec: ModelSpec
    ledger: Ledger
    session_id: str | None

    def __init__(
        self, wrapped: Model, spec: ModelSpec, ledger: Ledger, session_id: str | None = None
    ) -> None:
        super().__init__(wrapped)
        self.spec = spec
        self.ledger = ledger
        self.session_id = session_id

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        started = perf_counter()
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except BaseException as exc:
            self._record_failure(exc, started)
            raise
        self._record_usage(response.usage, started)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        started = perf_counter()
        try:
            async with super().request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
        except BaseException as exc:
            self._record_failure(exc, started)
            raise
        self._record_usage(stream.usage, started)

    def _record_usage(self, usage: RequestUsage, started: float) -> None:
        # Usage arrives at the end of a stream, so a stream the caller abandoned reports nothing.
        if usage.total_tokens == 0:
            self._write("incomplete", started)
            return
        self._write(
            "ok",
            started,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens,
            reasoning_tokens=usage.details.get("reasoning_tokens"),
            cost_usd=self.spec.cost(usage),
        )

    def _record_failure(self, exc: BaseException, started: float) -> None:
        self._write("failed", started, error=error_summary(exc))

    def _write(self, status: str, started: float, **fields: Any) -> None:
        self.ledger.record(
            LedgerEntry(
                model=self.spec.id,
                billing=self.spec.billing,
                status=status,
                latency_ms=(perf_counter() - started) * 1000,
                session_id=self.session_id,
                **fields,
            )
        )
