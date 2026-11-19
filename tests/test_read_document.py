"""read_document turns workspace documents into Markdown on this machine, with the same path
rules as Bud's other workspace reads."""

import io
import zipfile

import pytest

from bud import workspace
from bud.workspace import document_tool

DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats'
        '-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    ),
    "_rels/.rels": (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    ),
    "word/document.xml": (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>The lease renews on 3 March.'
        "</w:t></w:r></w:p></w:body></w:document>"
    ),
}


def docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in DOCX_PARTS.items():
            archive.writestr(name, text)
    return buffer.getvalue()


@pytest.fixture
def read(tmp_path):
    root = tmp_path / "workspace"
    (root / "reports").mkdir(parents=True)
    return root, document_tool(root).function


def test_word_and_csv_files_read_as_markdown(read):
    root, read_document = read
    (root / "reports" / "lease.docx").write_bytes(docx())
    (root / "dates.csv").write_text("name,date\nLease,3 March\n")

    assert read_document("reports/lease.docx") == {
        "ok": True,
        "path": "reports/lease.docx",
        "markdown": "The lease renews on 3 March.\n",
    }
    assert "| Lease | 3 March |" in read_document("dates.csv")["markdown"]


def test_unreadable_or_disallowed_files_are_refused(read, tmp_path, monkeypatch):
    root, read_document = read
    (root / "notes.docx").write_bytes(b"not a document at all")
    assert read_document("notes.docx")["ok"] is False
    assert read_document("missing.pdf")["ok"] is False

    # The same path rules as read_file: no hidden, private, or escaping paths, no links.
    (root / ".hidden.docx").write_bytes(docx())
    (root / "secret.docx").write_bytes(docx())
    outside = tmp_path / "outside.docx"
    outside.write_bytes(docx())
    (root / "linked.docx").symlink_to(outside)
    for path in (".hidden.docx", "secret.docx", "../outside.docx", "linked.docx"):
        assert read_document(path)["ok"] is False, path

    monkeypatch.setattr(workspace, "_DOCUMENT_BYTES", 100)
    (root / "big.docx").write_bytes(docx())
    assert "limit" in read_document("big.docx")["error"]
