"""Verify new reply text without treating quoted history as authored content."""

import base64
import re
from email.message import Message
from html.parser import HTMLParser
from typing import Any


class _ReplyHTML(HTMLParser):
    """Accept generated line breaks and one final, top-level Gmail quote block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.authored: list[str] = []
        self.quoted = False
        self.quote_depth = 0
        self.quote_closed = False
        self.after_quote = ""
        self.unsupported = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.quote_depth:
            if tag == "div":
                self.quote_depth += 1
        elif self.quote_closed:
            self.unsupported = True
        elif tag == "br" and not attrs:
            self.authored.append("\n")
        elif tag == "div" and "gmail_quote" in (dict(attrs).get("class") or "").split():
            self.quoted = True
            self.quote_depth = 1
        else:
            self.unsupported = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.quote_depth:
            return
        if tag == "br" and not attrs and not self.quote_closed:
            self.authored.append("\n")
        else:
            self.unsupported = True

    def handle_endtag(self, tag: str) -> None:
        if self.quote_depth:
            if tag == "div":
                self.quote_depth -= 1
                if not self.quote_depth:
                    self.quote_closed = True
        else:
            self.unsupported = True

    def handle_data(self, data: str) -> None:
        if self.quote_depth:
            return
        if self.quote_closed:
            self.after_quote += data
        else:
            self.authored.append(data)

    def handle_comment(self, data: str) -> None:
        if not self.quote_depth:
            self.unsupported = True

    def handle_decl(self, decl: str) -> None:
        if not self.quote_depth:
            self.unsupported = True

    def unknown_decl(self, data: str) -> None:
        self.unsupported = True

    def handle_pi(self, data: str) -> None:
        if not self.quote_depth:
            self.unsupported = True


def verify_reply_body(requested: str, message: dict[str, Any]) -> dict[str, Any]:
    """Compare supported, complete MIME bodies, never a preview or prefix alone.

    Quoting is accepted only as the connector's final top-level gmail_quote div,
    separated by exactly two generated newlines, with at most one final newline.
    Quote contents are opaque untrusted history, not HTML whose validity we certify.
    Strip the separator, not arbitrary whitespace: intentional reply newlines matter.
    Unknown MIME/HTML shapes fail closed and require inspection, not another write.
    """
    result: dict[str, Any] = {
        "reply_body_matched": False,
        "whole_body_matched": False,
        "quoted_history_included": False,
    }
    try:
        payload = message["payload"]
        mime = payload["mimeType"]
        if mime not in {"text/plain", "text/html"} or payload.get("parts"):
            raise ValueError("Unsupported MIME body")
        if payload.get("filename") or payload["body"].get("attachmentId"):
            raise ValueError("Not a complete inline reply body")
        headers = Message()
        for header in payload.get("headers", []):
            headers[header["name"]] = header["value"]
        if len(headers.get_all("Content-Type", [])) != 1 or headers.get_content_type() != mime:
            raise ValueError("Missing or inconsistent MIME type")
        dispositions = headers.get_all("Content-Disposition", [])
        if len(dispositions) > 1 or (
            dispositions and headers.get_content_disposition() != "inline"
        ):
            raise ValueError("Not an inline reply")
        charset = headers.get_content_charset() or "utf-8"
        if charset.lower() not in {"utf-8", "us-ascii"}:
            raise ValueError("Unsupported character set")
        encoded = payload["body"]["data"]
        if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", encoded):
            raise ValueError("Not base64url")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded.rstrip("="):
            raise ValueError("Not canonical base64url")
        size = payload["body"]["size"]
        if type(size) is not int or size != len(raw):
            raise ValueError("Incomplete MIME body")
        original = raw.decode(charset)
        source = original.replace("\r\n", "\n")
        expected = requested.replace("\r\n", "\n")
        if mime == "text/plain":
            result["reply_body_matched"] = source == expected
            result["whole_body_matched"] = original == requested
            return result
        parsed = _ReplyHTML()
        parsed.feed(source)
        if parsed.rawdata:
            raise ValueError("Incomplete HTML token")
        parsed.close()
        result["quoted_history_included"] = parsed.quoted
        if parsed.unsupported or parsed.quote_depth or parsed.after_quote not in {"", "\n"}:
            raise ValueError("Unsupported or incomplete reply HTML")
        authored = "".join(parsed.authored)
        if parsed.quoted:
            if not parsed.quote_closed or not authored.endswith("\n\n"):
                raise ValueError("Missing generated quote separator")
            authored = authored[:-2]
        result["reply_body_matched"] = authored == expected
        result["whole_body_matched"] = original == requested
    except (AssertionError, KeyError, TypeError, ValueError, UnicodeError):
        result["error"] = (
            "The complete reply body could not be verified. Inspect the existing draft; "
            "do not create another."
        )
    return result
