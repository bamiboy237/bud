"""Keep the owner's data out of log output.

bud's own log calls name error types and ids, never message text. Libraries are less
careful: DBOS logs a cancelled step with the repr of its arguments, which is the whole
model request. Every handler that writes output therefore gets one filter that:

- rewrites known noisy records (an expected cancellation) to a fixed line,
- replaces secret values from the environment with [REDACTED] (as Mistral Vibe's
  formatter does),
- cuts each record to a bounded length, so no library can dump a prompt (t3code logs
  bounded diagnostics instead of raw error objects for the same reason).
"""

import logging
import os
import re
from functools import cache

from pydantic import SecretStr

REDACTED = "[REDACTED]"
MAX_MESSAGE = 500
MAX_TRACEBACK = 4000
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
CANCELLATIONS = {"DBOSWorkflowCancelledError", "DBOSAwaitedWorkflowCancelledError"}
GUARDED_LOGGERS = ("", "dbos", "uvicorn", "uvicorn.error", "uvicorn.access")


def _secrets() -> list[str]:
    values = {
        value for name, value in os.environ.items() if SECRET_NAME.search(name) and len(value) >= 8
    }
    # bud reads its keys from .env into settings, not into the environment.
    values |= _settings_secrets()
    # Longest first, so a secret that contains another is hidden whole.
    return sorted(values, key=len, reverse=True)


@cache
def _settings_secrets() -> frozenset[str]:
    from bud.config import get_settings

    found: set[str] = set()
    for value in get_settings().model_dump().values():
        candidates = value.values() if isinstance(value, dict) else [value]
        found |= {
            item.get_secret_value()
            for item in candidates
            if isinstance(item, SecretStr) and len(item.get_secret_value()) >= 8
        }
    return frozenset(found)


def _clean(text: str, limit: int, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    if len(text) > limit:
        text = f"{text[:limit]}... [{len(text) - limit} more chars dropped]"
    return text


class PrivateLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        error = record.exc_info[1] if record.exc_info else None
        if record.name == "dbos" and (
            message.startswith("Asyncio task cancelled") or type(error).__name__ in CANCELLATIONS
        ):
            # Stopping a job cancels its workflow. DBOS reports that as a warning that
            # prints the step's arguments and as an error with a traceback. Both are
            # expected, so say so in one line.
            record.levelno, record.levelname = logging.INFO, "INFO"
            message = "Job step cancelled (expected when a job is stopped)."
            record.exc_info = None
        secrets = _secrets()
        record.msg, record.args = _clean(message, MAX_MESSAGE, secrets), None
        if record.exc_info:
            record.exc_text = _clean(
                logging.Formatter().formatException(record.exc_info), MAX_TRACEBACK, secrets
            )
            record.exc_info = None
        return True


def guard_logs() -> None:
    """Attach the filter to every handler bud and its libraries write through.

    Call after the libraries have added their handlers (DBOS adds one at construction).
    Safe to call more than once.
    """
    for name in GUARDED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, PrivateLogFilter) for f in handler.filters):
                handler.addFilter(PrivateLogFilter())
