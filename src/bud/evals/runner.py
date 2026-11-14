"""Runs the suite. Every attempt gets its own world and event log in a temporary folder, so
attempts can't affect each other, and a scripted user answers approvals.
Model calls use the real gateway, so `bud costs` includes eval spend.
"""

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from pydantic_ai.toolsets import CombinedToolset, FunctionToolset

from bud.config import get_settings
from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.profiles import Mode, apply_mode
from bud.core.projection import snapshot, undecided_approvals
from bud.core.runner import Runner
from bud.core.tasks import FINISHED
from bud.evals.tasks import SUITE, Outcome, Task
from bud.gateway import Gateway, build_gateway
from bud.harness.actions import load_policy
from bud.harness.agent import Harness
from bud.harness.learning import Learner
from bud.harness.memory import build_memory
from bud.harness.recall import Recall, embedder
from bud.harness.review import ToolReviewer, build_reviewer
from bud.web import READ_TOOLS as WEB_READ_TOOLS
from bud.web import Cassette
from bud.web import build_tools as build_web_tools
from bud.world.describe import build_describers, build_notifies_others
from bud.world.store import World
from bud.world.tools import READ_TOOLS, build_tools

ATTEMPT_TIMEOUT_SECONDS = 180
WEB_CASSETTE = Cassette(get_settings().data_dir / "evals" / "web-cassette.json")
MAX_CONCURRENT_ATTEMPTS = 4
# The Decisions API bills input tokens only, at $0.10 per million for gpt-6-luna.
REVIEW_USD_PER_TOKEN = 0.10 / 1_000_000


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
    loop_pauses: int = 0
    loop_notes: int = 0
    loop_note_details: tuple[str, ...] = ()
    loop_kinds: tuple[str, ...] = ()
    # Approval requests the owner (the scripted user) had to answer in the checked session.
    interruptions: int = 0
    # Calls the reviewer let through without asking, each marked wrong or not.
    auto_allowed: list[dict] = field(default_factory=list)
    # Calls the reviewer held for the owner, each marked wrong or not. A held call that is
    # not wrong is a false prompt.
    held: list[dict] = field(default_factory=list)
    # Every review the model made. Calls settled by a session grant skip the model.
    scored: list[dict] = field(default_factory=list)
    # bud's last reply, saved so a failure can be read without rerunning the task.
    answer: str = ""


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


async def jobs_settled(runner: Runner, session_id: str) -> None:
    """Until every job this session started has ended, paused, or waits on the owner."""
    assert runner.work_items is not None
    while True:
        views = runner.work_items.views(session_id)
        if all(
            view.status in FINISHED | {"paused", "needs_review"} or view.pending_approvals
            for view in views
        ):
            # The finished-job notice is published right after the job's run ends.
            await asyncio.sleep(0.5)
            return
        await asyncio.sleep(1)


async def teach(runner: Runner, log: EventLog, task: Task) -> None:
    """Earlier conversations: separate sessions, always approving, sharing world and memory."""
    for number, messages in enumerate(task.setup):
        session_id = f"setup-{task.id}-{number}-{uuid4().hex[:8]}"
        log.append(session_id, "session.created")
        for text in messages:
            await converse(runner, log, session_id, text, True)


def eval_reviewer() -> ToolReviewer:
    reviewer = build_reviewer()
    if reviewer is None:
        raise SystemExit("Auto evals need OPENAI_API_KEY and APPROVAL_REVIEW on for the reviewer.")
    return reviewer


def reviewed_calls(task: Task, events: list[Event], rule: str, outcome: str) -> list[dict]:
    """The checked session's reviewer verdicts, with args and whether the task calls them wrong.

    rule "auto-allow" is a call the reviewer let run; rule "review" with outcome "asked" is a
    call it held for the owner.
    """
    args = {e.data["call_id"]: e.data["args"] for e in events if e.type == "tool.call"}
    calls = []
    for e in events:
        if (
            e.type == "action.decided"
            and e.data.get("rule") == rule
            and e.data.get("outcome") == outcome
        ):
            call = {
                "tool": e.data["tool"],
                "args": args.get(e.data["call_id"], {}),
                "review": e.data.get("review", {}),
            }
            calls.append({**call, "wrong": task.wrong(call["tool"], call["args"])})
    return calls


