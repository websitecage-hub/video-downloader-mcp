from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from urllib.parse import urlsplit

from .config import ALLOW_PRIVATE_URLS

MAX_URL_LENGTH = 2048

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}

_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")

_MEDIA_EXTENSIONS = {
    "mp4",
    "m4v",
    "mov",
    "webm",
    "mkv",
    "avi",
    "flv",
    "wmv",
    "mp3",
    "m4a",
    "aac",
    "opus",
    "ogg",
    "wav",
}


class UnsafeURLError(ValueError):
    """Raised when a URL could reach a private/internal network address."""


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Validate a user-supplied URL and block SSRF targets.

    DNS is resolved here, so hostnames that point at private ranges are rejected.
    Returns the stripped URL on success and raises UnsafeURLError otherwise.
    """
    if not isinstance(url, str):
        raise UnsafeURLError("URL must be a string.")
    url = url.strip()
    if not url:
        raise UnsafeURLError("URL is empty.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"URL is too long (max {MAX_URL_LENGTH} characters).")

    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise UnsafeURLError("Invalid URL.") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("Only http:// and https:// URLs are supported.")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs containing credentials are not allowed.")
    if not host:
        raise UnsafeURLError("URL has no hostname.")

    hostname = host.rstrip(".").lower()
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURLError("That hostname is not allowed.")

    if ALLOW_PRIVATE_URLS:
        return url

    port = port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UnsafeURLError(f"Could not resolve hostname: {hostname}") from None

    if not results:
        raise UnsafeURLError(f"Could not resolve hostname: {hostname}")

    for result in results:
        address = result[4][0]
        if not _is_public_ip(address):
            raise UnsafeURLError(
                "That URL points to a private or internal address, which is not allowed."
            )

    return url


def safe_filename(title: str | None, extension: str, fallback: str = "video") -> str:
    """Build a clean, ASCII-only download filename."""
    ext = re.sub(r"[^a-zA-Z0-9]", "", extension or "").lower() or "mp4"
    if ext not in _MEDIA_EXTENSIONS:
        ext = "mp4"

    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9 ._()\[\]-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    text = text[:90].strip(" ._-") or fallback
    return f"{text}.{ext}"
