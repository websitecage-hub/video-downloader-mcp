from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_TIME_RE = re.compile(r"<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_CUE_TIME_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})"
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_SRV_TEXT_RE = re.compile(
    r"<text[^>]*\bstart=\"(?P<start>[0-9.]+)\"[^>]*\bdur=\"(?P<dur>[0-9.]+)\"[^>]*>(?P<text>.*?)</text>",
    re.DOTALL | re.IGNORECASE,
)
_TTML_P_RE = re.compile(
    r"<p[^>]*\bbegin=\"(?P<start>[^\"]+)\"[^>]*\bend=\"(?P<end>[^\"]+)\"[^>]*>(?P<text>.*?)</p>",
    re.DOTALL | re.IGNORECASE,
)


def format_time(seconds: float | int | None) -> str:
    try:
        value = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        value = 0
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] if numbers else 0.0


def clean_text(value: str) -> str:
    value = _INLINE_TIME_RE.sub("", value or "")
    value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = _SPACE_RE.sub(" ", value)
    return value.strip()


def _dedupe(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rolling auto-caption duplicates and zero-length cues."""
    cleaned: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda item: (item["start"], item["end"])):
        text = clean_text(str(segment.get("text") or ""))
        if not text:
            continue
        start = max(0.0, float(segment.get("start") or 0.0))
        end = max(start, float(segment.get("end") or start))
        if cleaned:
            previous = cleaned[-1]
            if text == previous["text"]:
                previous["end"] = max(previous["end"], end)
                continue
            if text.startswith(previous["text"]) and start - previous["start"] < 2.5:
                previous["text"] = text
                previous["end"] = max(previous["end"], end)
                continue
            if previous["text"].endswith(text) and start - previous["end"] < 1.0:
                previous["end"] = max(previous["end"], end)
                continue
        cleaned.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return cleaned


def parse_vtt(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = _CUE_TIME_RE.search(line)
        if not match:
            index += 1
            continue
        start = _timestamp(match.group("start"))
        end = _timestamp(match.group("end"))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        segment_text = clean_text(" ".join(body))
        if segment_text:
            segments.append({"start": start, "end": end, "text": segment_text})
    return _dedupe(segments)


def parse_srt(text: str) -> list[dict[str, Any]]:
    return parse_vtt(text)


def parse_json3(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    raw: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        segments = event.get("segs") or []
        parts = []
        for segment in segments:
            if isinstance(segment, dict):
                parts.append(str(segment.get("utf8") or ""))
        body = clean_text("".join(parts))
        if not body:
            continue
        try:
            start = float(event.get("tStartMs") or 0) / 1000.0
        except (TypeError, ValueError):
            start = 0.0
        try:
            duration = float(event.get("dDurationMs") or 0) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            next_start = None
            for following in events[index + 1 :]:
                if isinstance(following, dict) and following.get("tStartMs") is not None:
                    try:
                        next_start = float(following["tStartMs"]) / 1000.0
                    except (TypeError, ValueError):
                        next_start = None
                    break
            duration = max(0.4, (next_start - start) if next_start and next_start > start else 1.2)
        raw.append({"start": start, "end": start + duration, "text": body})
    return _dedupe(raw)


def parse_xml(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for match in _SRV_TEXT_RE.finditer(text):
        start = float(match.group("start"))
        duration = float(match.group("dur"))
        body = clean_text(match.group("text"))
        if body:
            segments.append({"start": start, "end": start + duration, "text": body})
    if not segments:
        for match in _TTML_P_RE.finditer(text):
            start = _timestamp(match.group("start"))
            end = _timestamp(match.group("end"))
            body = clean_text(match.group("text"))
            if body:
                segments.append({"start": start, "end": end, "text": body})
    return _dedupe(segments)


def parse_caption_file(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], "unreadable"
    suffix = path.suffix.lower()
    if suffix == ".json3" or text.lstrip().startswith("{"):
        segments = parse_json3(text)
        if segments:
            return segments, "json3"
    if suffix in {".xml", ".ttml", ".srv1", ".srv2", ".srv3"} or "<text" in text[:2000] or "<tt" in text[:2000]:
        segments = parse_xml(text)
        if segments:
            return segments, "xml"
    segments = parse_vtt(text)
    if segments:
        return segments, "vtt"
    segments = parse_srt(text)
    if segments:
        return segments, "srt"
    return [], suffix.lstrip(".") or "unknown"


def choose_best_caption(
    files: Iterable[Path], preferred_language: str | None = None
) -> tuple[list[dict[str, Any]], str, Path | None]:
    best: tuple[float, list[dict[str, Any]], str, Path | None] = (0.0, [], "none", None)
    preferred = (preferred_language or "").split("-")[0].lower()
    for path in files:
        segments, fmt = parse_caption_file(path)
        if not segments:
            continue
        text_length = sum(len(segment["text"]) for segment in segments)
        language = ""
        parts = path.name.split(".")
        if len(parts) >= 3:
            language = parts[-2].split("-")[0].lower()
        score = text_length + min(2000, len(segments) * 10)
        if preferred and language == preferred:
            score += 5000
        if language == "en":
            score += 1500
        if score > best[0]:
            best = (score, segments, fmt, path)
    return best[1], best[2], best[3]


def segments_in_range(
    segments: list[dict[str, Any]], start: float, end: float, max_chars: int = 1600
) -> str:
    selected = [
        segment["text"]
        for segment in segments
        if segment["end"] >= start and segment["start"] <= end
    ]
    text = " ".join(selected)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return text


def transcript_text(segments: list[dict[str, Any]], limit: int = 1200) -> str:
    lines = [f"[{format_time(segment['start'])}] {segment['text']}" for segment in segments[:limit]]
    return "\n".join(lines)


def _fts_query(query: str) -> str:
    tokens = _WORD_RE.findall(query or "")
    if not tokens:
        return ""
    escaped = []
    for token in tokens[:12]:
        token = token.replace('"', '""')
        if len(token) >= 3 and re.match(r"^[A-Za-z0-9_]+$", token):
            escaped.append(f'"{token}"*')
        else:
            escaped.append(f'"{token}"')
    return " OR ".join(escaped)


def build_index(db_path: Path, docs: Iterable[dict[str, Any]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS docs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, start REAL, end REAL, "
            "text TEXT, meta TEXT)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
            "text, content='docs', content_rowid='id', tokenize='porter unicode61')"
        )
        for doc in docs:
            text = str(doc.get("text") or "").strip()
            if not text:
                continue
            cursor = connection.execute(
                "INSERT INTO docs (kind, start, end, text, meta) VALUES (?, ?, ?, ?, ?)",
                (
                    str(doc.get("kind") or "text"),
                    float(doc.get("start") or 0.0),
                    float(doc.get("end") or 0.0),
                    text,
                    json.dumps(doc.get("meta") or {}, ensure_ascii=False),
                ),
            )
            connection.execute(
                "INSERT INTO docs_fts (rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, text),
            )
        connection.commit()
    finally:
        connection.close()


def add_docs(db_path: Path, docs: Iterable[dict[str, Any]]) -> None:
    if not db_path.is_file():
        build_index(db_path, docs)
        return
    connection = sqlite3.connect(db_path)
    try:
        for doc in docs:
            text = str(doc.get("text") or "").strip()
            if not text:
                continue
            cursor = connection.execute(
                "INSERT INTO docs (kind, start, end, text, meta) VALUES (?, ?, ?, ?, ?)",
                (
                    str(doc.get("kind") or "text"),
                    float(doc.get("start") or 0.0),
                    float(doc.get("end") or 0.0),
                    text,
                    json.dumps(doc.get("meta") or {}, ensure_ascii=False),
                ),
            )
            connection.execute(
                "INSERT INTO docs_fts (rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, text),
            )
        connection.commit()
    finally:
        connection.close()


def _snippet(text: str, query: str, width: int = 220) -> str:
    tokens = [token for token in _WORD_RE.findall(query or "") if len(token) > 1]
    low = text.lower()
    position = -1
    for token in tokens:
        position = low.find(token.lower())
        if position >= 0:
            break
    if position < 0:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, position - width // 3)
    end = min(len(text), start + width)
    snippet = text[start:end]
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def search_index(
    db_path: Path,
    query: str,
    *,
    limit: int = 8,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT d.id, d.kind, d.start, d.end, d.text, d.meta, bm25(docs_fts) AS score "
            "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
            "WHERE docs_fts MATCH ?"
        )
        params: list[Any] = [fts_query]
        if kind:
            sql += " AND d.kind = ?"
            params.append(kind)
        sql += " ORDER BY score LIMIT ?"
        params.append(max(1, limit))
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    results = []
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        results.append(
            {
                "kind": row["kind"],
                "start": round(float(row["start"] or 0.0), 2),
                "end": round(float(row["end"] or 0.0), 2),
                "time": format_time(row["start"]),
                "text": row["text"],
                "snippet": _snippet(row["text"], query),
                "score": round(float(row["score"] or 0.0), 4),
                "meta": meta,
            }
        )
    return results