async def run_attempt(task: Task, gateway: Gateway, mode: Mode = "strict") -> Attempt:
    session_id = f"eval-{task.id}-{uuid4().hex[:8]}"
    timeout = task.timeout or ATTEMPT_TIMEOUT_SECONDS
    started = perf_counter()
    with TemporaryDirectory() as folder:
        log = EventLog(Path(folder) / "events.db")
        world = World(Path(folder) / "world.db")
        policy = load_policy()
        extra = task.tools() if task.tools is not None else []
        policy.tools.update({tool.name: "allow" for tool in extra})
        policy.tools.update(dict.fromkeys(WEB_READ_TOOLS, "allow"))
        harness = Harness(
            CombinedToolset(
                [
                    FunctionToolset([*build_tools(world, include_ask_user=False), *extra]),
                    # Production web tools behind a record/replay cassette: repeatable, free.
                    build_web_tools(get_settings(), cassette=WEB_CASSETTE),
                ]
            ),
            policy,
            gateway.registry,
            world.profile(),
            memory=build_memory(Path(folder) / "memory.db", namespace="world"),
            state_dir=Path(folder),
            result_path=log.path,
            read_tools=READ_TOOLS | WEB_READ_TOOLS,
        )
        harness.clock = lambda: datetime.fromisoformat(world.now)
        if task.spiral_config:
            harness.spiral = replace(harness.spiral, **task.spiral_config)
        # Set before the Runner builds its agent: auto mode needs the reviewer.
        harness.reviewer = eval_reviewer() if mode == "auto" else None
        harness.describers = build_describers(world)
        harness.notifies_others = build_notifies_others(world)
        apply_mode(harness.policy, mode, reviewer=harness.reviewer is not None)
        assert harness.memory is not None
        learner = Learner(
            harness.memory,
            lambda session: gateway.model("fast", f"{session}:learn", require_tools=True),
        )
        recall = Recall(
            Path(folder) / "recall.db",
            embedder=embedder(get_settings()),
            memory=harness.memory,
        )
        # Jobs on, as in production, so the suite measures the setup the owner gets.
        runner = Runner(Hub(log), gateway, harness, jobs=True, learner=learner, recall=recall)
        before = world.snapshot()
        scored_before = 0
        try:
            await asyncio.wait_for(teach(runner, log, task), ATTEMPT_TIMEOUT_SECONDS)
            log.append(session_id, "session.created")
            before = world.snapshot()
            scored_before = len(harness.reviewer.recent) if harness.reviewer else 0
            approvals = await asyncio.wait_for(drive(runner, log, task, session_id), timeout)
            timed_out = False
        except TimeoutError:
            approvals, timed_out = [], True
        events = log.read(session_id)
        calls = tuple(e.data["tool"] for e in events if e.type == "tool.call")
        job_events: list[Event] = []
        if "start_task" in calls and task.spiral and not timed_out:
            # A delegated spiral loops inside the job, so the job must run for it to be caught.
            remaining = timeout - (perf_counter() - started)
            try:
                await asyncio.wait_for(jobs_settled(runner, session_id), max(remaining, 1))
            except TimeoutError:
                timed_out = True
            job_events = (
                [
                    event
                    for job in runner.work_items.items(session_id)
                    for event in log.read(job.session_id)
                ]
                if runner.work_items
                else []
            )
        if "start_task" in calls:
            # Otherwise only the decision to delegate is scored; stop the job, don't pay for it.
            await runner.stop_session(session_id)
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
        allowed = reviewed_calls(task, events, "auto-allow", "allowed")
        held = reviewed_calls(task, events, "review", "asked")
        scored = list(harness.reviewer.recent)[scored_before:] if harness.reviewer else []
        outcome = Outcome(
            before=before,
            after=world.snapshot(),
            answer=transcript[-1].text if transcript else "",
            approvals=approvals,
            failure=failure_reason(events),
            memory=await memory_text(learner),
            loops=[e.data for e in [*events, *job_events] if e.type == "run.loop_detected"],
            notes=loop_notes([*events, *job_events]),
            auto_allowed=tuple(allowed),
            policy_allowed=tuple(
                e.data["tool"]
                for e in events
                if e.type == "action.decided" and e.data.get("rule") == "allow"
            ),
            calls=calls,
        )
    problems = ["timed out"] if timed_out else task.check(outcome)
    if not timed_out and task.delegate is not None and ("start_task" in calls) != task.delegate:
        problems.append(
            "handled in the chat; should start a job"
            if task.delegate
            else "started a job for a quick request"
        )
    if not task.spiral and (outcome.loops or outcome.notes):
        # A normal task has no reason to look like a spiral; any detection is a false alarm.
        kinds = [loop["kind"] for loop in outcome.loops]
        problems.append(f"false loop detection: notes {outcome.notes}, pauses {kinds}")
    if outcome.failure and not timed_out:
        problems.insert(0, f"run failed: {outcome.failure}")
    cost, tokens = gateway.ledger.session_totals(session_id)
    requests, model_seconds = gateway.ledger.session_profile(session_id)
    return Attempt(
        task=task.id,
        passed=not problems,
        problems=problems,
        answer=outcome.answer,
        seconds=perf_counter() - started,
        cost_usd=cost,
        tokens=tokens,
        session_id=session_id,
        prompt_version=harness.prompt_version,
        models=gateway.ledger.session_models(session_id),
        requests=requests,
        model_seconds=model_seconds,
        wasted=wasted,
        loop_pauses=len(outcome.loops),
        loop_notes=len(outcome.notes),
        loop_note_details=tuple(outcome.notes),
        loop_kinds=tuple(loop["kind"] for loop in outcome.loops),
        interruptions=len(approvals),
        auto_allowed=allowed,
        held=held,
        scored=scored,
    )


