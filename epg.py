"""Stream an XMLTV file into NOW/NEXT rows.

The guide never keeps every programme. Each channel keeps the programme on
now and the earliest one after that, which is what the VLC list can show.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_when(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if len(text) < 14 or not text[:14].isdigit():
        return None
    year = int(text[0:4])
    month = int(text[4:6])
    day = int(text[6:8])
    hour = int(text[8:10])
    minute = int(text[10:12])
    second = int(text[12:14])
    try:
        clock = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    rest = text[14:].strip()
    if len(rest) >= 5 and rest[0] in "+-" and rest[1:5].isdigit():
        sign = 1 if rest[0] == "+" else -1
        offset = timedelta(hours=int(rest[1:3]), minutes=int(rest[3:5])) * sign
        clock = clock.replace(tzinfo=timezone(offset))
    else:
        clock = clock.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return int(clock.timestamp())


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _open_xml(path: Path):
    if _is_gzip(path):
        return gzip.open(path, "rb")
    return path.open("rb")


def _drop(root: ET.Element | None, elem: ET.Element) -> None:
    if root is not None and elem is not root:
        root.remove(elem)
    elem.clear()


def load_guide(path: Path, on_progress=None) -> tuple[dict[str, dict], int, int]:
    """Return tvg-id -> now/next, and the number of programme tags seen."""
    now = time.time()
    slots: dict[str, dict] = {}
    names: dict[str, list[str]] = {}
    seen = 0
    handle = _open_xml(path)
    try:
        root = None
        for event, elem in ET.iterparse(handle, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                continue
            tag = _local_name(elem.tag)
            if tag == "channel":
                channel_id = (elem.get("id") or "").strip()
                if channel_id:
                    labels = []
                    for child in elem:
                        if _local_name(child.tag) == "display-name":
                            label = (child.text or "").strip()
                            if label and label not in labels:
                                labels.append(label)
                    names[channel_id] = labels[:4]
                _drop(root, elem)
            elif tag == "programme":
                seen += 1
                channel_id = (elem.get("channel") or "").strip()
                start = _parse_when(elem.get("start"))
                stop = _parse_when(elem.get("stop"))
                title = ""
                desc = ""
                for child in elem:
                    child_tag = _local_name(child.tag)
                    if child_tag == "title" and not title:
                        title = (child.text or "").strip()
                    elif child_tag == "desc" and not desc:
                        desc = " ".join((child.text or "").split())[:400]
                if channel_id and start and stop:
                    slot = slots.get(channel_id)
                    if slot is None:
                        slot = {"now": None, "next": None}
                        slots[channel_id] = slot
                    item = {"start": start, "stop": stop, "title": title[:180], "desc": desc}
                    if start <= now < stop:
                        current = slot["now"]
                        if current is None or start >= current["start"]:
                            slot["now"] = item
                    elif start >= now:
                        upcoming = slot["next"]
                        if upcoming is None or start < upcoming["start"]:
                            slot["next"] = item
                _drop(root, elem)
                if on_progress and seen % 20000 == 0:
                    on_progress(seen, "read")
    finally:
        handle.close()

    rows: dict[str, dict] = {}
    channels = 0
    for channel_id, slot in slots.items():
        if not slot["now"] and not slot["next"]:
            continue
        channels += 1
        rows[channel_id] = slot
        for label in names.get(channel_id, []):
            rows.setdefault(label, slot)
    if on_progress:
        on_progress(seen, "save")
    return rows, seen, channels


def guide_fields(row) -> dict:
    """Format one epg_now row for the list and the details line."""
    if row is None:
        return {"now": "", "next": "", "left": "", "desc": ""}

    def clock(start: int, stop: int, title: str) -> str:
        if not start and not title:
            return ""
        parts = []
        if start:
            parts.append(datetime.fromtimestamp(int(start)).strftime("%H:%M"))
        if stop:
            parts.append(datetime.fromtimestamp(int(stop)).strftime("%H:%M"))
        when = "-".join(parts)
        title = (title or "").strip()
        return f"{when} {title}".strip()

    now_stop = int(row["now_stop"] or 0)
    left = ""
    if now_stop:
        left = f"{max(0, int((now_stop - time.time()) // 60))}m"
    return {
        "now": clock(int(row["now_start"] or 0), now_stop, row["now_title"] or ""),
        "next": clock(int(row["next_start"] or 0), int(row["next_stop"] or 0), row["next_title"] or ""),
        "left": left,
        "desc": row["now_desc"] or "",
    }


_XTREAM = re.compile(
    r"^https?://([^/?#]+)/(?:(?:live|movie|series)/)?[^/?#]+/[^/?#]+/(\d+)(?:[./?#].*)?$",
    re.IGNORECASE,
)
_PORTAL = re.compile(
    r"^(https?)://([^/?#]+)/(?:(?:live|movie|series)/)?([^/?#]+)/([^/?#]+)/(\d+)(?:[./?#].*)?$",
    re.IGNORECASE,
)


def stream_key(url: str) -> str | None:
    """Host plus stream id. The account secret stays out of the guide cache."""
    match = _XTREAM.match((url or "").strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _guide_text(value) -> str:
    raw = str(value or "").strip()
    if len(raw) < 8 or len(raw) % 4 != 0 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", raw):
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw
    if not decoded:
        return raw
    readable = sum(char.isprintable() or char in "\n\r\t" for char in decoded)
    if readable < len(decoded) * 0.9:
        return raw
    return " ".join(decoded.split())


def _unix(stamp, fallback: str | None) -> int | None:
    text = str(stamp or "").strip()
    if text.isdigit():
        return int(text)
    if fallback and len(fallback) >= 19 and fallback[4] == "-" and fallback[13] == ":":
        try:
            clock = datetime.strptime(fallback[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        clock = clock.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(clock.timestamp())
    return None


def programmes_from_listings(listings, now: float | None = None) -> dict:
    """Pick the programme on now and the earliest one after it."""
    moment = time.time() if now is None else now
    current = None
    upcoming = None
    for item in listings:
        if not isinstance(item, dict):
            continue
        start = _unix(item.get("start_timestamp"), item.get("start"))
        stop = _unix(item.get("stop_timestamp"), item.get("end"))
        if not start or not stop:
            continue
        slot = {
            "start": start,
            "stop": stop,
            "title": _guide_text(item.get("title"))[:180],
            "desc": _guide_text(item.get("description"))[:400],
        }
        if start <= moment < stop:
            if current is None or start >= current["start"]:
                current = slot
        elif start >= moment:
            if upcoming is None or start < upcoming["start"]:
                upcoming = slot
    current = current or {}
    upcoming = upcoming or {}
    return {
        "now_title": current.get("title") or "",
        "now_start": int(current.get("start") or 0),
        "now_stop": int(current.get("stop") or 0),
        "now_desc": current.get("desc") or "",
        "next_title": upcoming.get("title") or "",
        "next_start": int(upcoming.get("start") or 0),
        "next_stop": int(upcoming.get("stop") or 0),
        "fetched_at": int(moment),
    }


def fetch_short_epg(url: str, timeout: float = 6) -> dict | None:
    """NOW/NEXT from the portal named in a playlist URL. None means try again later."""
    match = _PORTAL.match((url or "").strip())
    if not match:
        return None
    scheme, host, user, password, stream = match.groups()
    query = urllib.parse.urlencode(
        {
            "username": user,
            "password": password,
            "action": "get_short_epg",
            "stream_id": stream,
            "limit": "4",
        }
    )
    request = urllib.request.Request(
        f"{scheme}://{host}/player_api.php?{query}",
        headers={"User-Agent": "IPTVGuide"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(200_000)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    listings = payload.get("epg_listings") if isinstance(payload, dict) else None
    if not isinstance(listings, list):
        return None
    return programmes_from_listings(listings)
