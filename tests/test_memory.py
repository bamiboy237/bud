"""Tests for Conversation Memory: hybrid retrieval, full dated chunks, and active-turn exclusion."""

from pathlib import Path

import pytest

from bud.core.memory import (
    ConversationMemory,
    DeterministicVectorEmbedder,
    _cosine_similarity,
    build_conversation_memory_tools,
)


def test_deterministic_vector_embedder():
    embedder = DeterministicVectorEmbedder(dimensions=64)
    v1 = embedder.embed("PostgreSQL database migration scheduled")
    v2 = embedder.embed("PostgreSQL migration schedule date")
    v3 = embedder.embed("Banana strawberry fruit smoothie")

    sim_related = _cosine_similarity(v1, v2)
    sim_unrelated = _cosine_similarity(v1, v3)

    assert len(v1) == 64
    assert sim_related > sim_unrelated
    assert sim_related > 0.4
    assert sim_unrelated < 0.3


def test_full_dated_chunks_storage_and_read(tmp_path: Path):
    db_file = tmp_path / "memory.db"
    memory = ConversationMemory(db_file)

    created_iso = "2026-11-15T14:30:00Z"
    chunk = memory.add_turn(
        session_id="session_101",
        turn_index=1,
        user_text="What was the decision regarding API rate limits?",
        assistant_text=(
            "We capped anonymous requests at 60/min and authenticated requests at 600/min."
        ),
        created_at=created_iso,
        metadata={"priority": "high"},
    )

    assert chunk.id == "turn:session_101:1"
    assert chunk.session_id == "session_101"
    assert chunk.turn_index == 1
    assert chunk.created_at == created_iso
    assert "2026-11-15T14:30:00Z" in chunk.formatted
    assert "Session: session_101 Turn: 1" in chunk.formatted
    assert "User: What was the decision" in chunk.formatted
    assert "Assistant: We capped anonymous requests" in chunk.formatted

    # Read back by turn ID
    read_by_id = memory.read_turn("session_101", "turn:session_101:1")
    assert read_by_id is not None
    assert read_by_id.text == chunk.text
    assert read_by_id.metadata.get("priority") == "high"

    # Read back by turn index
    read_by_idx = memory.read_turn("session_101", 1)
    assert read_by_idx is not None
    assert read_by_idx.id == chunk.id

    # Non-existent turn returns None
    assert memory.read_turn("session_101", 999) is None
    assert memory.read_turn("non_existent_session", 1) is None


def test_active_turn_exclusion(tmp_path: Path):
    db_file = tmp_path / "memory.db"
    memory = ConversationMemory(db_file)

    # Completed turn 1 in active session
    memory.add_turn(
        session_id="current_active_session",
        turn_index=1,
        user_text="Deploying version 1.4 to production cluster",
        assistant_text="Deployment to production cluster completed successfully.",
        created_at="2026-11-15T10:00:00Z",
        is_active=False,
    )

    # In-progress active turn 2 in active session (user typed a message, agent is reasoning)
    memory.add_turn(
        session_id="current_active_session",
        turn_index=2,
        user_text="What cluster did we deploy version 1.4 to?",
        assistant_text="Checking previous cluster logs...",
        created_at="2026-11-15T10:05:00Z",
        is_active=True,
        turn_id="turn:current_active_session:2",
    )

    # Turn in another completed session
    memory.add_turn(
        session_id="other_session",
        turn_index=1,
        user_text="Staging cluster deployment finished",
        assistant_text="Staging is healthy.",
        created_at="2026-11-14T10:00:00Z",
        is_active=False,
    )

    # 1. Search with active-turn exclusion:
    # Turn 2 must NOT be returned even though it contains identical words
    results = memory.search_conversations(
        query="What cluster did we deploy version 1.4 to?",
        active_session_id="current_active_session",
        active_turn_index=2,
        exclude_active_turn=True,
    )

    result_ids = [r.id for r in results]
    assert "turn:current_active_session:2" not in result_ids
    assert "turn:current_active_session:1" in result_ids

    # 2. When active turn is completed and marked inactive, it becomes searchable
    memory.mark_active("current_active_session", 2, active=False)
    updated_results = memory.search_conversations(
        query="What cluster did we deploy version 1.4 to?",
        active_session_id="current_active_session",
        active_turn_index=None,
        exclude_active_turn=True,
    )
    updated_ids = [r.id for r in updated_results]
    assert "turn:current_active_session:2" in updated_ids


def test_hybrid_retrieval_fts5_and_vector(tmp_path: Path):
    db_file = tmp_path / "memory.db"
    memory = ConversationMemory(db_file)

    # Turn with exact distinctive keyword/name (best for FTS5)
    memory.add_turn(
        session_id="session_team",
        turn_index=1,
        user_text="Alice Vance joined as principal infra engineer.",
        assistant_text="Welcome Alice Vance to the team.",
        created_at="2026-11-01T12:00:00Z",
        turn_id="turn_alice",
    )

    # Turn with conceptual/semantic match (best for vector embeddings)
    memory.add_turn(
        session_id="session_team",
        turn_index=2,
        user_text="Our kubernetes cluster nodes require maintenance and kernel patching.",
        assistant_text="Scheduled server container upgrades for Friday night.",
        created_at="2026-11-02T12:00:00Z",
        turn_id="turn_k8s",
    )

    # Distractor turn
    memory.add_turn(
        session_id="session_team",
        turn_index=3,
        user_text="What snacks should we buy for the kitchen?",
        assistant_text="Apples, almonds, and sparkling water.",
        created_at="2026-11-03T12:00:00Z",
        turn_id="turn_snacks",
    )

    # Exact keyword search finds Alice Vance
    res_keyword = memory.search_conversations("Alice Vance")
    assert len(res_keyword) > 0
    assert res_keyword[0].id == "turn_alice"

    # Semantic/paraphrased query finds container infrastructure maintenance
    res_semantic = memory.search_conversations("server container patching upgrades")
    assert len(res_semantic) > 0
    assert res_semantic[0].id == "turn_k8s"


@pytest.mark.asyncio
async def test_conversation_memory_tools(tmp_path: Path):
    db_file = tmp_path / "memory.db"
    memory = ConversationMemory(db_file)

    memory.add_turn(
        session_id="s_alpha",
        turn_index=1,
        user_text="Remember: the production database port is 5432 and SSL is required.",
        assistant_text="Saved DB connection config: port 5432, sslmode=require.",
        created_at="2026-11-10T10:00:00Z",
    )

    tools = build_conversation_memory_tools(
        memory, active_session_id="s_active", active_turn_index=1
    )
    search_tool = tools["search_conversations"]
    read_tool = tools["read_turn"]

    search_output = await search_tool(query="production database port")
    assert "Result 1" in search_output
    assert "port 5432" in search_output
    assert "Session: s_alpha Turn: 1" in search_output

    read_output = await read_tool(session_id="s_alpha", turn_id="1")
    assert "Session: s_alpha Turn: 1" in read_output
    assert "User: Remember: the production database port" in read_output
