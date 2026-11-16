"""Tests for Context Evaluation Suite.

Evaluates recall accuracy, active-turn leak prevention, and prompt cache stability.
"""

from pathlib import Path

from bud.core.memory import ConversationMemory
from bud.core.memory_eval import (
    ContextEvaluator,
    populate_benchmark_corpus,
)


def test_context_evaluation_benchmark(tmp_path: Path):
    db_file = tmp_path / "memory_eval.db"
    memory = ConversationMemory(db_file)

    cases = populate_benchmark_corpus(memory)
    evaluator = ContextEvaluator(memory)

    report = evaluator.evaluate(
        cases=cases,
        active_session_id="active_user_session",
        active_turn_index=100,
    )

    # 1. Total evaluation cases
    assert report.total_cases == len(cases)
    assert report.total_cases >= 5

    # 2. Recall metrics
    # Recall@5 should capture at least 80% of targets across distractors
    assert report.recall_at_5 >= 0.8
    # MRR (Mean Reciprocal Rank) should be >= 0.6
    assert report.mrr >= 0.6
    assert report.recall_at_1 >= 0.6

    # 3. Active-turn exclusion leak check: zero leaks
    assert report.active_turn_leaks == 0

    # 4. Prompt cache preservation: system prompt prefix remains completely stable
    assert report.prompt_cache_stable is True

    # 5. Token savings: on-demand retrieval uses far fewer tokens than dumping full history
    assert report.token_savings_percent >= 30.0

    # 6. Overall benchmark passed
    assert report.passed is True
