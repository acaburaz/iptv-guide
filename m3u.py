"""Stream an IPTV M3U into channel records.

HLS media playlists (segment lists) are one playable item, not thousands of
fake channels. IPTV lists are many #EXTINF entries, each with its own URL.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

_ATTR = re.compile(
    r"""([A-Za-z0-9_-]+)="([^"]*)"|([A-Za-z0-9_-]+)='([^']*)'|([A-Za-z0-9_-]+)=([^\s"']+)"""
)
_ALLOWED_OPTS = {"http-user-agent", "http-referrer", "http-reconnect"}
_PIPE_OPTS = {
    "user-agent": "http-user-agent",
    "http-user-agent": "http-user-agent",
    "referer": "http-referrer",
    "referrer": "http-referrer",
    "http-referer": "http-referrer",
    "http-referrer": "http-referrer",
}
_LINE_LIMIT = 1024 * 1024


def is_hls_file(path: Path) -> bool:
    """True when the file is an HLS document rather than a channel list."""
    with _open_binary(path) as handle:
        raw = handle.read(256 * 1024)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or _utf16_without_bom(raw):
        text = raw.decode("utf-16", "ignore")
    else:
        text = raw.decode("utf-8", "ignore")
    head = text.lower()
    # A stray HLS tag in an IPTV list must not collapse the whole file into one item.
    if "group-title" in head or "tvg-id" in head or "tvg-name" in head:
        return False
    if "#ext-x-targetduration" in head or "#ext-x-media-sequence" in head:
        return True
    if "#ext-x-stream-inf" in head:
        return True
    return False


