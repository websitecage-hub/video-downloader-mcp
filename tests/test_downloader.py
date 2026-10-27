from app.downloader import _FORMATS, VideoDownloader, normalize_quality


def test_normalize_quality():
    assert normalize_quality(None) == "720p"
    assert normalize_quality("best") == "1080p"
    assert normalize_quality("1080p") == "1080p"
    assert normalize_quality("720") == "720p"
    assert normalize_quality("480p") == "480p"
    assert normalize_quality("240p") == "360p"
    assert normalize_quality("4k") == "1080p"
    assert normalize_quality("nonsense") == "720p"


def test_format_selector_handles_portrait_video():
    assert "width<=720" in _FORMATS["720p"]
    assert "height<=720" in _FORMATS["720p"]


def test_first_video_from_playlist():
    playlist = {"_type": "playlist", "entries": [{"id": "one"}, {"id": "two"}]}
    assert VideoDownloader._first_video(playlist) == {"id": "one"}
    assert VideoDownloader._first_video({"id": "single"}) == {"id": "single"}


def test_friendly_error_bot_block():
    message = VideoDownloader._friendly_error(
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot."
    )
    assert "blocking downloads" in message


def test_friendly_error_private():
    message = VideoDownloader._friendly_error("ERROR: [youtube] abc: Private video. Sign in")
    assert "private" in message.lower()


def test_friendly_error_passthrough():
    assert VideoDownloader._friendly_error("ERROR: Something odd happened") == "Something odd happened"
