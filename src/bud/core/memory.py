"""Context & Long-Term Memory (COURSE Block B07).

Provides conversation indexing, hybrid retrieval (FTS5 + vector), active-turn exclusion,
and full dated chunk retrieval for long-term memory across sessions.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_DIMENSIONS = 128
RRF_K = 60
WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'@.-]{1,}")
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "our",
        "their",
    ]
)

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_chunks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    user_text TEXT,
    assistant_text TEXT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,
    metadata TEXT,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON conversation_chunks(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_chunks_created ON conversation_chunks(created_at);
CREATE INDEX IF NOT EXISTS idx_chunks_active ON conversation_chunks(session_id, is_active);

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_chunks_fts USING fts5(
    text,
    id UNINDEXED,
    session_id UNINDEXED,
    created_at UNINDEXED
);
"""


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.sumprod(a, b)
    norm_a = math.sqrt(math.sumprod(a, a))
    norm_b = math.sqrt(math.sumprod(b, b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


class DeterministicVectorEmbedder:
    """Deterministic, zero-dependency feature-hashed vector embedder.

    Produces normalized dense embeddings from words and character n-grams.
    Safe for fully offline test and production environments.
    """

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        text_clean = text.lower().strip()
        if not text_clean:
            return vector

        tokens: list[str] = []
        for word in WORD_PATTERN.findall(text_clean):
            tokens.append(word)
            # Add character 3-grams for subword matching
            if len(word) >= 3:
                for i in range(len(word) - 2):
                    tokens.append(word[i : i + 3])

        if not tokens:
            return vector

        for token in tokens:
            digest = md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[idx] += sign

        norm = math.sqrt(math.sumprod(vector, vector))
        if norm > 0.0:
            vector = [val / norm for val in vector]
        return vector

    def __call__(self, texts: list[str] | str) -> Any:
        if isinstance(texts, str):
            return self.embed(texts)
        return [self.embed(t) for t in texts]


@dataclass(frozen=True)
class TurnChunk:
    """Full dated chunk representing a conversation turn."""

    id: str
    session_id: str
    turn_index: int
    role: str
    user_text: str | None
    assistant_text: str | None
    created_at: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    is_active: bool = False

    @property
    def formatted(self) -> str:
        """Full dated chunk representation suitable for context citations."""
        header = f"[{self.created_at}] Session: {self.session_id} Turn: {self.turn_index}"
        lines = [header]
        if self.user_text:
            lines.append(f"User: {self.user_text.strip()}")
        if self.assistant_text:
            lines.append(f"Assistant: {self.assistant_text.strip()}")
        if not self.user_text and not self.assistant_text:
            lines.append(f"{self.role.capitalize()}: {self.text.strip()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class TurnSearchResult:
    """A scored search result with the full dated chunk and match details."""

    id: str
    session_id: str
    turn_index: int
    created_at: str
    role: str
    score: float
    chunk: TurnChunk
    lexical_rank: int | None = None
    vector_score: float | None = None

    @property
    def snippet(self) -> str:
        return self.chunk.formatted


def _format_dated_chunk(
    session_id: str,
    turn_index: int,
    created_at: str,
    role: str,
    user_text: str | None,
    assistant_text: str | None,
    raw_text: str | None = None,
) -> str:
    timestamp = created_at
    parts = [f"[{timestamp}] Session: {session_id} Turn: {turn_index}"]
    if user_text:
        parts.append(f"User: {user_text.strip()}")
    if assistant_text:
        parts.append(f"Assistant: {assistant_text.strip()}")
    if not user_text and not assistant_text and raw_text:
        parts.append(f"{role.capitalize()}: {raw_text.strip()}")
    return "\n".join(parts)


def _sanitize_fts_query(text: str) -> str | None:
    words = [re.split(r"['’]", w.lower())[0].strip(".-") for w in WORD_PATTERN.findall(text)]
    meaningful = [w for w in words if len(w) > 1 and w not in STOP_WORDS]
    if not meaningful:
        return None
    # Support prefix matching and OR combination for high recall
    terms = [f'"{w}"*' for w in sorted(set(meaningful))]
    return " OR ".join(terms)


class ConversationMemory:
    """Persistent hybrid memory store for conversation history.

    Features:
    - Active-turn exclusion: In-progress turns are excluded from search results.
    - Full dated chunks: Every turn retains its UTC timestamp, session_id, and turn_index.
    - Hybrid retrieval: SQLite FTS5 (BM25) + dense vector similarity with Reciprocal Rank Fusion.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        embedder: Any = None,
    ) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if embedder is None:
            self.embedder = DeterministicVectorEmbedder()
        else:
            self.embedder = embedder
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(MEMORY_SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10.0)) as db, db:
            yield db

    def _get_embedding(self, text: str) -> list[float]:
        try:
            if callable(self.embedder):
                res = self.embedder([text])
                if isinstance(res, list) and res:
                    if isinstance(res[0], list):
                        return res[0]
                    if isinstance(res[0], (int, float)):
                        return res
            elif hasattr(self.embedder, "embed"):
                return self.embedder.embed(text)
        except Exception:
            pass
        return DeterministicVectorEmbedder().embed(text)

    def add_turn(
        self,
        session_id: str,
        turn_index: int,
        *,
        user_text: str | None = None,
        assistant_text: str | None = None,
        role: str = "exchange",
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        is_active: bool = False,
        turn_id: str | None = None,
    ) -> TurnChunk:
        """Index a new conversation turn into both FTS5 and vector indices."""
        cid = turn_id or f"turn:{session_id}:{turn_index}"
        ts = created_at or datetime.now(UTC).isoformat()
        full_text = _format_dated_chunk(
            session_id=session_id,
            turn_index=turn_index,
            created_at=ts,
            role=role,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        meta_json = json.dumps(metadata or {})
        vector = self._get_embedding(full_text)
        blob = _pack_vector(vector) if vector else None

        with self._connect() as db:
            db.execute("DELETE FROM conversation_chunks_fts WHERE id = ?", (cid,))
            db.execute(
                """
                INSERT OR REPLACE INTO conversation_chunks
                (id, session_id, turn_index, role, user_text, assistant_text,
                 created_at, text, embedding, metadata, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    session_id,
                    turn_index,
                    role,
                    user_text,
                    assistant_text,
                    ts,
                    full_text,
                    blob,
                    meta_json,
                    1 if is_active else 0,
                ),
            )
            db.execute(
                """
                INSERT INTO conversation_chunks_fts (text, id, session_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (full_text, cid, session_id, ts),
            )

        return TurnChunk(
            id=cid,
            session_id=session_id,
            turn_index=turn_index,
            role=role,
            user_text=user_text,
            assistant_text=assistant_text,
            created_at=ts,
            text=full_text,
            metadata=metadata or {},
            is_active=is_active,
        )

    def mark_active(self, session_id: str, turn_index: int, active: bool = True) -> None:
        """Mark or unmark a turn as active."""
        with self._connect() as db:
            db.execute(
                """
                UPDATE conversation_chunks SET is_active = ?
                WHERE session_id = ? AND turn_index = ?
                """,
                (1 if active else 0, session_id, turn_index),
            )

    def read_turn(self, session_id: str, turn_id: str | int) -> TurnChunk | None:
        """Retrieve the full dated chunk for an exact turn by id or index."""
        with self._connect() as db:
            if isinstance(turn_id, int) or (isinstance(turn_id, str) and turn_id.isdigit()):
                row = db.execute(
                    """
                    SELECT id, session_id, turn_index, role, user_text, assistant_text,
                           created_at, text, metadata, is_active
                    FROM conversation_chunks
                    WHERE session_id = ? AND turn_index = ?
                    LIMIT 1
                    """,
                    (session_id, int(turn_id)),
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT id, session_id, turn_index, role, user_text, assistant_text,
                           created_at, text, metadata, is_active
                    FROM conversation_chunks
                    WHERE session_id = ? AND id = ?
                    LIMIT 1
                    """,
                    (session_id, str(turn_id)),
                ).fetchone()

        if not row:
            return None

        return TurnChunk(
            id=row[0],
            session_id=row[1],
            turn_index=row[2],
            role=row[3],
            user_text=row[4],
            assistant_text=row[5],
            created_at=row[6],
            text=row[7],
            metadata=json.loads(row[8]) if row[8] else {},
            is_active=bool(row[9]),
        )

    def search_conversations(
        self,
        query: str,
        session_id: str | None = None,
        *,
        limit: int = 5,
        exclude_active_turn: bool = True,
        active_session_id: str | None = None,
        active_turn_id: str | None = None,
        active_turn_index: int | None = None,
        min_vector_score: float = 0.1,
    ) -> list[TurnSearchResult]:
        """Hybrid retrieval across conversation turns.

        Args:
            query: The search keywords or natural language query.
            session_id: Optional filter to search within a specific session.
            limit: Maximum number of ranked results.
            exclude_active_turn: When True, the currently active turn is never returned.
            active_session_id: The session ID of the caller (defaults to session_id if given).
            active_turn_id: Explicit turn ID to exclude.
            active_turn_index: Explicit turn index to exclude.
            min_vector_score: Minimum cosine similarity for vector candidates.
        """
        target_active_session = active_session_id or session_id
        match_query = _sanitize_fts_query(query)

        # 1. Lexical retrieval via SQLite FTS5 (BM25)
        lexical_ranks: dict[str, int] = {}
        with self._connect() as db:
            if match_query:
                params: list[Any] = [match_query]
                where_clauses = ["conversation_chunks_fts MATCH ?"]

                if session_id:
                    where_clauses.append("c.session_id = ?")
                    params.append(session_id)

                if exclude_active_turn:
                    where_clauses.append("c.is_active = 0")
                    if target_active_session and active_turn_id:
                        where_clauses.append("c.id != ?")
                        params.append(active_turn_id)
                    if target_active_session and active_turn_index is not None:
                        where_clauses.append("(c.session_id != ? OR c.turn_index != ?)")
                        params.extend([target_active_session, active_turn_index])

                where_sql = " AND ".join(where_clauses)
                sql = f"""
                SELECT f.id, bm25(conversation_chunks_fts)
                FROM conversation_chunks_fts f
                JOIN conversation_chunks c ON c.id = f.id
                WHERE {where_sql}
                ORDER BY bm25(conversation_chunks_fts) ASC
                LIMIT 50
                """
                try:
                    rows = db.execute(sql, params).fetchall()
                    for rank, (cid, _) in enumerate(rows):
                        lexical_ranks[cid] = rank
                except sqlite3.OperationalError:
                    lexical_ranks = {}

            # 2. Dense vector candidates retrieval
            vector_params: list[Any] = []
            vec_where = ["embedding IS NOT NULL"]

            if session_id:
                vec_where.append("session_id = ?")
                vector_params.append(session_id)

            if exclude_active_turn:
                vec_where.append("is_active = 0")
                if target_active_session and active_turn_id:
                    vec_where.append("id != ?")
                    vector_params.append(active_turn_id)
                if target_active_session and active_turn_index is not None:
                    vec_where.append("(session_id != ? OR turn_index != ?)")
                    vector_params.extend([target_active_session, active_turn_index])

            vec_where_sql = " AND ".join(vec_where)
            vec_rows = db.execute(
                f"SELECT id, embedding FROM conversation_chunks WHERE {vec_where_sql}",
                vector_params,
            ).fetchall()

        # 3. Dense scoring
        vector_scores: dict[str, float] = {}
        vector_ranks: dict[str, int] = {}
        query_vector = self._get_embedding(query)

        if query_vector and vec_rows:
            scored: list[tuple[float, str]] = []
            for cid, blob in vec_rows:
                candidate_vec = _unpack_vector(blob)
                sim = _cosine_similarity(query_vector, candidate_vec)
                if sim >= min_vector_score:
                    scored.append((sim, cid))
            scored.sort(key=lambda x: x[0], reverse=True)
            for rank, (sim, cid) in enumerate(scored[:50]):
                vector_scores[cid] = sim
                vector_ranks[cid] = rank

        # 4. Reciprocal Rank Fusion (RRF)
        all_candidate_ids = set(lexical_ranks.keys()) | set(vector_ranks.keys())
        if not all_candidate_ids:
            return []

        rrf_scores: dict[str, float] = {}
        for cid in all_candidate_ids:
            score = 0.0
            if cid in lexical_ranks:
                score += 1.0 / (RRF_K + lexical_ranks[cid] + 1)
            if cid in vector_ranks:
                score += 1.0 / (RRF_K + vector_ranks[cid] + 1)
            rrf_scores[cid] = score

        best_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)[:limit]

        # 5. Hydrate full chunks
        with self._connect() as db:
            placeholders = ",".join("?" * len(best_ids))
            chunks_rows = db.execute(
                f"""
                SELECT id, session_id, turn_index, role, user_text, assistant_text,
                       created_at, text, metadata, is_active
                FROM conversation_chunks
                WHERE id IN ({placeholders})
                """,
                best_ids,
            ).fetchall()

        chunks_by_id = {
            r[0]: TurnChunk(
                id=r[0],
                session_id=r[1],
                turn_index=r[2],
                role=r[3],
                user_text=r[4],
                assistant_text=r[5],
                created_at=r[6],
                text=r[7],
                metadata=json.loads(r[8]) if r[8] else {},
                is_active=bool(r[9]),
            )
            for r in chunks_rows
        }

        results: list[TurnSearchResult] = []
        for cid in best_ids:
            if cid in chunks_by_id:
                results.append(
                    TurnSearchResult(
                        id=cid,
                        session_id=chunks_by_id[cid].session_id,
                        turn_index=chunks_by_id[cid].turn_index,
                        created_at=chunks_by_id[cid].created_at,
                        role=chunks_by_id[cid].role,
                        score=rrf_scores[cid],
                        chunk=chunks_by_id[cid],
                        lexical_rank=lexical_ranks.get(cid),
                        vector_score=vector_scores.get(cid),
                    )
                )

        return results


class SearchConversationsInput(BaseModel):
    query: str = Field(
        description="Search keywords or question to retrieve from previous conversation history."
    )
    session_id: str | None = Field(
        default=None, description="Optional session ID filter to limit search."
    )
    limit: int = Field(default=5, description="Maximum number of historical turns to return.")


class ReadTurnInput(BaseModel):
    session_id: str = Field(description="The session ID where the turn took place.")
    turn_id: str = Field(description="The turn ID or turn index number to read in full.")


def build_conversation_memory_tools(
    memory: ConversationMemory,
    *,
    active_session_id: str | None = None,
    active_turn_index: int | None = None,
) -> dict[str, Any]:
    """Exposes search_conversations and read_turn as agent-callable tools with
    automatic active-turn exclusion.
    """

    async def search_conversations(
        query: str, session_id: str | None = None, limit: int = 5
    ) -> str:
        """Search earlier conversation history for relevant facts, decisions, and discussions.

        Automatically excludes the active turn to prevent echo loops.
        """
        hits = memory.search_conversations(
            query=query,
            session_id=session_id,
            limit=limit,
            exclude_active_turn=True,
            active_session_id=active_session_id,
            active_turn_index=active_turn_index,
        )
        if not hits:
            return "No matching conversation history found."
        excerpts = [
            f"Result {i + 1} (score {h.score:.3f}):\n{h.snippet}" for i, h in enumerate(hits)
        ]
        return "\n\n---\n\n".join(excerpts)

    async def read_turn(session_id: str, turn_id: str) -> str:
        """Read the full dated chunk of a specific turn from conversation history."""
        chunk = memory.read_turn(session_id=session_id, turn_id=turn_id)
        if not chunk:
            return f"Turn {turn_id} not found in session {session_id}."
        return chunk.formatted

    return {
        "search_conversations": search_conversations,
        "read_turn": read_turn,
    }
