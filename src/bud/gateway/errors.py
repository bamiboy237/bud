"""Provider error summaries that never copy response bodies or credential URLs."""

import re
from typing import Literal

import httpx
from pydantic_ai import exceptions as ai
from pydantic_ai.exceptions import ModelAPIError

from bud.composio.connection import ConnectionError as AccountConnectionError
from bud.gateway.chatgpt import ChatGPTError
from bud.skills.installer import SkillError


class NoModelAvailable(Exception):
    """A requested model has no configured key, provider, or tool support."""


ErrorKind = Literal[
    "auth",
    "permission",
    "rate_limit",
    "quota",
    "network",
    "timeout",
    "service",
    "context_full",
    "content_filter",
    "model_unavailable",
    "account",
    "skill",
    "recovery",
    "unknown",
]

# The recovery move for each kind. Every surface derives its sentence and its
# button from the kind, so the two can never disagree.
NEXT_STEP: dict[ErrorKind, str] = {
    "auth": "Sign in to the model provider again, then resend.",
    "permission": "Pick a model this key can use (/model), then resend.",
    "rate_limit": "The provider is rate-limiting; resend in a minute.",
    "quota": "The provider account is out of credit; top it up or pick another model.",
    "network": "Check the internet connection, then resend.",
    "timeout": "The provider took too long; resend, or pick a faster model.",
    "service": "The provider is having trouble; resend shortly or pick another model.",
    "context_full": "The conversation is too long for this model; start a new one or pick "
    "a model with a larger context.",
    "content_filter": "The provider refused this content; rephrase the request.",
    "model_unavailable": "Configure that model's key, or pick another model (/model).",
    "account": "Reconnect the account, then resend.",
    "skill": "Check the skill, then resend.",
    "recovery": "bud restarted with different code or settings; resend the request.",
    "unknown": "Resend; if it fails again, check `bud serve` output.",
}


def error_kind(exc: BaseException) -> ErrorKind:
    """Classify a run failure once, into a closed set of kinds."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return error_kind(exc.exceptions[0])
    checks: list[tuple[type[BaseException] | tuple[type[BaseException], ...], ErrorKind]] = [
        (ai.ModelQuotaExceededError, "quota"),
        (ai.ModelRateLimitError, "rate_limit"),
        (ai.ModelContextWindowExceededError, "context_full"),
        (ai.ContentFilterError, "content_filter"),
        (
            (ai.ModelTimeoutError, ai.ModelRequestTimeout, TimeoutError, httpx.TimeoutException),
            "timeout",
        ),
        ((ai.ModelConnectionError, httpx.NetworkError), "network"),
        ((ai.ModelServerError, ai.ModelOverloadedError), "service"),
        ((NoModelAvailable, ai.ModelUnavailableError), "model_unavailable"),
        (ChatGPTError, "auth"),
        (AccountConnectionError, "account"),
        (SkillError, "skill"),
    ]
    for types, kind in checks:
        if isinstance(exc, types):
            return kind
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    if status == 429:
        return "rate_limit"
    if status in (408, 504):
        return "timeout"
    if isinstance(status, int) and status >= 500:
        return "service"
    return "unknown"


def failure(exc: BaseException) -> dict[str, str]:
    """The fields a run.failed event carries: what happened, its kind, the next step."""
    kind = error_kind(exc)
    return {"reason": error_summary(exc), "kind": kind, "next": NEXT_STEP[kind]}


def error_summary(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(error_summary(error) for error in exc.exceptions)[:500]
    if isinstance(exc, (ChatGPTError, AccountConnectionError, SkillError, NoModelAvailable)):
        return str(exc)
    fields = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        fields.append(f"HTTP {status}")
    if isinstance(exc, ModelAPIError):
        if status == 401:
            fields.append("Check the model key or sign in to the model provider again")
        elif status == 403:
            fields.append("Check access to the selected model")
        code = exc.provider_error_code
        if code and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            fields.append(code)
    return ": ".join(fields)
