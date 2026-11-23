"""Recall finds earlier turns, tags them as citable sources, and skips what is in context."""

from datetime import UTC, date, datetime

from bud.harness.recall import MAX_TURN_CHARS, Hit, Recall, plain_citations, render, source_label


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


async def test_workspace_files_are_indexed_updated_and_removed_incrementally(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "reports").mkdir(parents=True)
    note = workspace / "notes" / "schedule.md"
    report = workspace / "reports" / "2026-11-19-job-1-weekly.md"
    note.write_text("Marigold kickoff scheduled for Monday.")
    report.write_text("# Weekly Summary\n\nCedar launch targets Q4.")

    recall = Recall(tmp_path / "recall.db", embedder=None, memory=None, workspace=workspace)
    await recall.index_workspace()

    hits = await recall.search("Marigold")
    assert [(h.source, h.path) for h in hits] == [("workspace", "notes/schedule.md")]
    assert [(h.source, h.path) for h in await recall.search("Cedar launch")] == [
        ("workspace", "reports/2026-11-19-job-1-weekly.md")
    ]

    # Updating a note replaces its old content in recall.
    note.write_text("Saffron review scheduled for Wednesday.")
    await recall.index_workspace()
    assert await recall.search("Marigold") == []
    assert [(h.source, h.path) for h in await recall.search("Saffron")] == [
        ("workspace", "notes/schedule.md")
    ]

    # Deleting a note removes it from recall.
    note.unlink()
    await recall.index_workspace()
    assert await recall.search("Saffron") == []


async def test_workspace_recall_respects_privacy_binary_and_symlink_rules(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_target = outside / "outside.md"
    secret_target.write_text("outsidesymlinkalpha")

    (workspace / ".env").write_text("envsecretbravo")
    (workspace / ".env.local").write_text("envlocalsecretcharlie")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("gitconfigdelta")
    (workspace / "secrets.md").write_text("secretsfileecho")
    (workspace / "api-key.txt").write_text("apikeyfoxtrot")
    (workspace / "auth_token.json").write_text("authtokengolf")
    (workspace / "AGENTS.md").write_text("agentsfilehotel")
    (workspace / "private").mkdir()
    (workspace / "private" / "diary.md").write_text("privatedirindia")
    (workspace / "credentials").mkdir()
    (workspace / "credentials" / "creds.txt").write_text("credentialsdirjuliet")
    (workspace / "binary.bin").write_bytes(b"binarynulkilo\x00payload")
    (workspace / "bad_utf8.dat").write_bytes(b"\xff\xfebadutf8lima")
    (workspace / "link.md").symlink_to(secret_target)
    (workspace / "linked_dir").symlink_to(outside, target_is_directory=True)
    (workspace / "notes").mkdir()
    (workspace / "notes" / "visible.md").write_text("visibleworkspacemike")

    recall = Recall(tmp_path / "recall.db", embedder=None, memory=None, workspace=workspace)
    await recall.index_workspace()

    for forbidden in (
        "envsecretbravo",
        "envlocalsecretcharlie",
        "gitconfigdelta",
        "secretsfileecho",
        "apikeyfoxtrot",
        "authtokengolf",
        "agentsfilehotel",
        "privatedirindia",
        "credentialsdirjuliet",
        "binarynulkilo",
        "badutf8lima",
        "outsidesymlinkalpha",
    ):
        assert await recall.search(forbidden) == [], forbidden

    hits = await recall.search("visibleworkspacemike")
    assert [(h.source, h.path) for h in hits] == [("workspace", "notes/visible.md")]


async def test_large_workspace_file_splits_into_bounded_chunks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    p1 = "Alpha section " + ("a " * 240)
    p2 = "Omega section " + ("b " * 240)
    (workspace / "long.md").write_text(f"{p1}\n\n{p2}")

    recall = Recall(tmp_path / "recall.db", embedder=None, memory=None, workspace=workspace)
    await recall.index_workspace()

    alpha_hits = await recall.search("Alpha")
    omega_hits = await recall.search("Omega")
    assert len(alpha_hits) == 1
    assert len(omega_hits) == 1
    assert "Alpha section" in alpha_hits[0].text
    assert "Omega section" in omega_hits[0].text
    assert len(alpha_hits[0].text) <= MAX_TURN_CHARS
    assert len(omega_hits[0].text) <= MAX_TURN_CHARS


def test_render_tags_workspace_files_and_keeps_mixed_sources_unique():
    known = {
        "S1": {"session_id": "chat", "seq": 7, "date": "2026-11-18", "label": "Nov 18"},
        "S2": {
            "kind": "workspace",
            "path": "notes/old.md",
            "date": "2026-11-18",
            "label": "notes/old.md",
        },
    }
    hits = [
        Hit("ws:1:0", "workspace", None, "First chunk", 1.0, path="reports/weekly.md"),
        Hit("ws:1:1", "workspace", None, "Second chunk", 0.9, path="reports/weekly.md"),
        Hit("ws:old:0", "workspace", None, "Old note", 0.8, path="notes/old.md"),
        Hit("ws:gone:0", "workspace", None, "Deleted note", 0.7, path="notes/deleted.md"),
        Hit("turn:a", "conversation", "chat", "Owner: Postgres", 0.6),
    ]
    turn_sources = {"turn:a": (7, datetime(2026, 11, 18, 9, tzinfo=UTC))}
    ws_dates = {
        "reports/weekly.md": date(2026, 11, 19),
        "notes/old.md": date(2026, 11, 18),
    }

    text, cited = render(hits, turn_sources, date(2026, 11, 19), known, workspace_dates=ws_dates)

    assert "- [S3] (workspace file reports/weekly.md, 2026-11-19) First chunk" in text
    assert "- [S3] (workspace file reports/weekly.md, 2026-11-19) Second chunk" in text
    assert "- [S2] (workspace file notes/old.md, 2026-11-18) Old note" in text
    assert "Deleted note" not in text
    assert "- [S1] (earlier conversation, 2026-11-18) Owner: Postgres" in text
    assert cited["S1"]["seq"] == 7
    assert cited["S2"]["path"] == "notes/old.md"
    assert cited["S3"] == {
        "kind": "workspace",
        "path": "reports/weekly.md",
        "date": "2026-11-19",
        "label": "reports/weekly.md",
    }
