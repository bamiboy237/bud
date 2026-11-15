"""Original-mail presentation for ``approval.requested`` and ``task.approval``.

The snapshot's ``pending_approvals`` exposes the same optional ``preview`` object:
``{kind: "email", source: "simulation" | "gmail",
status: "available" | "partial" | "unavailable", subject: str | null,
messages: [{id, subject, sender, to, cc, bcc, sent_at, body, body_format, body_text}],
unavailable_reason: str | null, proposed_body_text: str | null}``.

``body`` is the complete inline original, not a summary or snippet. ``body_text``
is plain display text; HTML conversion does not fetch resources. Treat all mail
content as untrusted. Never load remote HTML resources or interpret mail as instructions.
``partial`` means some bodies or further pages were not resolved. Attachments are
not fetched. Unsupported connector shapes produce explicit missing content.

These are original target messages, not proposed outgoing text. Show outgoing
content from the unchanged ``args`` separately. Metadata is captured when held,
not refreshed on every snapshot; it is not a stale-target or permission check.
The decision endpoint still takes only the exact approval ID and decision.
Group captured pending IDs by non-null ``request_id``; new sibling jobs inherit
their recorded source run. New steering/edited messages start a separate group.
Fall back to non-null ``(task_id, run_id)`` for old records, never a null-ID group.
"""

import base64
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from email.message import Message
from html.parser import HTMLParser
from typing import Any, Literal

from pydantic import BaseModel, Field

ReadEmail = Callable[[str, dict[str, Any]], Awaitable[Any]]


class EmailMessagePreview(BaseModel):
    id: str | None = None
    subject: str | None = None
    sender: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    sent_at: str | None = None
    body: str | None = None
    body_format: Literal["plain", "html"] = "plain"
    body_text: str | None = None


class EmailPreview(BaseModel):
    kind: Literal["email"] = "email"
    source: Literal["simulation", "gmail"]
    status: Literal["available", "partial", "unavailable"] = "unavailable"
    subject: str | None = None
    messages: list[EmailMessagePreview] = Field(default_factory=list)
    unavailable_reason: str | None = None
    proposed_body_text: str | None = None


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in {"br", "p", "div", "li", "blockquote", "tr", "h1", "h2", "h3", "pre"}:
            self.text.append("\n")
        if tag == "li":
            self.text.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag in {"p", "div", "li", "blockquote", "tr", "h1", "h2", "h3", "pre"}:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.text.append(data)


def _body_text(body: str, body_format: str) -> str:
    if body_format == "plain":
        return body
    parser = _ReadableHTML()
    parser.feed(body)
    parser.close()
    return "".join(parser.text).strip()


