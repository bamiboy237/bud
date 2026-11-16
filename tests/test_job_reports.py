"""A job that ends gets a bud turn that tells the owner, without waiting for their next message."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from bud.core.projection import JOB_REPORT
from tests.runner_support import reply, running_bud

BRIEF = {
    "goal": "Find the renewal date of the lease.",
    "inputs": [],
    "expected_result": "The renewal date.",
}


def last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def bud_with_one_job(prompts: list[str]):
    """The owner asks, bud starts a job, the job finishes, and bud reports when told to."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(tool.name == "finish_job" for tool in info.output_tools):
            summary = {"summary": "The lease renews on 3 March."}
            return ModelResponse(parts=[ToolCallPart("finish_job", summary, tool_call_id="f1")])
        prompt = last_prompt(messages)
        prompts.append(prompt)
        if prompt.startswith(JOB_REPORT):
            return reply("Your lease job is done: it renews on 3 March.")
        if not prompt.startswith("When does my lease renew?"):
            return reply("Nothing else is pending.")
        if isinstance(messages[-1], ModelRequest) and any(
            part.part_kind == "tool-return" for part in messages[-1].parts
        ):
            return reply("I started a job to find it.")
        start = {"title": "Find the renewal date", "brief": BRIEF}
        return ModelResponse(parts=[ToolCallPart("start_task", start, tool_call_id="s1")])

    return model


async def test_a_finished_job_is_reported_without_a_new_message(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        events = bud.log.read("owner")

    queued = [e.data for e in events if e.type == "run.queued"]
    assert [run["trigger"] for run in queued] == ["message", "job"]
    report = queued[-1]
    assert report["request"] is None
    assert [e.type for e in events if e.type.startswith("task.")][-1] == "task.completed"
    # The report turn sees the job's result in its job updates.
    assert "The lease renews on 3 March." in prompts[-1]
    completed = [e for e in events if e.type == "run.completed"]
    assert completed[-1].data["run_id"] == report["run_id"]
    transcript = [turn.text for turn in bud.runner.snapshot("owner").transcript]
    assert "Your lease job is done: it renews on 3 March." in transcript
    # A job report is not the owner speaking.
    assert [e.data["text"] for e in events if e.type == "user.message"] == [
        "When does my lease renew?"
    ]


async def test_the_next_message_does_not_repeat_a_reported_job(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        bud.runner.start("owner", "Thanks. Anything else?", "message-2")
        await bud.settle()
        events = bud.log.read("owner")

    assert [e.data["trigger"] for e in events if e.type == "run.queued"] == [
        "message",
        "job",
        "message",
    ]
    assert prompts[-1].startswith("Thanks. Anything else?")
    assert "3 March" not in prompts[-1]
