import asyncio
import os
from dataclasses import dataclass
from time import perf_counter

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, RunTimedOut, UnexpectedModelBehavior, UserError
from pydantic_ai.usage import RunUsage

from bud.config import get_settings


@dataclass
class RunRecord:
    model: str
    output: str | None
    latency: float
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    requests: int
    tool_calls: int
    error: str | None


async def call_model(model: str, prompt: str) -> RunRecord:
    usage = RunUsage()
    started = perf_counter()

    try:
        agent = Agent(model=model)
        result = await agent.run(prompt, timeout=60, usage=usage)

    except (ModelAPIError, RunTimedOut, UnexpectedModelBehavior, UserError) as exc:
        return RunRecord(
            model=model,
            output=None,
            latency=perf_counter() - started,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            requests=usage.requests,
            tool_calls=usage.tool_calls,
            error=f"{type(exc).__name__}: {str(exc)}",
        )

    latency = perf_counter() - started
    usage = result.usage

    return RunRecord(
        model=model,
        output=result.output,
        latency=latency,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        requests=usage.requests,
        tool_calls=usage.tool_calls,
        error=None,
    )


async def main() -> None:
    settings = get_settings()
    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key.get_secret_value()
    else:
        os.environ.pop("OPENAI_API_KEY", None)
    if settings.deepseek_api_key:
        os.environ["DEEPSEEK_API_KEY"] = settings.deepseek_api_key.get_secret_value()
    else:
        os.environ.pop("DEEPSEEK_API_KEY", None)

    prompt = "Explain what recursive language models are in clear concise terms"

    models = ["deepseek:deepseek-flash", "openai:gpt-6-luna"]

    records: list[RunRecord] = []

    for model in models:
        print(f"Calling {model}...", flush=True)
        record = await call_model(model, prompt)
        records.append(record)

    print(
        f"\n{'Model':<30} {'Seconds':>9} {'Input':>7} "
        f"{'Output':>7} {'Cached':>7} {'Reqs':>5} {'Tools':>5}"
    )

    for record in records:
        print(
            f"{record.model:<30} {record.latency:>9.2f} "
            f"{str(record.input_tokens):>7} "
            f"{str(record.output_tokens):>7} "
            f"{str(record.cache_read_tokens):>7} "
            f"{record.requests:>5} {record.tool_calls:>5}"
        )

    for record in records:
        print(f"\n--- {record.model} ---")
        if record.error is not None:
            print(f"ERROR: {record.error}")
        else:
            print(record.output)


if __name__ == "__main__":
    asyncio.run(main())
