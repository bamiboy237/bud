"""Process-level coverage for durable chat uploads."""

import base64

import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, TextContent, UserPromptPart
from pydantic_ai.profiles import ModelProfile

from bud.core.projection import Attachment
from bud.workspace import save_attachment
from tests.runner_support import reply, running_bud
from tests.test_server import completed_turn, running

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="
)


def test_durable_model_receives_uploaded_image_after_restart(tmp_path):
    file = {"id": "c" * 32, "name": "test.png"}
    with running(tmp_path) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        path = f"/sessions/{session}/attachments/{file['id']}"
        assert (
            server.client.put(path, params={"name": file["name"]}, content=PNG).status_code == 204
        )
        server.request(
            "POST",
            f"/sessions/{session}/messages",
            status=202,
            json={"text": "Inspect the attached PNG.", "message_id": "png", "attachments": [file]},
        )
        first = server.wait(
            lambda: next(iter(server.events("run.completed")), None),
            "vision request completed",
        )
        assert "Visible PNG images: 1" in str(first["messages"])
        assert base64.urlsafe_b64encode(PNG).decode() not in str(first["messages"])

    with running(tmp_path) as server:
        result = completed_turn(server, session, "Look at the same image again.", "follow-up", 2)
        assert "Visible PNG images: 1" in str(result.data["messages"])


@pytest.mark.parametrize("vision", [True, False])
async def test_model_gets_the_same_image_or_local_url_on_a_followup(tmp_path, vision):
    session = "owner"
    image = Attachment(id="a" * 32, name="Stats question.png")
    notes = Attachment(id="b" * 32, name="notes.md")
    seen = []

    def respond(messages, _info):
        seen.append(
            [
                item
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, list)
                for item in part.content
            ]
        )
        return reply()

    profile = ModelProfile(supports_image_input=vision)
    async with running_bud(tmp_path, respond, model_profile=profile) as bud:
        bud.log.append(session, "session.created")
        save_attachment(bud.root, image.path(session), PNG)
        save_attachment(bud.root, notes.path(session), b"Sample notes")
        bud.runner.start(session, "", "upload", attachments=[image, notes])
        await bud.settle()
        assert len(seen) == 1
        expected_uri = (bud.root / image.path(session)).as_uri()
        assert "%20" in expected_uri
        completed = next(event for event in bud.log.read(session) if event.type == "run.completed")
        stored = bud.log.conversation(session, completed.data["run_id"])
        assert stored is not None
        assert base64.urlsafe_b64encode(PNG).decode() not in stored
        assert expected_uri not in stored

    async with running_bud(tmp_path, respond, model_profile=profile) as bud:
        bud.runner.start(session, "Is that one or two tailed?", "follow-up")
        await bud.settle()

    assert len(seen) == 2
    for items in seen:
        images = [item for item in items if isinstance(item, BinaryContent)]
        urls = [item for item in items if isinstance(item, str) and expected_uri in item]
        assert [(item.data, item.media_type) for item in images] == (
            [(PNG, "image/png")] if vision else []
        )
        assert bool(urls) is not vision
        assert any(
            isinstance(item, TextContent) and notes.path(session) in item.content for item in items
        )


def test_chat_files_survive_restart_with_scoped_idempotent_upload(tmp_path):
    file = {"id": "a" * 32, "name": "sample.csv"}
    data = b"item,total\nbooks,4\n"
    body = {"text": "", "message_id": "files", "attachments": [file]}
    with running(tmp_path) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        other = server.request("POST", "/sessions", status=201)["session_id"]
        path = f"/sessions/{session}/attachments/{file['id']}"
        query = {"name": file["name"]}
        for _ in range(2):
            assert server.client.put(path, params=query, content=data).status_code == 204
        assert server.client.put(path, params=query, content=b"changed").status_code == 409
        assert (
            server.client.put(path, params={"name": "../sample.csv"}, content=data).status_code
            == 422
        )
        assert (
            server.client.get(path, params=query, headers={"Authorization": ""}).status_code == 401
        )
        assert server.client.get(path.replace(session, other), params=query).status_code == 404
        server.request("POST", f"/sessions/{other}/messages", status=422, json=body)
        assert server.request("POST", f"/sessions/{session}/messages", status=202, json=body) == {
            "duplicate": False
        }
        assert server.request("POST", f"/sessions/{session}/messages", status=202, json=body) == {
            "duplicate": True
        }
        server.wait(
            lambda: next(
                (turn for turn in server.snapshot(session)["transcript"] if turn["attachments"]),
                None,
            ),
            "attachment appears in chat",
        )
        run = server.events("run.queued")[0]
        assert run["request"] == ""
        assert run["attachment_paths"] == [f"attachments/{session}/{file['id']}/sample.csv"]

    with running(tmp_path) as server:
        assert server.snapshot(session)["transcript"][0]["attachments"] == [file]
        response = server.client.get(path, params=query)
        assert (
            response.content == data
            and response.headers["content-type"] == "application/octet-stream"
        )
        preview = server.request("GET", path, params={**query, "preview": "true"})
        assert "books" in preview["markdown"]
