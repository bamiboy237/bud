"""Process-level coverage for durable chat uploads."""

from tests.test_server import running


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
