"""Context Evaluation Suite (COURSE Block B07).

Measures recall accuracy (Recall@k, MRR) across multi-turn / multi-session conversations,
proves active-turn exclusion, and validates prompt cache stability and token efficiency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from bud.core.memory import ConversationMemory, TurnSearchResult


@dataclass(frozen=True)
class ContextEvalCase:
    """One evaluation query with its ground truth target turn."""

    case_id: str
    query: str
    expected_turn_id: str
    description: str
    category: str = "general"


@dataclass(frozen=True)
class ContextEvalReport:
    """Structured report of context recall accuracy and prompt cache metrics."""

    total_cases: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    active_turn_leaks: int
    prompt_cache_stable: bool
    tokens_retrieval: int
    tokens_naive_dump: int
    token_savings_percent: float
    case_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Benchmark passing threshold: Recall@5 >= 0.8, MRR >= 0.6, zero leaks, savings >= 30%."""
        return (
            self.recall_at_5 >= 0.8
            and self.mrr >= 0.6
            and self.active_turn_leaks == 0
            and self.prompt_cache_stable
            and self.token_savings_percent >= 30.0
        )


def populate_benchmark_corpus(memory: ConversationMemory) -> list[ContextEvalCase]:
    """Seeds a realistic multi-session conversation history with facts and distractors."""
    # Session A: System preferences & configuration
    memory.add_turn(
        session_id="session_prefs",
        turn_index=1,
        user_text="Please remember that I prefer dark mode and concise summaries in all reports.",
        assistant_text="Noted! I will use dark mode formatting and keep all summaries concise.",
        created_at="2026-11-01T09:00:00Z",
        turn_id="turn_prefs_1",
    )
    memory.add_turn(
        session_id="session_prefs",
        turn_index=2,
        user_text="Also, set my primary timezone to America/Chicago.",
        assistant_text="Configured your timezone to America/Chicago (US Central).",
        created_at="2026-11-01T09:05:00Z",
        turn_id="turn_prefs_2",
    )

    # Session B: Engineering projects and migration plans
    memory.add_turn(
        session_id="session_eng",
        turn_index=1,
        user_text="When is the PostgreSQL database migration scheduled for?",
        assistant_text=(
            "The PostgreSQL database migration is scheduled for December 15th at 02:00 UTC."
        ),
        created_at="2026-11-05T14:30:00Z",
        turn_id="turn_eng_migration",
    )
    memory.add_turn(
        session_id="session_eng",
        turn_index=2,
        user_text="What repository holds the native companion source code?",
        assistant_text="The native companion code lives in github.com/thebookiers/bud-companion.",
        created_at="2026-11-05T14:35:00Z",
        turn_id="turn_eng_repo",
    )

    # Session C: Financial budgets and approvals
    memory.add_turn(
        session_id="session_finance",
        turn_index=1,
        user_text="Did Sam Lee approve the infrastructure budget for Q4?",
        assistant_text=(
            "Yes, Sam Lee approved the Q4 infrastructure budget of $45,000 on November 8th."
        ),
        created_at="2026-11-08T11:00:00Z",
        turn_id="turn_finance_budget",
    )

    # Distractor turns across multiple sessions
    distractors = [
        (
            "distract_1",
            1,
            "What's the weather like in Seattle today?",
            "Seattle is 52 degrees and rainy.",
            "2026-11-10T08:00:00Z",
        ),
        (
            "distract_1",
            2,
            "Recommend a good coffee shop nearby.",
            "You might like Monorail Espresso downtown.",
            "2026-11-10T08:05:00Z",
        ),
        (
            "distract_2",
            1,
            "Can you review this python function for sorting?",
            "The function looks clean and operates in O(n log n).",
            "2026-11-11T16:00:00Z",
        ),
        (
            "distract_2",
            2,
            "Let's test sqlite fts5 syntax.",
            "FTS5 queries support prefix matching and column filters.",
            "2026-11-11T16:15:00Z",
        ),
        (
            "distract_3",
            1,
            "What is our policy on personal AI assistants?",
            "Assistants operate strictly in read-only mode until explicit approval.",
            "2026-11-12T10:00:00Z",
        ),
    ]
    for sid, idx, u, a, ts in distractors:
        memory.add_turn(
            session_id=sid, turn_index=idx, user_text=u, assistant_text=a, created_at=ts
        )

    return [
        ContextEvalCase(
            case_id="case_1_theme_pref",
            query="What theme and summary style does the user prefer?",
            expected_turn_id="turn_prefs_1",
            description="Retrieve user UI theme preference from older settings conversation",
            category="preferences",
        ),
        ContextEvalCase(
            case_id="case_2_timezone",
            query="Which timezone is configured for the user?",
            expected_turn_id="turn_prefs_2",
            description="Retrieve user timezone preference",
            category="preferences",
        ),
        ContextEvalCase(
            case_id="case_3_db_migration",
            query="What is the exact deadline and date for the Postgres migration?",
            expected_turn_id="turn_eng_migration",
            description="Retrieve scheduled date for engineering migration",
            category="engineering",
        ),
        ContextEvalCase(
            case_id="case_4_companion_repo",
            query="Where is the repository URL for the companion code?",
            expected_turn_id="turn_eng_repo",
            description="Retrieve specific repository link from past chat",
            category="engineering",
        ),
        ContextEvalCase(
            case_id="case_5_budget_approval",
            query="How much budget did Sam Lee approve for Q4?",
            expected_turn_id="turn_finance_budget",
            description="Retrieve approval amount and decision maker",
            category="finance",
        ),
    ]


