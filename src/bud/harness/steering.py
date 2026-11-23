"""Mid-run steering capability and stream preemption for active agent runs."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import (
    BaseToolCallPart,
    FinalResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    PartEndEvent,
    PartStartEvent,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from bud.gateway.tracked import request_attribution, request_session


@dataclass
class SteerSignal:
    """In-memory wakeup and stream-abort signal for one active run."""

    run_id: str
    abort_stream: asyncio.Event = field(default_factory=asyncio.Event)
    in_model_call: bool = False
    model_steps: int = 0


ClaimSteer = Callable[[str, str], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]]
GetSignal = Callable[[str, str | None], SteerSignal | None]


def _make_steer_request(claimed: list[dict[str, Any]]) -> ModelRequest:
    text = "\n\n".join(str(item["text"]) for item in claimed)
    ids = [str(item["message_id"]) for item in claimed if item.get("message_id")]
    return ModelRequest(
        parts=[UserPromptPart(text)],
        metadata={"bud_steer": True, "bud_steer_ids": ids, "bud_steer_count": len(claimed)},
    )


def _merge_owner_request(existing: Any, steer_text: str) -> str:
    if isinstance(existing, str) and existing.strip():
        return f"{existing}\n\n{steer_text}"
    return steer_text


_IN_STEERABLE_STREAM: ContextVar[bool] = ContextVar("bud_in_steerable_stream", default=False)


class _AbortableStreamedResponse(StreamedResponse):
    """Wraps a provider stream so a mid-call steer aborts text streaming cleanly."""

    def __init__(
        self,
        *,
        model_request_parameters: ModelRequestParameters,
        open_stream: Callable[[], AbstractAsyncContextManager[StreamedResponse]],
        signal: SteerSignal,
        fallback_model_name: str,
    ) -> None:
        super().__init__(model_request_parameters=model_request_parameters)
        self._open_stream = open_stream
        self._signal = signal
        self._fallback_model_name = fallback_model_name
        self._inner: StreamedResponse | None = None
        self._aborted = False
        self._saw_tool_call = False
        self._started_at = datetime.now(UTC)

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        if self._signal.abort_stream.is_set():
            self._aborted = True
            return

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def pump() -> None:
            inner: StreamedResponse | None = None
            stream_token = _IN_STEERABLE_STREAM.set(True)
            try:
                async with self._open_stream() as opened:
                    inner = opened
                    self._inner = opened
                    # Share the inner parts manager so PartEndEvent and get() stay exact.
                    self.__dict__["_parts_manager"] = opened._parts_manager
                    if opened.metadata:
                        self.metadata = {**(self.metadata or {}), **opened.metadata}
                    async for event in opened:
                        if isinstance(event, PartStartEvent) and isinstance(
                            event.part, BaseToolCallPart
                        ):
                            self._saw_tool_call = True
                        if isinstance(event, PartEndEvent | FinalResultEvent):
                            continue
                        await queue.put(("event", event))
                    self._sync_from_inner(opened)
                await queue.put(("done", None))
            except asyncio.CancelledError:
                if inner is not None:
                    with contextlib.suppress(Exception):
                        await inner.cancel()
                    self._sync_from_inner(inner)
                raise
            except BaseException as exc:
                if inner is not None:
                    self._sync_from_inner(inner)
                await queue.put(("error", exc))
            finally:
                _IN_STEERABLE_STREAM.reset(stream_token)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                get_task = asyncio.create_task(queue.get())
                abort_task: asyncio.Task[bool] | None = (
                    None
                    if self._saw_tool_call
                    else asyncio.create_task(self._signal.abort_stream.wait())
                )
                waiters: set[asyncio.Task[Any]] = {get_task}
                if abort_task is not None:
                    waiters.add(abort_task)
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                if abort_task is not None and abort_task in done and not self._saw_tool_call:
                    self._aborted = True
                    pump_task.cancel()
                    await asyncio.gather(pump_task, return_exceptions=True)
                    return

                kind, payload = get_task.result()
                if kind == "done":
                    await pump_task
                    return
                if kind == "error":
                    await pump_task
                    raise payload
                yield payload
        finally:
            if not pump_task.done():
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)

    def _sync_from_inner(self, inner: StreamedResponse) -> None:
        self._usage = inner.usage
        self.finish_reason = inner.finish_reason
        self.provider_response_id = inner.provider_response_id
        self.provider_details = inner.provider_details
        self.canonical_system = inner.canonical_system
        if inner.metadata:
            self.metadata = {**(self.metadata or {}), **inner.metadata}

    def get(self) -> ModelResponse:
        if self._aborted:
            self.metadata = {**(self.metadata or {}), "bud_steered_abort": True}
            # Preserve partial text without marking interrupted so the run keeps going.
            self._finished = True
            self._cancelled = False
        return super().get()

    async def close_stream(self) -> None:
        if self._inner is not None:
            await self._inner.close_stream()

    @property
    def usage(self) -> RequestUsage:
        if self._inner is not None:
            return self._inner.usage
        return super().usage

    @property
    def model_name(self) -> str:
        return self._inner.model_name if self._inner is not None else self._fallback_model_name

    @property
    def provider_name(self) -> str | None:
        return self._inner.provider_name if self._inner is not None else None

    @property
    def provider_url(self) -> str | None:
        return self._inner.provider_url if self._inner is not None else None

    @property
    def timestamp(self) -> datetime:
        return self._inner.timestamp if self._inner is not None else self._started_at


def wrap_steerable_stream(
    open_stream: Callable[[], AbstractAsyncContextManager[StreamedResponse]],
    *,
    model_request_parameters: ModelRequestParameters,
    signal: SteerSignal,
    fallback_model_name: str,
) -> StreamedResponse:
    """Wrap a provider stream so mid-call steering aborts streaming cleanly."""
    return _AbortableStreamedResponse(
        model_request_parameters=model_request_parameters,
        open_stream=open_stream,
        signal=signal,
        fallback_model_name=fallback_model_name,
    )


def in_steerable_stream() -> bool:
    return _IN_STEERABLE_STREAM.get()


def _signal_for_context(
    get_signal: GetSignal,
    run_context: RunContext[Any] | None,
) -> SteerSignal | None:
    meta = run_context.metadata if run_context is not None else None
    session_id = (meta.get("bud_session_id") if isinstance(meta, dict) else None) or (
        request_session.get()
    )
    if not isinstance(session_id, str) or not session_id:
        return None
    attr = request_attribution.get()
    run_id = (run_context.run_id if run_context is not None else None) or (
        attr.run_id if attr else None
    )
    return get_signal(session_id, run_id)


@dataclass(init=False)
class SteerableModel(WrapperModel):
    """Model wrapper that aborts an in-flight text stream cleanly when steered."""

    def __init__(
        self,
        wrapped: Model,
        get_signal: GetSignal,
    ) -> None:
        super().__init__(wrapped)
        self._get_signal = get_signal

    @contextlib.asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        signal = _signal_for_context(self._get_signal, run_context)
        if signal is None or _IN_STEERABLE_STREAM.get():
            async with super().request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
            return

        def open_inner() -> AbstractAsyncContextManager[StreamedResponse]:
            return super(SteerableModel, self).request_stream(
                messages, model_settings, model_request_parameters, run_context
            )

        yield wrap_steerable_stream(
            open_inner,
            model_request_parameters=model_request_parameters,
            signal=signal,
            fallback_model_name=self.model_name,
        )


@dataclass(kw_only=True)
class SteeringCapability(AbstractCapability[None]):
    """Delivers mid-run owner steer messages into an active run between or during steps."""

    claim: ClaimSteer
    get_signal: GetSignal
    wake: Callable[[str], None] = lambda _session_id: None
    wrap_model: bool = True
    id: str | None = "bud_steering"

    def get_wrapper_model(self, model: Model) -> Model | None:
        if not self.wrap_model or isinstance(model, SteerableModel):
            return None
        # Inside DBOS durable mode, DurableModel owns the step boundary; TrackedModel handles
        # stream preemption inside the step instead of wrapping outside DurableModel.
        if hasattr(model, "_request_stream_segment"):
            return None
        return SteerableModel(model, self.get_signal)

    @durable_operation("claim_steer")
    async def _claim(
        self, ctx: RunContext[None], session_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        result = self.claim(session_id, run_id)
        if inspect.isawaitable(result):
            return await result
        return cast(list[dict[str, Any]], result)

    def _apply_claimed(self, ctx: RunContext[None], claimed: list[dict[str, Any]]) -> ModelRequest:
        steer_text = "\n\n".join(str(item["text"]) for item in claimed)
        if ctx.metadata is not None:
            ctx.metadata["bud_request"] = _merge_owner_request(
                ctx.metadata.get("bud_request"), steer_text
            )
        return _make_steer_request(claimed)

    async def before_model_request(
        self,
        ctx: RunContext[None],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        meta = ctx.metadata or {}
        session_id = meta.get("bud_session_id")
        run_id = ctx.run_id
        if not isinstance(session_id, str) or not session_id or not run_id:
            return request_context
        if meta.get("bud_revision_pending"):
            return request_context
        signal = self.get_signal(session_id, run_id)
        has_completed_model_step = (
            signal.model_steps > 0 if signal is not None else ctx.run_step > 0
        )
        if has_completed_model_step:
            claimed = await self._claim(ctx, session_id, run_id)
            if claimed:
                steer_msg = self._apply_claimed(ctx, claimed)
                ctx.messages.append(steer_msg)
                request_context.messages = [*request_context.messages, steer_msg]
                if signal is not None:
                    signal.abort_stream.clear()
                self.wake(session_id)
        if signal is not None:
            signal.in_model_call = True
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[None],
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        meta = ctx.metadata or {}
        session_id = meta.get("bud_session_id")
        run_id = ctx.run_id
        if not isinstance(session_id, str) or not session_id or not run_id:
            return response
        signal = self.get_signal(session_id, run_id)
        if signal is not None:
            signal.in_model_call = False
            signal.model_steps += 1
        if meta.get("bud_revision_pending"):
            return response
        aborted = bool((response.metadata or {}).get("bud_steered_abort"))
        # If the model called tools (and wasn't aborted), let CallToolsNode run first so
        # ask/review approvals supersede cleanly and allowed tools finish before step N+1.
        if response.tool_calls and not aborted:
            return response
        claimed = await self._claim(ctx, session_id, run_id)
        if claimed:
            steer_msg = self._apply_claimed(ctx, claimed)
            if signal is not None:
                signal.abort_stream.clear()
            ctx.enqueue(steer_msg, priority="asap")
            self.wake(session_id)
        return response
