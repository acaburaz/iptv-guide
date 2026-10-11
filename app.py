"""IPTV Guide — search a large M3U list and open one channel in VLC.

The browser only asks for a window of channels. VLC never sees the playlist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from catalog import Catalog, write_demo
from player import VlcPlayer

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PLAYLISTS = DATA / "playlists"
STATIC = ROOT / "static" / "index.html"
UPLOAD_LIMIT = 640 * 1024 * 1024
UPLOAD_MB = UPLOAD_LIMIT // (1024 * 1024)
EXT_VERSION = 1

catalog = Catalog(DATA / "catalog.db")
player = VlcPlayer()
PUBLIC_LISTS = json.loads((ROOT / "public_playlists.json").read_text(encoding="utf-8"))
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        path = urlparse(self.path).path
        if path.startswith(("/api/channels", "/api/player", "/api/jobs", "/api/ext/ping")):
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        try:
            self._get()
        except BrokenPipeError:
            return
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            self._post()
        except BrokenPipeError:
            return
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def _get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in {"/", "/index.html"}:
            self._file(STATIC, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/sources":
            self._json(200, {"sources": catalog.sources()})
            return
        if path == "/api/public":
            self._json(200, PUBLIC_LISTS)
            return
        if path == "/api/groups":
            self._json(200, catalog.groups(_need_int(query, "source")))
            return
        if path == "/api/channels":
            started = time.perf_counter()
            page = catalog.channels(
                _need_int(query, "source"),
                group=_first(query, "group"),
                query=_first(query, "q"),
                offset=_int(query, "offset", 0),
                limit=_int(query, "limit", 180),
                view=_first(query, "view") or "all",
            )
            page["ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._json(200, page)
            return
        if path == "/api/player":
            self._json(200, player.refresh() if _first(query, "fresh") == "1" else player.snapshot())
            return
        if path == "/api/ext/ping":
            self._json(200, {"ok": True, "version": EXT_VERSION})
            return
        if path == "/api/ext/public":
            url = _first(query, "url").strip()
            known = None
            for group in PUBLIC_LISTS.get("groups", []):
                for item in group.get("items", []):
                    if item.get("url") == url:
                        known = item
                        break
                if known:
                    break
            if not known:
                self._json(400, {"error": "That list is not in the built-in public menu"})
                return
            dest = PLAYLISTS / f"{uuid.uuid4().hex[:8]}.m3u"
            PLAYLISTS.mkdir(parents=True, exist_ok=True)
            try:
                _download(url, dest)
            except ValueError as exc:
                dest.unlink(missing_ok=True)
                self._json(400, {"error": str(exc)})
                return
            label = " ".join(str(known.get("name") or "Public").split())[:120]
            job = _start_import(dest, url, label)
            self._json(200, job)
            return
        if path == "/api/ext/guide":
            raw_ids = _first(query, "ids").replace(" ", "").split(",")
            ids = []
            for item in raw_ids:
                if item.isdigit():
                    ids.append(int(item))
            self._json(200, {"items": catalog.guide_for(ids)})
            return
        if path == "/api/ext/channel":
            channel = catalog.get_channel(_need_int(query, "id"))
            if not channel:
                self._json(404, {"error": "Channel not found"})
                return
            self._json(200, channel)
            return
        if path == "/api/ext/remember":
            channel = catalog.get_channel(_need_int(query, "id"))
            if not channel:
                self._json(404, {"error": "Channel not found"})
                return
            catalog.remember(channel)
            self._json(200, {"ok": True})
            return
        if path == "/api/ext/index":
            try:
                file_path = _local_playlist(_first(query, "path"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            label = file_path.stem[:120] or "Playlist"
            job = _start_import(file_path, f"path:{file_path}", label)
            self._json(200, job)
            return
        if path == "/api/profiles":
            self._json(200, {"profiles": catalog.profiles()})
            return
        if path == "/api/ext/epg":
            try:
                file_path = _epg_file(_first(query, "path"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            name = " ".join(_first(query, "name").split())[:120]
            m3u = _first(query, "m3u")[:2000]
            if name:
                catalog.save_profile(name, m3u, str(file_path))
            job = _start_epg(file_path)
            self._json(200, job)
            return
        if path == "/api/ext/profile":
            name = _first(query, "name")
            if not name.strip():
                self._json(400, {"error": "Profile name is missing"})
                return
            catalog.save_profile(name, _first(query, "m3u"), _first(query, "epg"))
            self._json(200, {"ok": True, "profiles": catalog.profiles()})
            return
        if path == "/api/ext/profile/delete":
            catalog.delete_profile(_first(query, "name"))
            self._json(200, {"ok": True, "profiles": catalog.profiles()})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            wait = _first(query, "wait") == "1"
            deadline = time.monotonic() + 1.0
            while True:
                payload = _job_payload(job_id)
                if payload is None:
                    self._json(404, {"error": "Unknown import"})
                    return
                if not wait or payload["state"] != "running" or time.monotonic() >= deadline:
                    self._json(200, payload)
                    return
                time.sleep(0.2)
        self._json(404, {"error": "Not found"})

    def _post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/import/file":
            name = Path(unquote(self.headers.get("X-Filename", "playlist.m3u"))).name
            name = name.replace("\x00", "") or "playlist.m3u"
            if name in {".", ".."}:
                name = "playlist.m3u"
            dest = PLAYLISTS / f"{uuid.uuid4().hex[:8]}-{name}"
            PLAYLISTS.mkdir(parents=True, exist_ok=True)
            _spool(self, dest, UPLOAD_LIMIT)
            job = _start_import(dest, f"name:{name}", Path(name).stem or name)
            self._json(200, job)
            return
        data = _json_body(self)
        if path == "/api/import/url":
            url = str(data.get("url") or "").strip()
            dest = PLAYLISTS / f"{uuid.uuid4().hex[:8]}.m3u"
            PLAYLISTS.mkdir(parents=True, exist_ok=True)
            try:
                _download(url, dest)
            except ValueError as exc:
                dest.unlink(missing_ok=True)
                self._json(400, {"error": str(exc)})
                return
            given = " ".join(str(data.get("name") or "").replace("\x00", "").split())
            label = given or Path(urlparse(url).path).stem or urlparse(url).netloc or "Playlist"
            job = _start_import(dest, url, label[:120])
            self._json(200, job)
            return
        if path == "/api/demo":
            dest = PLAYLISTS / "demo.m3u"
            write_demo(dest)
            job = _start_import(dest, "demo:iptv-rack", "Demo list")
            self._json(200, job)
            return
        if path == "/api/play":
            channel = catalog.get_channel(_as_int(data.get("id")))
            if not channel:
                self._json(404, {"error": "Channel not found"})
                return
            try:
                volume = data.get("volume")
                snapshot = player.play(channel, None if volume is None else int(volume))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._json(503, {"error": str(exc)})
                return
            catalog.remember(channel)
            self._json(200, snapshot)
            return
        if path == "/api/player":
            action = str(data.get("action") or "")
            try:
                value = None if data.get("value") is None else int(data.get("value"))
                snapshot = player.command(action, value)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._json(503, {"error": str(exc)})
                return
            self._json(200, snapshot)
            return
        if path == "/api/favorites":
            try:
                favorite = catalog.toggle_favorite(_as_int(data.get("id")))
            except KeyError:
                self._json(404, {"error": "Channel not found"})
                return
            self._json(200, {"favorite": favorite})
            return
        if path == "/api/sources/delete":
            catalog.delete_source(_as_int(data.get("id")))
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "Not found"})

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json(500, {"error": "Interface file is missing"})
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def _job_payload(job_id: str) -> dict | None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        return {
            "id": job["id"],
            "state": job["state"],
            "phase": job["phase"],
            "count": job["count"],
            "error": job["error"],
            "source_id": job["source_id"],
            "kind": job["kind"],
        }


def _start_import(path: Path, origin: str, name: str) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "state": "running",
        "phase": "read",
        "count": 0,
        "error": "",
        "source_id": None,
        "kind": "channels",
    }
    with jobs_lock:
        jobs[job["id"]] = job
        if len(jobs) > 40:
            for key, existing in list(jobs.items()):
                if existing["state"] in {"done", "error"}:
                    jobs.pop(key, None)
                if len(jobs) <= 30:
                    break

    def run() -> None:
        try:
            def progress(count: int, phase: str = "read") -> None:
                with jobs_lock:
                    job["count"] = count
                    job["phase"] = phase

            source_id = catalog.import_file(path, origin, name, progress)
            finished = next(
                (item["channel_count"] for item in catalog.sources() if item["id"] == source_id),
                job["count"],
            )
            with jobs_lock:
                job["source_id"] = source_id
                job["count"] = finished
                job["state"] = "done"
        except Exception as exc:
            traceback.print_exc()
            with jobs_lock:
                job["state"] = "error"
                job["error"] = str(exc)

    threading.Thread(target=run, daemon=True).start()
    return dict(job)


def _start_epg(path: Path) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "state": "running",
        "phase": "read",
        "count": 0,
        "error": "",
        "source_id": None,
        "kind": "epg",
    }
    with jobs_lock:
        jobs[job["id"]] = job

    def run() -> None:
        try:
            def progress(count: int, phase: str = "read") -> None:
                with jobs_lock:
                    job["count"] = count
                    job["phase"] = phase

            count = catalog.import_epg(path, progress)
            with jobs_lock:
                job["count"] = count
                job["state"] = "done"
        except Exception as exc:
            traceback.print_exc()
            with jobs_lock:
                job["state"] = "error"
                job["error"] = str(exc)

    threading.Thread(target=run, daemon=True).start()
    return dict(job)


def _epg_file(raw: str) -> Path:
    text = raw.strip().strip('"')
    if not text:
        raise ValueError("Choose the XMLTV guide file")
    if text.startswith(("http://", "https://")):
        dest = PLAYLISTS / f"{uuid.uuid4().hex[:8]}.xml"
        PLAYLISTS.mkdir(parents=True, exist_ok=True)
        _download(text, dest)
        return dest
    file_path = Path(text)
    try:
        if not file_path.is_file():
            raise ValueError("Guide file not found")
        return file_path.resolve()
    except OSError as exc:
        raise ValueError("Guide file not found") from exc


def _download(url: str, dest: Path) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError("Playlist URL must start with http:// or https://")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IPTVRack/1.0",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            final = response.geturl()
            if not str(final).startswith(("http://", "https://")):
                raise ValueError("Playlist URL redirected away from HTTP")
            total = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > UPLOAD_LIMIT:
                        raise ValueError(f"Playlist is larger than {UPLOAD_MB} MB")
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Playlist server returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not download playlist: {exc.reason}") from exc
    if dest.stat().st_size == 0:
        raise ValueError("Playlist download was empty")


def _spool(handler: BaseHTTPRequestHandler, dest: Path, limit: int) -> None:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        raise ValueError("Missing upload")
    if length > limit:
        raise ValueError(f"Playlist is larger than {UPLOAD_MB} MB")
    remaining = length
    with dest.open("wb") as handle:
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handle.write(chunk)
            remaining -= len(chunk)
    if remaining:
        dest.unlink(missing_ok=True)
        raise ValueError("Upload ended early")


def _local_playlist(raw: str) -> Path:
    text = raw.strip().strip('"')
    if not text:
        raise ValueError("Type the path to the M3U file")
    file_path = Path(text)
    try:
        if not file_path.is_file():
            raise ValueError("File not found")
        return file_path.resolve()
    except OSError as exc:
        raise ValueError("File not found") from exc


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    if length > 1_000_000:
        raise ValueError("Request is too large")
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _first(query: dict, key: str) -> str:
    values = query.get(key) or [""]
    return str(values[0])


def _int(query: dict, key: str, default: int) -> int:
    raw = _first(query, key)
    if raw == "":
        return default
    return int(raw)


def _need_int(query: dict, key: str) -> int:
    raw = _first(query, key)
    if raw == "":
        raise ValueError(f"Missing {key}")
    return int(raw)


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Missing id") from exc


def install_vlc_extension() -> Path | None:
    """Copy the search panel into VLC's per-user extension folder."""
    src = ROOT / "vlc" / "iptv-rack.lua"
    appdata = os.environ.get("APPDATA")
    if not appdata or not src.is_file():
        return None
    dest_dir = Path(appdata) / "vlc" / "lua" / "extensions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8").replace("__IPTV_RACK_DIR__", str(ROOT))
    dest = dest_dir / "iptv-rack.lua"
    if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
        dest.write_text(text, encoding="utf-8", newline="\n")
    return dest


