"""Runs the suite. Every attempt gets its own world and event log in a temporary folder, so
attempts can't affect each other, and a scripted user answers approvals.
Model calls use the real gateway, so `bud costs` includes eval spend.
"""

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from bud.config import get_settings
from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import snapshot, undecided_approvals
from bud.core.runner import Runner
from bud.evals.tasks import SUITE, Outcome, Task
from bud.gateway import Gateway, build_gateway
from bud.harness.actions import load_policy
from bud.harness.agent import Harness
from bud.harness.learning import Learner
from bud.harness.memory import build_memory
from bud.world.store import World
from bud.world.tools import READ_TOOLS, build_tools

ATTEMPT_TIMEOUT_SECONDS = 180
MAX_CONCURRENT_ATTEMPTS = 4


@dataclass(frozen=True)
class Attempt:
    task: str
    passed: bool
    problems: list[str]
    seconds: float
    cost_usd: float
    tokens: int
    session_id: str
    prompt_version: str
    models: list[dict[str, str]]
    requests: int = 0
    model_seconds: float = 0.0
    wasted: int = 0


def failure_reason(events: list[Event]) -> str | None:
    last = next((e for e in reversed(events) if e.type.startswith("run.")), None)
    return last.data["reason"] if last is not None and last.type == "run.failed" else None


async def converse(
    runner: Runner, log: EventLog, session_id: str, text: str, approve: bool
) -> list[dict]:
    """Play the user for one message: answer every approval with the scripted decision."""
    approvals: list[dict] = []
    runner.start(session_id, text, uuid4().hex)
    while True:
        while session_id in runner.tasks:
            await asyncio.shield(runner.tasks[session_id])
        pending = undecided_approvals(log.read(session_id))
        if not pending:
            await runner.settle_learning()
            return approvals
        for request in pending:
            approvals.append(request)
            runner.decide(session_id, request["approval_id"], approve)


async def drive(runner: Runner, log: EventLog, task: Task, session_id: str) -> list[dict]:
    return await converse(runner, log, session_id, task.instruction, task.approve)


async def teach(runner: Runner, log: EventLog, task: Task) -> None:
    """Earlier conversations: separate sessions, always approving, sharing world and memory."""
    for number, messages in enumerate(task.setup):
        session_id = f"setup-{task.id}-{number}-{uuid4().hex[:8]}"
        log.append(session_id, "session.created")
        for text in messages:
            await converse(runner, log, session_id, text, True)


async def run_attempt(task: Task, gateway: Gateway) -> Attempt:
    session_id = f"eval-{task.id}-{uuid4().hex[:8]}"
    started = perf_counter()
    with TemporaryDirectory() as folder:
        log = EventLog(Path(folder) / "events.db")
        world = World(Path(folder) / "world.db")
        harness = Harness(
            build_tools(world, include_ask_user=False),
            load_policy(),
            gateway.registry,
            world.profile(),
            memory=build_memory(Path(folder) / "memory.db", namespace="world"),
            spend_path=Path(folder) / "spend.db",
            result_path=log.path,
            read_tools=READ_TOOLS,
        )
        harness.clock = lambda: datetime.fromisoformat(world.now)
        assert harness.memory is not None
        learner = Learner(
            harness.memory,
            lambda session: gateway.model("fast", f"{session}:learn", require_tools=True),
        )
        runner = Runner(Hub(log), gateway, harness, learner=learner)
        before = world.snapshot()
        try:
            await asyncio.wait_for(teach(runner, log, task), ATTEMPT_TIMEOUT_SECONDS)
            log.append(session_id, "session.created")
            before = world.snapshot()
            approvals = await asyncio.wait_for(
                drive(runner, log, task, session_id), ATTEMPT_TIMEOUT_SECONDS
            )
            timed_out = False
        except TimeoutError:
            approvals, timed_out = [], True
        await runner.stop()
        events = log.read(session_id)
        # A call the model aimed at a tool it cannot see costs a whole model round trip.
        wasted = sum(
            1
            for e in events
            if e.type == "tool.result"
            and e.data.get("outcome") == "retried"
            and "Unknown tool name" in str(e.data.get("result", ""))
        )
        transcript = snapshot(session_id, events).transcript
        outcome = Outcome(
            before=before,
            after=world.snapshot(),
            answer=transcript[-1].text if transcript else "",
            approvals=approvals,
            failure=failure_reason(events),
            memory=await memory_text(learner),
        )
    problems = ["timed out"] if timed_out else task.check(outcome)
    if outcome.failure and not timed_out:
        problems.insert(0, f"run failed: {outcome.failure}")
    cost, tokens = gateway.ledger.session_totals(session_id)
    requests, model_seconds = gateway.ledger.session_profile(session_id)
    return Attempt(
        task=task.id,
        passed=not problems,
        problems=problems,
        seconds=perf_counter() - started,
        cost_usd=cost,
        tokens=tokens,
        session_id=session_id,
        prompt_version=harness.prompt_version,
        models=gateway.ledger.session_models(session_id),
        requests=requests,
        model_seconds=model_seconds,
        wasted=wasted,
    )


