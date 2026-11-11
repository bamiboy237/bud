"""Per-call approval triage via OpenAI's Decisions API (POST /v1/decisions).

A predicate question returns a calibrated probability that an "ask" tool call is
safe to run without the owner. Anything unclear fails closed: a low score, a
refusal, or an endpoint error means the owner is asked, exactly as before.
Hard invariants never reach the model: sends and real-account writes stay "ask"
in code (PROTECTED), and only AUTO_CANDIDATES may be auto-allowed.
"""

import json
from typing import Any

import httpx

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISION_MODEL = "gpt-6-luna"

SAFE_PREDICATE = (
    "Does the tool call match exactly what the owner asked for, and is it safe to run "
    "without asking the owner? Answer true only for reversible self-hosted writes "
    "(simulated-world state, workspace files, memory, skill installs) with no "
    "real-account effect, no unknown recipients, and no content the owner never asked "
    "for; false for anything involving sending, real accounts, or unrequested changes."
)

# Sends and real-account writes always ask, in every mode: never model-decided.
PROTECTED = frozenset(
    {
        "send_email",
        "GMAIL_SEND_EMAIL",
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GOOGLECALENDAR_CREATE_EVENT",
        "GOOGLECALENDAR_PATCH_EVENT",
    }
)


def auto_candidates() -> frozenset[str]:
    """The "ask"-classed self-hosted writes the decision model may auto-allow.

    Imported lazily: bud.harness.memory imports bud.harness.actions, so a module-level
    import here would be circular.
    """
    from bud.harness.memory import MEMORY_RULES
    from bud.skills import RULES as SKILL_RULES
    from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
    from bud.world.tools import WRITE_TOOLS as WORLD_WRITE_TOOLS

    return frozenset(
        (WORLD_WRITE_TOOLS - {"send_email"})
        | WORKSPACE_WRITE_TOOLS
        | {name for name, action in MEMORY_RULES.items() if action == "ask"}
        | {name for name, action in SKILL_RULES.items() if action == "ask"}
    )


class DecisionClient:
    def __init__(self, api_key: str, *, threshold: float, timeout: float = 10.0) -> None:
        self.threshold = threshold
        self._client = httpx.AsyncClient(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def safe_probability(self, tool: str, args: dict[str, Any], context: str) -> float:
        """P(safe to auto-run). 0.0 on refusal or any error — fail closed."""
        payload = {
            "model": DECISION_MODEL,
            "input": (
                f"Owner and world context: {context}\n"
                f"Tool call: {tool} {json.dumps(args, default=str)[:4000]}"
            ),
            "questions": [{"type": "predicate", "name": "safe", "instructions": SAFE_PREDICATE}],
        }
        try:
            response = await self._client.post(DECISIONS_URL, headers=self._headers, json=payload)
            response.raise_for_status()
            answer = next(
                (a for a in response.json().get("answers", []) if a.get("name") == "safe"), None
            )
            if answer is None or "probability" not in answer:
                return 0.0
            return float(answer["probability"])
        except Exception:
            return 0.0

    async def close(self) -> None:
        await self._client.aclose()


def build_decision_client() -> DecisionClient | None:
    """None when disabled or unconfigured — the gateway then always asks."""
    from bud.config import get_settings

    settings = get_settings()
    if not settings.decision_auto_allow or settings.openai_api_key is None:
        return None
    return DecisionClient(
        settings.openai_api_key.get_secret_value(), threshold=settings.decision_threshold
    )