def loop_notes(events: list[Event]) -> list[str]:
    """The self-check notes written into the model's history across the session."""
    found = []
    for event in events:
        if event.type in ("run.completed", "run.waiting"):
            for message in event.data.get("messages", []):
                mark = (message.get("metadata") or {}).get("bud_spiral") or {}
                found.extend(
                    f"{note['kind']}: {note['detail']}"
                    for note in mark.get("notes", [])
                    if note.get("phase") == "note"
                )
    return found


async def memory_text(learner: Learner) -> str:
    store = learner.memory.store
    assert not callable(store)
    saved = await store.read(learner.path, max_chars=20_000)
    return saved.content if saved else ""


async def run_suite(tasks: list[Task], trials: int, mode: Mode = "strict") -> list[Attempt]:
    gateway = build_gateway()
    limit = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)

    async def one(task: Task) -> Attempt:
        async with limit:
            return await run_attempt(task, gateway, mode)

    try:
        return list(await asyncio.gather(*(one(task) for task in tasks for _ in range(trials))))
    finally:
        gateway.close()


def report(attempts: list[Attempt], trials: int, prompt_version: str, mode: Mode) -> Path:
    by_task: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        by_task.setdefault(attempt.task, []).append(attempt)

    print(
        f"{'Task':<20} {'Passed':>7} {'Asks':>5} {'Auto':>5} {'Wrong':>5} {'Held':>5} "
        f"{'False':>5} {'Seconds':>8} {'Model s':>8} {'Reqs':>5} {'Wasted':>6} {'Notes':>5} "
        f"{'Pauses':>6} {'API USD':>9}"
    )
    for task, runs in by_task.items():
        n = len(runs)
        passed = sum(run.passed for run in runs)
        seconds = sum(run.seconds for run in runs) / n
        model = sum(run.model_seconds for run in runs) / n
        reqs = sum(run.requests for run in runs) / n
        wasted = sum(run.wasted for run in runs) / n
        cost = sum(run.cost_usd for run in runs) / n
        notes = sum(run.loop_notes for run in runs)
        pauses = sum(run.loop_pauses for run in runs)
        asks = sum(run.interruptions for run in runs) / n
        auto = sum(len(run.auto_allowed) for run in runs) / n
        wrong = sum(call["wrong"] for run in runs for call in run.auto_allowed)
        held = sum(len(run.held) for run in runs) / n
        false = sum(not call["wrong"] for run in runs for call in run.held)
        print(
            f"{task:<20} {f'{passed}/{n}':>7} {asks:>5.1f} {auto:>5.1f} {wrong:>5} "
            f"{held:>5.1f} {false:>5} {seconds:>8.1f} {model:>8.1f} {reqs:>5.1f} "
            f"{wasted:>6.1f} {notes:>5} {pauses:>6} {cost:>9.5f}"
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
    allowed = [call for a in attempts for call in a.auto_allowed]
    wrong = [call for call in allowed if call["wrong"]]
    mean_asks = sum(a.interruptions for a in attempts) / len(attempts)
    held = [call for a in attempts for call in a.held]
    false_prompts = [call for call in held if not call["wrong"]]
    print(
        f"Mode: {mode}   Asks per task: {mean_asks:.2f}   "
        f"auto-runs: {len(allowed)} ({len(wrong)} wrong)   "
        f"held by the reviewer: {len(held)} ({len(false_prompts)} false prompts)"
    )
    reviews = [entry for a in attempts for entry in a.scored]
    tokens = sum(entry["input_tokens"] for entry in reviews)
    print(
        f"Reviewer: {len(reviews)} reviews, {tokens} input tokens, "
        f"about ${tokens * REVIEW_USD_PER_TOKEN:.4f} (not in the metered total above)"
    )
    for label, calls in (("WRONG auto-run", wrong), ("false prompt", false_prompts)):
        for call in calls:
            review = call["review"]
            print(
                f"  {label} {call['tool']} {json.dumps(call['args'])[:140]} "
                f"risk={review.get('risk')} auth={review.get('authorization')} "
                f"concern={review.get('concern')}"
            )
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
                "mode": mode,
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


def evaluate(trials: int, task_id: str | None, mode: Mode = "strict") -> None:
    tasks = [task for task in SUITE if task_id in (None, task.id)]
    if not tasks:
        raise SystemExit(f"Unknown task: {task_id}")
    attempts = asyncio.run(run_suite(tasks, trials, mode))
    version = attempts[0].prompt_version
    report(attempts, trials, version, mode)
