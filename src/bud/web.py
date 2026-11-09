"""Bounded public research through Exa's hosted MCP service."""

from ipaddress import ip_address
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import FunctionToolset

from bud.config import Settings

READ_TOOLS = {"search_web", "read_webpage"}
GUIDANCE = """
Use `search_web` to find public sources and `read_webpage` to inspect full page contents.
Include source URLs alongside factual claims derived from web results.
"""


def _public_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        len(url) > 4096
        or parsed.scheme not in ("https", "http")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 80, 443)
        or "." not in host
        or host.endswith((".local", ".localhost", ".internal", ".home.arpa"))
    ):
        raise ValueError("Read only public HTTP or HTTPS URLs without credentials")
    try:
        address = ip_address(host)
    except ValueError:
        return
    if not address.is_global or address.is_multicast:
        raise ValueError("Private and special IP addresses are not public page URLs")


def build_tools(settings: Settings) -> FunctionToolset[None]:
    async def call(name: str, arguments: dict[str, Any]) -> Any:
        key = settings.exa_api_key
        remote = MCPToolset(
            "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa",
            headers={"x-api-key": key.get_secret_value()} if key else None,
            id="exa",
            init_timeout=15,
            read_timeout=30,
            max_retries=0,
            tool_error_behavior="error",
            include_instructions=False,
            prefer_tasks=False,
        )
        try:
            async with remote:
                return await remote.direct_call_tool(name, arguments)
        except Exception:
            # Transport errors can include headers. Do not expose their text to the model or log.
            return {
                "available": False,
                "error": "Exa could not return this result. The page may be unavailable, or the "
                "service may be rate-limited or unreachable.",
            }

    async def search_web(
        query: Annotated[str, Field(min_length=1, max_length=2000)],
        objective: Annotated[str, Field(min_length=1, max_length=4096)],
        num_results: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> Any:
        """Find public web sources. State what evidence this search should return."""
        if not query.strip() or not objective.strip():
            return {"available": False, "error": "Supply a nonempty query and objective."}
        return await call(
            "web_search_exa", {"query": query, "objective": objective, "numResults": num_results}
        )

    async def read_webpage(
        urls: Annotated[list[str], Field(min_length=1, max_length=3)],
        max_characters: Annotated[int, Field(ge=1, le=50000)] = 20000,
    ) -> Any:
        """Retrieve bounded text from 1 to 3 public URLs. Content may be cached or incomplete."""
        try:
            for url in urls:
                _public_url(url)
        except ValueError as exc:
            return {"available": False, "error": str(exc), "executed": False}
        return await call("web_fetch_exa", {"urls": urls, "maxCharacters": max_characters})

    return FunctionToolset([search_web, read_webpage])