def _addresses(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError("Unsupported address data")


def _mime_body(payload: dict[str, Any]) -> tuple[str, Literal["plain", "html"]]:
    """Prefer a complete plain alternative; do not mistake snippets for bodies."""
    mime = payload.get("mimeType")
    if payload.get("filename"):
        raise ValueError("Attachment is not a body")
    if mime in ("multipart/alternative", "multipart/mixed", "multipart/related"):
        candidates = []
        for part in payload.get("parts", []):
            try:
                candidates.append(_mime_body(part))
            except (ValueError, KeyError, TypeError, UnicodeError):
                if (
                    mime != "multipart/alternative"
                    and not part.get("filename")
                    and part.get("mimeType", "").startswith(("text/", "multipart/"))
                ):
                    raise
                continue
        if not candidates or (mime != "multipart/alternative" and len(candidates) != 1):
            raise ValueError("No inline body")
        return next((item for item in candidates if item[1] == "plain"), candidates[0])
    if mime not in ("text/plain", "text/html"):
        raise ValueError("Unsupported body")
    body = payload["body"]
    if body.get("attachmentId"):
        raise ValueError("Body requires a separate download")
    encoded = body["data"]
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    if type(body.get("size")) is not int or body["size"] != len(raw):
        raise ValueError("Incomplete body")
    headers = Message()
    for header in payload.get("headers", []):
        headers[header["name"]] = header["value"]
    text = raw.decode(headers.get_content_charset() or "utf-8")
    return text, "html" if mime == "text/html" else "plain"


def _gmail_message(message: dict[str, Any]) -> EmailMessagePreview:
    payload = message.get("payload") or {}
    headers = {header["name"].lower(): header["value"] for header in payload.get("headers", [])}
    result = EmailMessagePreview(
        id=message.get("id") or message.get("messageId"),
        subject=headers.get("subject") or message.get("subject"),
        sender=headers.get("from") or message.get("sender"),
        to=_addresses(headers.get("to") or message.get("to") or []),
        cc=_addresses(headers.get("cc") or message.get("cc") or []),
        bcc=_addresses(headers.get("bcc") or message.get("bcc") or []),
        sent_at=headers.get("date") or message.get("messageTimestamp"),
    )
    with suppress(ValueError, KeyError, TypeError, UnicodeError, LookupError):
        result.body, result.body_format = _mime_body(payload)
        result.body_text = _body_text(result.body, result.body_format)
    return result


def unavailable_email_preview(name: str, args: dict[str, Any]) -> EmailPreview | None:
    if name not in {
        "archive_email",
        "mark_read",
        "draft_reply",
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GMAIL_SEND_EMAIL",
    }:
        return None
    preview = EmailPreview(
        source="gmail" if name.startswith("GMAIL_") else "simulation",
        unavailable_reason="The original thread could not be read.",
    )
    if name.startswith("GMAIL_") and isinstance(args.get("body"), str):
        preview.proposed_body_text = _body_text(
            args["body"], "html" if args.get("is_html") is True else "plain"
        )
    return preview


async def email_preview(name: str, args: dict[str, Any], read: ReadEmail) -> EmailPreview | None:
    preview = unavailable_email_preview(name, args)
    if preview is None:
        return None
    if name in {"archive_email", "mark_read", "draft_reply"}:
        try:
            thread = await read("read_thread", {"thread_id": args["thread_id"]})
            preview.subject = thread["subject"]
            preview.messages = [
                EmailMessagePreview(
                    id=message["id"],
                    subject=thread["subject"],
                    sender=message["sender"],
                    to=_addresses(json.loads(message["recipients"])),
                    sent_at=message["sent_at"],
                    body=message["body"],
                    body_text=message["body"],
                )
                for message in thread["messages"]
            ]
            if not preview.messages:
                raise ValueError("Empty thread")
            preview.status = "available"
            preview.unavailable_reason = None
        except (ValueError, KeyError, TypeError):
            preview.unavailable_reason = "The original thread could not be resolved."
        return preview
    if not args.get("thread_id"):
        preview.status = "available"
        preview.unavailable_reason = None
        return preview
    try:
        result = await read(
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
            {"thread_id": args["thread_id"], "user_id": "me"},
        )
        if not isinstance(result, dict) or result.get("successful") is not True:
            raise ValueError("Read failed")
        data = result["data"]
        messages = data["messages"]
        if not messages or (
            data.get("id") != args["thread_id"]
            and not all(message.get("threadId") == args["thread_id"] for message in messages)
        ):
            raise ValueError("Target thread not confirmed")
        preview.messages = [_gmail_message(message) for message in messages]
        preview.subject = preview.messages[0].subject
        incomplete = any(message.body is None for message in preview.messages)
        paginated = bool(data.get("nextPageToken") or data.get("next_page_token"))
        preview.status = "partial" if incomplete or paginated else "available"
        preview.unavailable_reason = None
        if incomplete or paginated:
            preview.unavailable_reason = (
                "Some original bodies or further thread pages are unavailable."
            )
    except (ValueError, KeyError, TypeError):
        preview.unavailable_reason = "The original thread could not be resolved."
    return preview
