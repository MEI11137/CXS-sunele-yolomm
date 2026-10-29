"""PDF/DOCX parsing and citation search (C-09).

First version handles PDF and DOCX only. Legacy ``.doc`` is refused with a
conversion hint because silently mis-parsing it would be worse than saying no.

PDF text is read from the page content streams in page order, so a citation's
page number is the real page number. Scanned pages have no text layer; they are
reported per page and the document becomes ``partial`` rather than pretending
the text exists. DOCX has no page concept, so it is anchored by paragraph and
table ordinal only — no invented page numbers.

Parsing stores text, tables and anchors; it never edits or re-lays-out the
source document, and every read of a quote goes through the caller's own
permission check upstream.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SUPPORTED = {
    ".pdf": ("pdf", "pypdf"),
    ".docx": ("docx", "ooxml-word"),
}
CONVERT_HINT = "旧版 .doc 请先另存为 .docx 再解析"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(database_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            asset_id TEXT,
            project_id TEXT,
            owner_id INTEGER,
            title TEXT,
            original_name TEXT,
            format TEXT NOT NULL,
            status TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0,
            block_count INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            error TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (project_id, sha256)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS document_blocks (
            document_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            block_type TEXT NOT NULL,
            page_no INTEGER,
            paragraph_no INTEGER,
            table_no INTEGER,
            text TEXT NOT NULL,
            PRIMARY KEY (document_id, ordinal)
        )"""
    )


def describe_source(asset: dict) -> dict:
    suffix = Path(str(asset.get("original_name") or asset.get("object_key") or "")).suffix.lower()
    if suffix == ".doc":
        return {"format": "doc", "parser": None, "supported": False, "reason": CONVERT_HINT}
    entry = SUPPORTED.get(suffix)
    if entry is None:
        return {
            "format": suffix.lstrip(".") or "unknown",
            "parser": None,
            "supported": False,
            "reason": f"暂不支持的文档格式 {suffix or '(无扩展名)'}，首版支持 PDF 与 DOCX",
        }
    return {"format": entry[0], "parser": entry[1], "supported": True, "reason": None}


# ------------------------------------------------------------------------ PDF


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    objects: dict[int, bytes] = {}
    for match in re.finditer(rb"(\d+)\s+\d+\s+obj\b(.*?)endobj", data, re.S):
        objects[int(match.group(1))] = match.group(2)
    return objects


def _pdf_stream(body: bytes) -> bytes:
    index = body.find(b"stream")
    if index < 0:
        return b""
    start = index + len(b"stream")
    while start < len(body) and body[start:start + 1] in (b"\r", b"\n"):
        start += 1
    end = body.rfind(b"endstream")
    if end < 0:
        end = len(body)
    raw = body[start:end]
    header = body[:index]
    if b"/FlateDecode" in header:
        for candidate in (raw, raw.strip(b"\r\n"), raw.rstrip()):
            try:
                return zlib.decompress(candidate)
            except zlib.error:
                continue
        return b""
    return raw


