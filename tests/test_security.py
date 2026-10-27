import pytest

from app.security import UnsafeURLError, safe_filename, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://www.tiktok.com/@scout2015/video/6718335390845095173",
        "http://example.com/video.mp4",
    ],
)
def test_accepts_public_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://example.com/file.mp4",
        "file:///etc/passwd",
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://localhost.localdomain/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://user:pass@example.com/video.mp4",
        "http://metadata.google.internal/",
        "http://printer.local/",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_rejects_overlong_url():
    with pytest.raises(UnsafeURLError):
        validate_url("https://example.com/" + "a" * 3000)


def test_safe_filename_strips_unsafe_characters():
    name = safe_filename('../../My <Video>: "Best" | 2026?', "mp4")
    assert name == "My Video Best 2026.mp4"
    assert "/" not in name and "\\" not in name
    assert name.endswith(".mp4")
    assert "<" not in name and ">" not in name and '"' not in name


def test_safe_filename_handles_unicode_and_empty():
    assert safe_filename("Café — résumé 🎬", "MP4").endswith(".mp4")
    assert safe_filename("", "mp4") == "video.mp4"
    assert safe_filename(None, "exe") == "video.mp4"