async def memory_text(learner: Learner) -> str:
    store = learner.memory.store
    assert not callable(store)
    saved = await store.read(learner.path, max_chars=20_000)
    return saved.content if saved else ""


async def run_suite(tasks: list[Task], trials: int) -> list[Attempt]:
    gateway = build_gateway()
    limit = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)

    async def one(task: Task) -> Attempt:
        async with limit:
            return await run_attempt(task, gateway)

    try:
        return list(await asyncio.gather(*(one(task) for task in tasks for _ in range(trials))))
    finally:
        gateway.close()


def report(attempts: list[Attempt], trials: int, prompt_version: str) -> Path:
    by_task: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        by_task.setdefault(attempt.task, []).append(attempt)

    print(
        f"{'Task':<20} {'Passed':>7} {'Seconds':>8} {'Model s':>8} {'Reqs':>5} "
        f"{'Wasted':>6} {'API USD':>9}"
    )
    for task, runs in by_task.items():
        n = len(runs)
        passed = sum(run.passed for run in runs)
        seconds = sum(run.seconds for run in runs) / n
        model = sum(run.model_seconds for run in runs) / n
        reqs = sum(run.requests for run in runs) / n
        wasted = sum(run.wasted for run in runs) / n
        cost = sum(run.cost_usd for run in runs) / n
        print(
            f"{task:<20} {f'{passed}/{n}':>7} {seconds:>8.1f} {model:>8.1f} {reqs:>5.1f} "
            f"{wasted:>6.1f} {cost:>9.5f}"
        )

    pass_rate = sum(a.passed for a in attempts) / len(attempts)
    pass_all = sum(all(r.passed for r in runs) for runs in by_task.values()) / len(by_task)
    total = sum(a.cost_usd for a in attempts)
    mean_reqs = sum(a.requests for a in attempts) / len(attempts)
    mean_wasted = sum(a.wasted for a in attempts) / len(attempts)
    print(
        f"\nPass rate: {pass_rate:.0%}   pass^{trials}: {pass_all:.0%}   "
        f"Metered API total: ${total:.4f}"
    )
    print(f"Model requests per task: {mean_reqs:.1f}   wasted calls per task: {mean_wasted:.2f}")
    if any(model["billing"] == "subscription" for a in attempts for model in a.models):
        print("ChatGPT plan usage is included in token counts, not API USD.")

    failures = [a for a in attempts if not a.passed]
    if failures:
        print("\nFailures:")
    for attempt in failures:
        print(f"  {attempt.task} ({attempt.session_id}): {'; '.join(attempt.problems)[:200]}")

    folder = get_settings().data_dir / "evals"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(
        json.dumps(
            {
                "prompt_version": prompt_version,
                "trials": trials,
                "pass_rate": pass_rate,
                "pass_all_trials": pass_all,
                "total_usd": total,
                "attempts": [asdict(attempt) for attempt in attempts],
            },
            indent=2,
        )
    )
    print(f"\nSaved {path}")
    return path


def evaluate(trials: int, task_id: str | None) -> None:
    tasks = [task for task in SUITE if task_id in (None, task.id)]
    if not tasks:
        raise SystemExit(f"Unknown task: {task_id}")
    attempts = asyncio.run(run_suite(tasks, trials))
    version = attempts[0].prompt_version
    report(attempts, trials, version)
