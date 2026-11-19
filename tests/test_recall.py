"""Recall finds earlier turns, tags them as citable sources, and skips what is in context."""

from datetime import UTC, date, datetime

from bud.harness.recall import Hit, Recall, plain_citations, render, source_label


async def test_search_includes_this_conversation_except_turns_still_in_context(tmp_path):
    recall = Recall(tmp_path / "recall.db", embedder=None, memory=None)
    recall.add_turn("chat", "old", "Marigold review moved to Tuesday", "Noted.")
    recall.add_turn("chat", "recent", "Marigold review agenda is ready", "Great.")

    hits = await recall.search("Marigold review", exclude_turns=["turn:recent"])

    # The same conversation counts: its older turn may have been compacted away.
    assert [hit.id for hit in hits] == ["turn:old"]
    assert hits[0].session_id == "chat"


def test_render_tags_each_earlier_turn_and_returns_its_source():
    hits = [
        Hit("mem:1", "memory", None, "Prefers mornings", 1.0),
        Hit("turn:a", "conversation", "chat", "Owner: We chose Postgres", 0.9),
        Hit("turn:b", "conversation", "other", "Owner: Correction: SQLite", 0.8),
    ]
    sources = {
        "turn:a": (7, datetime(2026, 11, 18, 9, tzinfo=UTC)),
        "turn:b": (13, datetime(2025, 3, 2, 9, tzinfo=UTC)),
    }

    text, cited = render(hits, sources, date(2026, 11, 19))

    assert "- (memory) Prefers mornings" in text
    assert "- [S1] (earlier conversation, 2026-11-18) Owner: We chose Postgres" in text
    assert "- [S2] (earlier conversation, 2025-03-02) Owner: Correction: SQLite" in text
    assert cited == {
        "S1": {"session_id": "chat", "seq": 7, "date": "2026-11-18", "label": "Nov 18"},
        "S2": {"session_id": "other", "seq": 13, "date": "2025-03-02", "label": "Mar 2, 2025"},
    }


def test_render_drops_an_excerpt_whose_original_is_gone():
    hits = [Hit("turn:gone", "conversation", "chat", "Owner: deleted", 1.0)]
    assert render(hits, {}, date(2026, 11, 19)) == ("", {})


def test_plain_clients_see_dates_and_never_an_invented_tag():
    sources = {"S1": {"label": "Nov 18"}}
    reply = "We chose SQLite [S1], not Postgres [S7]."
    assert plain_citations(reply, sources) == "We chose SQLite (Nov 18), not Postgres."
    assert plain_citations(reply, None) == "We chose SQLite, not Postgres."


def test_labels_name_the_year_only_when_it_differs():
    assert source_label(date(2026, 1, 5), date(2026, 11, 19)) == "Jan 5"
    assert source_label(date(2025, 12, 31), date(2026, 1, 2)) == "Dec 31, 2025"


def test_tags_stay_unique_across_a_conversation():
    known = {"S1": {"session_id": "chat", "seq": 7, "date": "2026-11-18", "label": "Nov 18"}}
    hits = [
        Hit("turn:new", "conversation", "other", "Owner: A new decision", 1.0),
        Hit("turn:a", "conversation", "chat", "Owner: We chose Postgres", 0.9),
    ]
    sources = {
        "turn:new": (3, datetime(2026, 11, 19, 9, tzinfo=UTC)),
        "turn:a": (7, datetime(2026, 11, 18, 9, tzinfo=UTC)),
    }

    text, cited = render(hits, sources, date(2026, 11, 19), known)

    # A source seen before keeps its tag; a new one never reuses an older reply's tag.
    assert cited["S1"]["seq"] == 7
    assert cited["S2"]["session_id"] == "other"
    assert "- [S2] (earlier conversation, 2026-11-19) Owner: A new decision" in text
