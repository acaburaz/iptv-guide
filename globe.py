"""Globe TV guide files, used when a channel has no programme of its own.

The country list is the gzip files published at
https://github.com/globetvapp/epg
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACK = json.loads((ROOT / "globe_epg.json").read_text(encoding="utf-8"))
BASE = str(PACK["base"])
COUNTRIES: dict[str, list[str]] = PACK["countries"]

# Short tokens that show up in playlist names and mean a country folder.
_ALIAS = {
    "usa": "Usa",
    "us": "Usa",
    "sweden": "Sweden",
    "sverige": "Sweden",
    "se": "Sweden",
    "uk": "Unitedkingdom",
    "gb": "Unitedkingdom",
    "britain": "Unitedkingdom",
    "de": "Germany",
    "deutschland": "Germany",
    "fr": "France",
    "es": "Spain",
    "espana": "Spain",
    "italia": "Italy",
    "pt": "Portugal",
    "nl": "Netherlands",
    "norge": "Norway",
    "norway": "Norway",
    "dk": "Denmark",
    "denmark": "Denmark",
    "finland": "Finland",
    "poland": "Poland",
    "br": "Brazil",
    "brasil": "Brazil",
    "ca": "Canada",
    "au": "Australia",
    "tr": "Turkey",
    "turkiye": "Turkey",
    "greece": "Greece",
    "ireland": "Ireland",
    "belgium": "Belgium",
    "ch": "Switzerland",
    "austria": "Austria",
    "mx": "Mexico",
    "jp": "Japan",
    "kr": "Korea",
    "korea": "Korea",
    "indian": "India",
    "india": "India",
    "za": "Southafrica",
}

_STOP = {
    "hd",
    "sd",
    "fhd",
    "uhd",
    "4k",
    "hevc",
    "tv",
    "the",
    "and",
    "channel",
    "live",
    "plus",
    "vip",
    "ppv",
    "only",
    "event",
    "events",
    "sport",
    "sports",
    "news",
    "movie",
    "movies",
    "cinema",
}
_SUFFIX = re.compile(r"\.[a-z]{2}$", re.IGNORECASE)
_DT = re.compile(r"^([a-z]{3,5})dt\d*$")
_CALL = re.compile(r"\(([A-Za-z]{3,5})\)")
_LEAD = re.compile(r"^[A-Za-z]{2}\s*\|\s*")
_QUALITY = re.compile(r"\b(?:HD|FHD|SD|UHD|4K|HEVC)\b", re.IGNORECASE)


def globe_keys(label: str) -> list[str]:
    """Index keys for one Globe channel id or display name."""
    text = _SUFFIX.sub("", (label or "").strip())
    phrase = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not phrase:
        return []
    keys = [phrase]
    compact = phrase.replace(" ", "")
    if compact != phrase:
        keys.append(compact)
    digital = _DT.match(compact)
    if digital:
        keys.append(digital.group(1))
    return [key for key in dict.fromkeys(keys) if len(key) >= 3 and key not in _STOP]


def channel_keys(name: str, tvg_name: str = "", tvg_id: str = "") -> list[str]:
    """Keys a playlist channel can use to find a Globe programme."""
    texts = []
    for value in (tvg_id, tvg_name, name):
        if not value:
            continue
        texts.append(value)
        texts.extend(_CALL.findall(value))
        cleaned = _LEAD.sub("", value)
        cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
        cleaned = _QUALITY.sub(" ", cleaned)
        texts.append(cleaned)
    keys = []
    for text in texts:
        keys.extend(globe_keys(text))
    return list(dict.fromkeys(keys))


def guess_country(*parts: str) -> str | None:
    """Pick a Globe folder from a category or channel name."""
    blob = " ".join(part for part in parts if part).lower()
    if not blob:
        return None
    aliases = sorted(_ALIAS.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, folder in aliases:
        if folder not in COUNTRIES:
            continue
        if re.search(rf"(^|[^a-z]){re.escape(alias)}([^a-z]|$)", blob):
            return folder
    for folder in COUNTRIES:
        word = folder.lower()
        if len(word) < 4:
            continue
        if re.search(rf"(^|[^a-z]){re.escape(word)}([^a-z]|$)", blob):
            return folder
    return None


def download_country(folder: str, dest_dir: Path) -> list[Path]:
    """Save one country's gzip guides. Missing files are skipped."""
    names = COUNTRIES.get(folder) or []
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for name in names:
        if not name.endswith(".xml.gz") or "/" in name or "\\" in name:
            continue
        target = dest_dir / name
        url = BASE + urllib.parse.quote(folder) + "/" + urllib.parse.quote(name)
        request = urllib.request.Request(url, headers={"User-Agent": "IPTVGuide"})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        if payload[:2] != b"\x1f\x8b":
            continue
        target.write_bytes(payload)
        saved.append(target)
    return saved