def _decode_pdf_string(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _unescape_pdf(raw: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index:index + 1]
        if char == b"\\" and index + 1 < len(raw):
            following = raw[index + 1:index + 2]
            mapping = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                       b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
            if following in mapping:
                out += mapping[following]
                index += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", raw[index + 1:index + 4])
            if octal:
                out.append(int(octal.group(0), 8) & 0xFF)
                index += 1 + len(octal.group(0))
                continue
            out += following
            index += 2
            continue
        out += char
        index += 1
    return _decode_pdf_string(bytes(out))


def _content_text(content: bytes) -> str:
    pieces: list[str] = []
    pattern = re.compile(rb"\((?:\\.|[^\\()])*\)|\[[^\]]*\]\s*TJ|T\*|ET", re.S)
    for match in pattern.finditer(content):
        token = match.group(0)
        if token.startswith(b"("):
            pieces.append(_unescape_pdf(token[1:-1]))
        elif token.rstrip().endswith(b"TJ"):
            for inner in re.finditer(rb"\((?:\\.|[^\\()])*\)", token, re.S):
                pieces.append(_unescape_pdf(inner.group(0)[1:-1]))
        elif token == b"T*" or token == b"ET":
            pieces.append("\n")
    text = "".join(pieces)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def parse_pdf(data: bytes) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF 解析需要安装 requirements-agent.txt 中的 pypdf") from exc
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 已加密，请先提供可读取的副本")
    pages = reader.pages
    blocks: list[dict] = []
    warnings: list[str] = []
    empty_pages: list[int] = []
    for page_index, page in enumerate(pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            blocks.append({"block_type": "text", "page_no": page_index,
                           "paragraph_no": None, "table_no": None, "text": text})
        else:
            empty_pages.append(page_index)
    if empty_pages:
        warnings.append(
            "以下页面没有可提取的文本层，可能是扫描页：" +
            ", ".join(str(number) for number in empty_pages[:20]) +
            "；需要 OCR 或按个人模型能力另行处理"
        )
    tables: list[dict] = []
    return {
        "format": "pdf",
        "page_count": len(pages),
        "blocks": blocks,
        "tables": tables,
        "warnings": warnings,
        "empty_pages": empty_pages,
    }


# ----------------------------------------------------------------------- DOCX


def _paragraph_text(node) -> str:
    return "".join(element.text or "" for element in node.iter(f"{WORD}t")).strip()


def parse_docx(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise ValueError("DOCX 缺少 word/document.xml")
        payload = archive.read("word/document.xml")
    root = ElementTree.fromstring(payload)
    body = root.find(f"{WORD}body")
    blocks: list[dict] = []
    tables: list[dict] = []
    paragraph_no = 0
    table_no = 0
    if body is None:
        return {"format": "docx", "page_count": 0, "blocks": [], "tables": [],
                "warnings": ["文档正文为空"]}
    for child in body:
        if child.tag == f"{WORD}p":
            paragraph_no += 1
            text = _paragraph_text(child)
            style = child.find(f"{WORD}pPr/{WORD}pStyle")
            style_name = style.get(f"{WORD}val") if style is not None else ""
            heading = bool(re.match(r"(Heading|标题)\s*\d", style_name or "", re.I))
            if not text:
                continue
            blocks.append({
                "block_type": "heading" if heading else "text",
                "page_no": None, "paragraph_no": paragraph_no,
                "table_no": None, "text": text,
            })
        elif child.tag == f"{WORD}tbl":
            table_no += 1
            rows: list[list[str]] = []
            for row in child.findall(f"{WORD}tr"):
                cells = [_paragraph_text(cell) for cell in row.findall(f"{WORD}tc")]
                rows.append(cells)
            text = "\n".join(" | ".join(cells) for cells in rows if any(cells))
            if not text.strip():
                continue
            blocks.append({
                "block_type": "table", "page_no": None,
                "paragraph_no": None, "table_no": table_no, "text": text,
            })
            tables.append({"table_no": table_no, "rows": len(rows),
                           "columns": max((len(cells) for cells in rows), default=0)})
    warnings = ["Word 文档没有固定页码，引用按段落与表格序号定位"]
    return {"format": "docx", "page_count": 0, "blocks": blocks, "tables": tables,
            "warnings": warnings}


# ---------------------------------------------------------------- persistence


def parse_document(database_path: str | Path, asset: dict, local_path: str,
                   title: str | None, owner_id: int | None) -> dict:
    path = Path(local_path)
    description = describe_source(asset)
    if not description["supported"]:
        raise ValueError(description["reason"])
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    project_id = asset.get("project_id")
    with _connect(database_path) as conn:
        _ensure_tables(conn)
        existing = conn.execute(
            "SELECT id FROM documents WHERE project_id IS ? AND sha256=?",
            (project_id, digest),
        ).fetchone()
        if existing:
            return get_document(database_path, existing["id"])

    warnings: list[str] = []
    try:
        if description["format"] == "pdf":
            parsed = parse_pdf(data)
        else:
            parsed = parse_docx(path)
    except Exception as exc:
        return _persist_failure(database_path, asset, title, owner_id, description,
                                digest, str(exc))

    blocks = parsed["blocks"]
    if not blocks:
        return _persist_failure(
            database_path, asset, title, owner_id, description, digest,
            "没有解析出任何文本内容（可能是扫描件或空文档）",
            warnings=parsed.get("warnings") or [],
        )
    warnings.extend(parsed.get("warnings") or [])
    status = "partial" if parsed.get("empty_pages") else "ready"
    if description["format"] == "docx":
        status = "ready"

    document_id = str(uuid.uuid4())
    timestamp = _now()
    with _connect(database_path) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO documents (id, asset_id, project_id, owner_id, title, "
            "original_name, format, status, page_count, block_count, sha256, error, "
            "warnings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                document_id, asset.get("id"), project_id, owner_id,
                title or asset.get("original_name") or "未命名文档",
                asset.get("original_name"), description["format"], status,
                parsed.get("page_count") or 0, len(blocks), digest,
                json.dumps(warnings, ensure_ascii=False), timestamp, timestamp,
            ),
        )
        conn.executemany(
            "INSERT INTO document_blocks (document_id, ordinal, block_type, page_no, "
            "paragraph_no, table_no, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (document_id, ordinal, block["block_type"], block.get("page_no"),
                 block.get("paragraph_no"), block.get("table_no"), block["text"])
                for ordinal, block in enumerate(blocks)
            ],
        )
    return {
        "document_id": document_id,
        "status": status,
        "page_count": parsed.get("page_count") or 0,
        "block_count": len(blocks),
        "warnings": warnings,
    }


