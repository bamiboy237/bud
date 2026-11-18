"""Opt-in, content-free model tracing for the server process."""

from collections.abc import Generator
from contextlib import contextmanager

from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError

from bud.config import Settings


class TracingSetupError(UserError):
    """Tracing was requested without the optional SDK."""


@contextmanager
def tracing(settings: Settings) -> Generator[None]:
    token = settings.logfire_token
    if token is None or not token.get_secret_value().strip():
        yield
        return

    try:
        import logfire
    except ImportError as exc:
        raise TracingSetupError(
            "LOGFIRE_TOKEN is set. Install tracing with `uv sync --locked --extra tracing`."
        ) from exc

    logfire.configure(
        token=token.get_secret_value(),
        send_to_logfire=True,
        service_name="bud",
        console=False,
        inspect_arguments=False,
    )
    try:
        logfire.instrument_pydantic_ai(include_content=False, include_binary_content=False)
        yield
    finally:
        Agent.instrument_all(False)
        logfire.shutdown(timeout_millis=2000)
