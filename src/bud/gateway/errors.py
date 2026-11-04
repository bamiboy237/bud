"""Provider error summaries that never copy response bodies or credential URLs."""

import re

from pydantic_ai.exceptions import ModelAPIError

from bud.composio.connection import ConnectionError as AccountConnectionError
from bud.gateway.chatgpt import ChatGPTError


def error_summary(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(error_summary(error) for error in exc.exceptions)[:500]
    if isinstance(exc, (ChatGPTError, AccountConnectionError)):
        return str(exc)
    fields = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        fields.append(f"HTTP {status}")
    if isinstance(exc, ModelAPIError):
        code = exc.provider_error_code
        if code and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            fields.append(code)
    return ": ".join(fields)