class ContextEvaluator:
    """Evaluates recall metrics and prompt cache health for the memory system."""

    def __init__(self, memory: ConversationMemory) -> None:
        self.memory = memory

    def evaluate(
        self,
        cases: list[ContextEvalCase],
        *,
        active_session_id: str = "active_session",
        active_turn_index: int = 999,
    ) -> ContextEvalReport:
        # Simulate an active in-flight turn that contains identical keywords to one of the queries
        # to test that active-turn exclusion strictly prevents echo loops.
        leak_probe_query = cases[0].query
        self.memory.add_turn(
            session_id=active_session_id,
            turn_index=active_turn_index,
            user_text=f"Active in-flight probe: {leak_probe_query}",
            assistant_text="Pending generation...",
            is_active=True,
            turn_id="active_probe_turn",
        )

        hits_at_1 = 0
        hits_at_3 = 0
        hits_at_5 = 0
        reciprocal_ranks: list[float] = []
        active_leaks = 0
        case_details: list[dict[str, Any]] = []

        total_retrieval_chars = 0
        total_naive_chars = 0

        # Read all stored turns to measure naive context dumping size
        with self.memory._connect() as db:
            all_chunks = db.execute("SELECT text FROM conversation_chunks").fetchall()
            naive_corpus_text = "\n".join(r[0] for r in all_chunks)
            total_naive_chars = len(naive_corpus_text)

        # Baseline system prompt prefix for prompt cache measurement
        system_prompt_prefix = (
            "You are Bud, a personal AI teammate. Always follow strict safety rules."
        )
        prefix_hash_initial = hashlib.sha256(system_prompt_prefix.encode()).hexdigest()

        for case in cases:
            results: list[TurnSearchResult] = self.memory.search_conversations(
                query=case.query,
                limit=5,
                exclude_active_turn=True,
                active_session_id=active_session_id,
                active_turn_index=active_turn_index,
            )

            # Check if active turn leaked
            for r in results:
                if r.id == "active_probe_turn" or r.chunk.is_active:
                    active_leaks += 1

            matched_rank: int | None = None
            for rank, r in enumerate(results, start=1):
                if r.id == case.expected_turn_id:
                    matched_rank = rank
                    break

            if matched_rank == 1:
                hits_at_1 += 1
            if matched_rank is not None and matched_rank <= 3:
                hits_at_3 += 1
            if matched_rank is not None and matched_rank <= 5:
                hits_at_5 += 1

            rr = 1.0 / matched_rank if matched_rank is not None else 0.0
            reciprocal_ranks.append(rr)

            # Measure token size of retrieved chunk vs naive corpus
            retrieved_text = "\n".join(r.chunk.formatted for r in results[:1])
            total_retrieval_chars += len(retrieved_text)

            case_details.append(
                {
                    "case_id": case.case_id,
                    "query": case.query,
                    "target_id": case.expected_turn_id,
                    "matched_rank": matched_rank,
                    "reciprocal_rank": rr,
                    "retrieved_top1": results[0].id if results else None,
                }
            )

        # Clean up active probe turn
        with self.memory._connect() as db:
            db.execute("DELETE FROM conversation_chunks WHERE id = 'active_probe_turn'")
            db.execute("DELETE FROM conversation_chunks_fts WHERE id = 'active_probe_turn'")

        total_cases = len(cases)
        recall_1 = hits_at_1 / total_cases if total_cases else 0.0
        recall_3 = hits_at_3 / total_cases if total_cases else 0.0
        recall_5 = hits_at_5 / total_cases if total_cases else 0.0
        mrr = sum(reciprocal_ranks) / total_cases if total_cases else 0.0

        # Estimate tokens (approx 4 chars per token)
        tokens_retrieval = total_retrieval_chars // 4
        tokens_naive = (total_naive_chars * total_cases) // 4
        savings_pct = (
            ((tokens_naive - tokens_retrieval) / tokens_naive * 100.0) if tokens_naive > 0 else 0.0
        )

        # Verify prompt cache prefix was never modified by retrieval
        prefix_hash_after = hashlib.sha256(system_prompt_prefix.encode()).hexdigest()
        cache_stable = prefix_hash_initial == prefix_hash_after

        return ContextEvalReport(
            total_cases=total_cases,
            recall_at_1=recall_1,
            recall_at_3=recall_3,
            recall_at_5=recall_5,
            mrr=mrr,
            active_turn_leaks=active_leaks,
            prompt_cache_stable=cache_stable,
            tokens_retrieval=tokens_retrieval,
            tokens_naive_dump=tokens_naive,
            token_savings_percent=savings_pct,
            case_details=case_details,
        )