def _persist_failure(database_path, asset, title, owner_id, description, digest,
                     message, warnings=None) -> dict:
    document_id = str(uuid.uuid4())
    timestamp = _now()
    with _connect(database_path) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO documents (id, asset_id, project_id, owner_id, title, "
            "original_name, format, status, page_count, block_count, sha256, error, "
            "warnings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', 0, 0, ?, ?, ?, ?, ?)",
            (
                document_id, asset.get("id"), asset.get("project_id"), owner_id,
                title or asset.get("original_name") or "未命名文档",
                asset.get("original_name"), description["format"], digest, message,
                json.dumps(warnings or [], ensure_ascii=False), timestamp, timestamp,
            ),
        )
    return {
        "document_id": document_id,
        "status": "failed",
        "page_count": 0,
        "block_count": 0,
        "warnings": list(warnings or []) + [message],
    }


def get_document(database_path: str | Path, document_id: str) -> dict:
    with _connect(database_path) as conn:
        _ensure_tables(conn)
        row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if row is None:
        raise FileNotFoundError("文档未解析或不存在")
    record = dict(row)
    record['document_id'] = record['id']
    record["warnings"] = json.loads(record.get("warnings_json") or "[]")
    return record


def read_document_blocks(database_path, document_id, offset=0, limit=20):
    """Ordered parsed source for preview/tools; caller must authorize the document."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('原文分页参数无效')
    with _connect(database_path) as conn:
        rows=conn.execute('SELECT ordinal,block_type,page_no,paragraph_no,table_no,text FROM document_blocks WHERE document_id=? ORDER BY ordinal LIMIT ? OFFSET ?', (document_id,limit,offset)).fetchall()
    return [{**dict(row),'text':row['text'][:4000]} for row in rows]


def list_documents(database_path, owner_id, project_id):
    with _connect(database_path) as conn:
        _ensure_tables(conn)
        ids = [r[0] for r in conn.execute("SELECT id FROM documents WHERE owner_id=? AND project_id=? ORDER BY created_at DESC", (owner_id, project_id))]
    return [get_document(database_path, item) for item in ids]


def search_document(database_path: str | Path, document_id: str, query: str,
                    limit: int = 20) -> dict:
    needle = query.strip().lower()
    if not needle:
        raise ValueError("检索词不能为空")
    document = get_document(database_path, document_id)
    if document["status"] == "failed":
        raise ValueError("文档解析失败，无法检索：" + (document.get("error") or ""))
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT ordinal, block_type, page_no, paragraph_no, table_no, text "
            "FROM document_blocks WHERE document_id=? ORDER BY ordinal",
            (document_id,),
        ).fetchall()
    hits: list[dict] = []
    truncated = False
    for row in rows:
        text = row["text"]
        position = text.lower().find(needle)
        if position < 0:
            continue
        if len(hits) >= limit:
            truncated = True
            break
        start = max(0, position - 80)
        excerpt = text[start:start + 240]
        hits.append({
            "document_id": document_id,
            "document_version": document["sha256"],
            "source_file_id": document["asset_id"],
            "block": row["ordinal"],
            "block_type": row["block_type"],
            "page_no": row["page_no"],
            "paragraph_no": row["paragraph_no"],
            "table_no": row["table_no"],
            "excerpt": excerpt,
            "citation": _citation(document, row),
        })
    return {"hits": hits, "truncated": truncated}


def _citation(document: dict, block) -> str:
    title = document.get("title") or document.get("original_name") or document["id"]
    if block["page_no"]:
        anchor = f"第 {block['page_no']} 页"
    elif block["table_no"]:
        anchor = f"表格 {block['table_no']}"
    elif block["paragraph_no"]:
        anchor = f"第 {block['paragraph_no']} 段"
    else:
        anchor = f"第 {block['ordinal']} 块"
    return f"{title} · {anchor}"