def serve(port: int, open_browser: bool) -> None:
    httpd = None
    chosen = port
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            chosen = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"No free port from {port} to {port + 19}")
    httpd.daemon_threads = True
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "port.txt").write_text(str(chosen), encoding="ascii")
    url = f"http://127.0.0.1:{chosen}/"
    print(f"IPTV Guide  {url}")
    extension = install_vlc_extension()
    if extension:
        print(f"VLC plugin {extension}")
        print("In VLC: restart, then View -> IPTV Guide")
    if player.path:
        print(f"VLC        {player.path}")
    else:
        print("VLC        not found — install it from https://www.videolan.org/")
    print("Stop       Ctrl+C  (VLC keeps playing the current channel)")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


def selftest() -> None:
    import tempfile
    import time

    from m3u import iter_channels

    tmp = Path(tempfile.mkdtemp(prefix="iptv-rack-"))
    sample = tmp / "sample.m3u"
    sample.write_text(
        "\ufeff#EXTM3U\n"
        '#EXTINF:-1 tvg-id="w1" tvg-logo="http://img.example/a.png" group-title="News, World",Channel, HD\n'
        "#EXTVLCOPT:http-user-agent=IPTVRack\n"
        "#EXTVLCOPT:sout=#std{access=file}\n"
        "relative.ts\n"
        "#EXTGRP:Sports\n"
        "#EXTINF:-1,Match\n"
        "https://example.com/live\n",
        encoding="utf-8",
    )
    rows = list(iter_channels(sample, "name:sample.m3u"))
    assert rows[0]["name"] == "Channel, HD", rows[0]
    assert rows[0]["group"] == "News, World", rows[0]
    assert rows[0]["opts"] == [["http-user-agent", "IPTVRack"]], rows[0]
    assert rows[0]["url"].startswith("file:"), rows[0]
    assert rows[1]["group"] == "Sports", rows[1]

    hls = tmp / "movie.m3u8"
    hls.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
        "#EXTINF:10,\nseg0.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    hls_rows = list(iter_channels(hls, "https://cdn.example/movie.m3u8"))
    assert len(hls_rows) == 1 and hls_rows[0]["url"] == "https://cdn.example/movie.m3u8"

    big = tmp / "big.m3u"
    count = 20000
    with big.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        handle.write('#EXTINF:-1 tvg-id="w1" tvg-name="Español Uno" group-title="News",Español Uno\nhttp://example.invalid/es\n')
        handle.write('#EXTINF:-1 group-title="Clips",<img src=x onerror=alert(1)>\nhttp://example.invalid/x\n')
        for index in range(1, count + 1):
            handle.write(f'#EXTINF:-1 tvg-id="n{index}" group-title="Bulk",Item {index:05d}\nhttp://example.invalid/c/{index}\n')

    store = Catalog(tmp / "catalog.db")
    started = time.perf_counter()
    source_id = store.import_file(big, "name:big.m3u", "big")
    elapsed = time.perf_counter() - started
    info = store.groups(source_id)
    assert info["total"] == count + 2, info
    grouped = {item["name"]: item["count"] for item in info["groups"]}
    assert grouped.get("Bulk") == count, grouped
    assert elapsed < 20, elapsed

    started = time.perf_counter()
    found = store.channels(source_id, group="", query="Espanol", offset=0, limit=10, view="all")
    search_ms = (time.perf_counter() - started) * 1000
    assert found["total"] == 1 and found["items"][0]["name"] == "Español Uno", found
    assert search_ms < 500, search_ms

    page = store.channels(source_id, group="Bulk", query="", offset=18000, limit=50, view="all")
    assert page["total"] == count, page["total"]
    assert page["items"][0]["name"] == "Item 18001", page["items"][0]
    started = time.perf_counter()
    broad = store.channels(source_id, group="", query="Item", offset=0, limit=180, view="all")
    broad_ms = (time.perf_counter() - started) * 1000
    assert broad["total"] == count, broad["total"]
    assert broad_ms < 250, broad_ms
    deep = store.channels(source_id, group="", query="Item", offset=18000, limit=20, view="all")
    assert len(deep["items"]) == 20
    nasty = store.channels(source_id, group="Clips", query="", offset=0, limit=5, view="all")
    assert nasty["items"][0]["name"] == "<img src=x onerror=alert(1)>"

    guide = tmp / "guide.xml"
    guide.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="w1"><display-name>Español Uno</display-name></channel>
  <programme start="20200101000000 +0000" stop="20990101000000 +0000" channel="w1">
    <title>Evening News</title>
    <desc>Headlines tonight</desc>
  </programme>
  <programme start="20990101000000 +0000" stop="20990102000000 +0000" channel="w1">
    <title>Late Show</title>
  </programme>