def looks_like_playlist(path: Path) -> bool:
    """True for an M3U or a plain list of stream addresses. JSON catalogs are not playlists."""
    with _open_binary(path) as handle:
        raw = handle.read(65536)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")) or _utf16_without_bom(raw):
        raw = raw.decode("utf-16", "replace").encode("utf-8")
    head = raw.lstrip()
    if head.startswith((b"{", b"[")):
        return False
    text = raw.decode("utf-8", "replace")
    if "#EXTM3U" in text.upper() or "#EXTINF:" in text.upper():
        return True
    return any(line.strip().startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://")) for line in text.splitlines())


def iter_channels(path: Path, origin: str) -> Iterator[dict]:
    """Yield channel dicts. Relative URLs resolve against the playlist."""
    if is_hls_file(path):
        yield _single(path, origin)
        return

    pending_name = ""
    pending_attrs: dict[str, str] = {}
    pending_group = ""
    pending_opts: list[list[str]] = []

    for line in _iter_lines(path):
        line = line.strip()
        if not line:
            continue
        if _starts(line, "#EXTGRP:"):
            pending_group = _clean(line[8:], 200) or pending_group
            continue
        if _starts(line, "#EXTVLCOPT:"):
            opt = _option(line[11:])
            if opt:
                pending_opts.append(opt)
            continue
        if _starts(line, "#EXTINF:"):
            meta, name = _split_extinf(line[8:])
            pending_attrs = _parse_attrs(meta)
            pending_name = _clean(name, 300)
            continue
        if line.startswith("#"):
            continue
        address, pipe_opts = _split_pipe(line)
        if not _plausible_address(address):
            pending_name = ""
            pending_attrs = {}
            pending_opts = []
            continue

        url = _resolve_url(address, origin, path)
        if not url:
            pending_name = ""
            pending_attrs = {}
            pending_opts = []
            continue

        attrs = pending_attrs
        group = _clean(attrs.get("group-title", ""), 200) or pending_group or "Ungrouped"
        name = pending_name or _clean(attrs.get("tvg-name", ""), 300) or _name_from_url(url)
        yield {
            "name": name or "Untitled",
            "url": url,
            "group": group or "Ungrouped",
            "tvg_id": _clean(attrs.get("tvg-id", ""), 200),
            "tvg_name": _clean(attrs.get("tvg-name", ""), 300),
            "logo": _clean(attrs.get("tvg-logo", ""), 1000),
            "opts": list(pending_opts) + pipe_opts,
        }
        pending_name = ""
        pending_attrs = {}
        pending_opts = []


def _single(path: Path, origin: str) -> dict:
    if origin.startswith(("http://", "https://")):
        url = origin
    else:
        url = path.resolve().as_uri()
    name = path.stem or "Stream"
    return {
        "name": name,
        "url": url,
        "group": "Stream",
        "tvg_id": "",
        "tvg_name": "",
        "logo": "",
        "opts": [],
    }


def _open_binary(path: Path):
    """Open a playlist, including one the browser saved gzip-compressed."""
    with path.open("rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return path.open("rb")


def _utf16_without_bom(raw: bytes) -> bool:
    return len(raw) >= 4 and raw[1] == 0 and raw[3] == 0 and raw[0] != 0


def _iter_lines(path: Path) -> Iterator[str]:
    with _open_binary(path) as handle:
        start = handle.read(4)
        handle.seek(0)
        if start.startswith((b"\xff\xfe", b"\xfe\xff")) or _utf16_without_bom(start):
            text = handle.read().decode("utf-16", "replace").replace("\x00", "")
            yield from text.splitlines()
            return
        if start.startswith(b"\xef\xbb\xbf"):
            handle.read(3)
        while True:
            raw = _read_line(handle)
            if raw is None:
                break
            if raw == b"":
                continue
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError:
                yield raw.decode("cp1252", "replace")


def _read_line(handle) -> bytes | None:
    raw = handle.readline(_LINE_LIMIT)
    if not raw:
        return None
    if len(raw) >= _LINE_LIMIT and not raw.endswith(b"\n"):
        while True:
            more = handle.readline(_LINE_LIMIT)
            if not more or more.endswith(b"\n"):
                break
        return b""
    return raw


def _split_extinf(body: str) -> tuple[str, str]:
    """Split duration/attributes from the title at the first comma outside quotes."""
    quoted = False
    for index, char in enumerate(body):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return body[:index], body[index + 1 :].strip()
    return body.strip(), ""


def _parse_attrs(meta: str) -> dict[str, str]:
    if "=" not in meta:
        return {}
    found: dict[str, str] = {}
    for match in _ATTR.finditer(meta):
        key = next(part for part in (match.group(1), match.group(3), match.group(5)) if part)
        value = next(part for part in (match.group(2), match.group(4), match.group(6)) if part is not None)
        found[key.lower()] = value.strip()
    return found


def _split_pipe(line: str) -> tuple[str, list[list[str]]]:
    """Kodi-style `url|User-Agent=...&Referer=...` becomes a URL plus VLC options."""
    if "|" not in line:
        return line, []
    url, extra = line.split("|", 1)
    opts: list[list[str]] = []
    for part in re.split(r"[|&]", extra):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        mapped = _PIPE_OPTS.get(key.strip().lower().replace("_", "-"))
        if not mapped:
            continue
        opt = _option(f"{mapped}={value.strip()}")
        if opt:
            opts.append(opt)
    return url, opts


def _option(text: str) -> list[str] | None:
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip().lower()
    value = value.strip().replace("\r", "").replace("\n", "")
    if key not in _ALLOWED_OPTS or not value or len(value) > 400:
        return None
    return [key, value]


def _starts(line: str, tag: str) -> bool:
    return line.upper().startswith(tag)


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _plausible_address(line: str) -> bool:
    text = _strip_quotes(line)
    if not text or text.startswith("#"):
        return False
    # Provider "plus" lists put {utc} and brace characters inside real stream URLs.
    if "://" in text:
        return True
    if any(char in text for char in "{}<>"):
        return False
    return not any(char.isspace() for char in text)


def _resolve_url(token: str, origin: str, playlist: Path) -> str:
    url = _strip_quotes(token.replace("\x00", ""))
    if not url or url.startswith("#") or len(url) > 8000:
        return ""
    if "://" in url:
        return url
    if origin.startswith(("http://", "https://")):
        return urljoin(origin, url)
    return (playlist.parent / url).resolve().as_uri()


def _name_from_url(url: str) -> str:
    leaf = url.rsplit("/", 1)[-1]
    leaf = leaf.split("?", 1)[0].split("#", 1)[0]
    return _clean(leaf, 120)


def _clean(value: str, limit: int) -> str:
    text = " ".join(value.replace("\x00", "").split())
    return text[:limit]
