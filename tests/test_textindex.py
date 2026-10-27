from pathlib import Path

from app import textindex


def test_format_time():
    assert textindex.format_time(0) == "0:00"
    assert textindex.format_time(75) == "1:15"
    assert textindex.format_time(3725) == "1:02:05"


def test_parse_vtt_dedupes_rolling_captions():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000 align:start
the quick brown

00:00:02.000 --> 00:00:04.000 align:start
the quick brown fox

00:00:04.000 --> 00:00:06.000 align:start
<c>jumps</c> over <00:00:05.000>the lazy dog
"""
    segments = textindex.parse_vtt(vtt)
    assert [s["text"] for s in segments] == ["the quick brown fox", "jumps over the lazy dog"]
    assert segments[0]["start"] == 0.0
    assert segments[1]["end"] == 6.0


def test_parse_json3():
    payload = (
        '{"events":[{"tStartMs":0,"dDurationMs":1500,"segs":[{"utf8":"hello "},{"utf8":"world"}]},'
        '{"tStartMs":1500,"segs":[{"utf8":"again"}]}]}'
    )
    segments = textindex.parse_json3(payload)
    assert segments[0]["text"] == "hello world"
    assert segments[1]["start"] == 1.5
    assert segments[1]["end"] > segments[1]["start"]


def test_parse_srv_xml():
    xml = '<transcript><text start="1.0" dur="2.0">Hello there</text></transcript>'
    segments = textindex.parse_xml(xml)
    assert segments == [{"start": 1.0, "end": 3.0, "text": "Hello there"}]


def test_choose_best_caption_prefers_language(tmp_path: Path):
    english = tmp_path / "vid.en.vtt"
    english.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8")
    spanish = tmp_path / "vid.es.vtt"
    spanish.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhola amigo\n", encoding="utf-8")
    segments, fmt, path = textindex.choose_best_caption([english, spanish], "es")
    assert segments[0]["text"] == "hola amigo"
    assert fmt == "vtt"
    assert path == spanish


def test_index_and_search(tmp_path: Path):
    db = tmp_path / "index.sqlite"
    textindex.build_index(
        db,
        [
            {"kind": "transcript", "start": 0, "end": 2, "text": "welcome to the product demo"},
            {"kind": "transcript", "start": 2, "end": 4, "text": "the price is twenty dollars"},
            {"kind": "ocr", "start": 4, "end": 5, "text": "SUBSCRIBE NOW", "meta": {"shot": 2}},
            {"kind": "chapter", "start": 0, "end": 6, "text": "Introduction and pricing"},
        ],
    )
    results = textindex.search_index(db, "price", limit=5)
    assert results
    pricing = [r for r in results if "dollars" in r["text"]]
    assert pricing
    assert pricing[0]["time"] == "0:02"

    ocr = textindex.search_index(db, "subscribe", kind="ocr")
    assert len(ocr) == 1
    assert ocr[0]["meta"]["shot"] == 2

    textindex.add_docs(
        db,
        [{"kind": "ocr", "start": 6, "end": 7, "text": "limited time offer"}],
    )
    extra = textindex.search_index(db, "limited")
    assert extra and extra[0]["text"] == "limited time offer"


def test_segments_in_range_truncates():
    segments = [{"start": 0, "end": 1, "text": "alpha beta gamma delta"}]
    text = textindex.segments_in_range(segments, 0, 2, max_chars=12)
    assert text.endswith("…")
    assert textindex.segments_in_range(segments, 5, 6) == ""