</tv>
""",
        encoding="utf-8",
    )
    assert store.import_epg(guide) >= 1
    titled = store.channels(source_id, group="", query="Evening", offset=0, limit=5, view="all")
    assert titled["total"] >= 1 and titled["items"][0]["name"] == "Español Uno", titled
    assert "Evening News" in titled["items"][0]["now"], titled["items"][0]
    assert "Late Show" in titled["items"][0]["next"], titled["items"][0]
    detail = store.get_channel(titled["items"][0]["id"])
    assert detail and "Headlines" in detail["desc"], detail
    store.save_profile("Home", "C:/list.m3u", str(guide))
    assert store.profiles()[0]["name"] == "Home"
    local = tmp / "kabc.xml"
    local.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="KABCDT.us"><display-name>KABCDT.us</display-name></channel>
  <programme start="20200101000000 +0000" stop="20990101000000 +0000" channel="KABCDT.us">
    <title>Evening News</title>
    <desc>Headlines tonight</desc>
  </programme>
</tv>
""",
        encoding="utf-8",
    )
    store._merge_globe_file(local)
    import sqlite3

    with sqlite3.connect(store.path) as raw:
        raw.execute(
            """
            INSERT INTO channels(source_id, name, url, group_name, tvg_id, tvg_name, logo, opts)
            VALUES (?, ?, ?, ?, '', '', '', '[]')
            """,
            (source_id, "US| ABC (KABC) LOS ANGELES", "http://example.invalid/kabc", "Locals"),
        )
        local_id = int(raw.execute("SELECT last_insert_rowid()").fetchone()[0])
        raw.commit()
    local_guide = store.guide_for([local_id])
    assert local_guide and "Evening News" in local_guide[0]["now"], local_guide
    import base64

    from epg import programmes_from_listings, stream_key

    assert stream_key("http://tv.example:80/user/secret/55") == "tv.example:80/55"
    assert stream_key("https://tv.example/live/user/secret/55.ts") == "tv.example/55"
    assert stream_key("http://example.invalid/es") is None
    from globe import channel_keys, globe_keys, guess_country

    assert "kabc" in globe_keys("KABCDT.us")
    assert "kjrh" in channel_keys("US| NBC 02 (KJRH) TULSA", "", "")
    assert "bbc news" in channel_keys("UK| BBC NEWS HD", "", "")
    assert guess_country("AM | USA NBC", "") == "Usa"
    assert guess_country("[EN] SERIES", "Drama") is None
    slot = programmes_from_listings(
        [
            {
                "title": base64.b64encode(b"Evening News").decode("ascii"),
                "description": base64.b64encode(b"Headlines").decode("ascii"),
                "start_timestamp": "1000",
                "stop_timestamp": "5000",
            },
            {
                "title": "Late Show",
                "start_timestamp": "5000",
                "stop_timestamp": "8000",
            },
        ],
        now=2000,
    )
    assert slot["now_title"] == "Evening News", slot
    assert slot["next_title"] == "Late Show", slot
    assert slot["now_desc"] == "Headlines"

    channel_id = found["items"][0]["id"]
    assert store.toggle_favorite(channel_id) is True
    favs = store.channels(source_id, group="", query="", offset=0, limit=10, view="favorites")
    assert favs["total"] == 1
    junk = tmp / "public_playlists.json"
    junk.write_text('{\n  "url": "https://example.com/a.m3u"\n}\n', encoding="utf-8")
    try:
        store.import_file(junk, "name:public_playlists.json", "public_playlists")
    except ValueError as exc:
        assert "not a channel list" in str(exc)
    else:
        raise AssertionError("json catalog should not import as channels")

    empty = tmp / "empty.m3u"
    empty.write_text("#EXTM3U\n", encoding="utf-8")
    try:
        store.import_file(empty, "name:empty.m3u", "empty")
    except ValueError as exc:
        assert "No channels" in str(exc)
    else:
        raise AssertionError("empty playlist should fail")

    public = json.loads((ROOT / "public_playlists.json").read_text(encoding="utf-8"))
    listed = [item for group in public["groups"] for item in group["items"]]
    assert any(item["name"] == "News" and item["url"].endswith("/categories/news.m3u") for item in listed)
    assert any(item["name"].endswith("Sweden") for item in listed)
    assert all(not item["url"].endswith("/xxx.m3u") for item in listed)
    assert len(listed) > 400

    print(
        f"SELFTEST_OK import_s={elapsed:.2f} search_ms={search_ms:.1f} "
        f"broad_ms={broad_ms:.1f} channels={info['total']} public={len(listed)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search an IPTV M3U list and play one channel in VLC")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    serve(args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
