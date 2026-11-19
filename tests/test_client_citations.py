"""The terminal client prints a reply's citation tags as dates, even while it streams."""

from bud.core.client import StreamedReply

SOURCES = {"S1": {"label": "Nov 18"}}


def stream(chunks: list[str], reply: str, sources: dict | None) -> str:
    streamed = StreamedReply()
    printed = "".join(streamed.feed(chunk) for chunk in chunks)
    return printed + streamed.finish(reply, sources)


def test_a_tag_split_across_chunks_prints_as_its_date():
    chunks = ["We chose SQ", "Lite ", "[S", "1], not Post", "gres."]
    reply = "We chose SQLite [S1], not Postgres."
    assert stream(chunks, reply, SOURCES) == "We chose SQLite (Nov 18), not Postgres."


def test_text_before_a_tag_prints_as_it_streams():
    streamed = StreamedReply()
    assert streamed.feed("Tuesday, as you said ") == "Tuesday, as you said"
    assert streamed.feed("[S1].") == ""
    assert streamed.finish("Tuesday, as you said [S1].", SOURCES) == " (Nov 18)."


def test_an_unknown_tag_and_a_lost_stream_still_end_correctly():
    assert stream(["Tuesday [S7]."], "Tuesday [S7].", SOURCES) == "Tuesday."
    # Part of the stream never arrived: the stored reply is printed whole.
    assert stream(["Tues"], "Monday [S1].", SOURCES) == "Tues\n[complete reply]\nMonday (Nov 18)."


def test_a_replaced_response_does_not_force_a_full_reprint():
    streamed = StreamedReply()
    streamed.feed("First try, abandoned ")
    streamed.discard()  # text.reset for a retried request
    printed = streamed.feed("Tuesday [S1].")
    assert printed + streamed.finish("Tuesday [S1].", SOURCES) == "Tuesday (Nov 18)."
