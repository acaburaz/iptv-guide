"""SQLite catalog for large IPTV lists.

Channels are queried in pages. Search uses FTS5 so the UI never receives
the full playlist.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from epg import fetch_short_epg, guide_fields, load_guide, stream_key
from globe import channel_keys, download_country, globe_keys, guess_country
from m3u import iter_channels, looks_like_playlist

_TOKEN = re.compile(r"[0-9A-Za-z\u00C0-\u024F\u0400-\u04FF]+")
_INSERT = """
INSERT INTO channels (source_id, name, url, group_name, tvg_id, tvg_name, logo, opts)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        conn = self._connect()
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    origin TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL,
                    channel_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    tvg_id TEXT NOT NULL DEFAULT '',
                    tvg_name TEXT NOT NULL DEFAULT '',
                    logo TEXT NOT NULL DEFAULT '',
                    opts TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_ch_source_group_name
                    ON channels(source_id, group_name, name, id);
                CREATE INDEX IF NOT EXISTS idx_ch_source_name
                    ON channels(source_id, name, id);
                CREATE INDEX IF NOT EXISTS idx_ch_url
                    ON channels(url);
                CREATE INDEX IF NOT EXISTS idx_ch_source_tvg
                    ON channels(source_id, tvg_id);
                CREATE INDEX IF NOT EXISTS idx_ch_source_tvg_name
                    ON channels(source_id, tvg_name);

                CREATE VIRTUAL TABLE IF NOT EXISTS channels_fts USING fts5(
                    name, group_name, tvg_name, tvg_id,
                    content='channels',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS source_groups (
                    source_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL,
                    n INTEGER NOT NULL,
                    PRIMARY KEY (source_id, group_name)
                );

                CREATE TABLE IF NOT EXISTS epg_now (
                    tvg_id TEXT PRIMARY KEY,
                    now_title TEXT NOT NULL DEFAULT '',
                    now_start INTEGER NOT NULL DEFAULT 0,
                    now_stop INTEGER NOT NULL DEFAULT 0,
                    now_desc TEXT NOT NULL DEFAULT '',
                    next_title TEXT NOT NULL DEFAULT '',
                    next_start INTEGER NOT NULL DEFAULT 0,
                    next_stop INTEGER NOT NULL DEFAULT 0
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS epg_fts USING fts5(
                    now_title,
                    next_title,
                    tvg_id UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    name TEXT PRIMARY KEY,
                    m3u TEXT NOT NULL DEFAULT '',
                    epg TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS epg_globe (
                    country TEXT PRIMARY KEY,
                    fetched_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS epg_fallback (
                    name_key TEXT PRIMARY KEY,
                    now_title TEXT NOT NULL DEFAULT '',
                    now_start INTEGER NOT NULL DEFAULT 0,
                    now_stop INTEGER NOT NULL DEFAULT 0,
                    now_desc TEXT NOT NULL DEFAULT '',
                    next_title TEXT NOT NULL DEFAULT '',
                    next_start INTEGER NOT NULL DEFAULT 0,
                    next_stop INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS epg_cache (
                    stream_key TEXT PRIMARY KEY,
                    now_title TEXT NOT NULL DEFAULT '',
                    now_start INTEGER NOT NULL DEFAULT 0,
                    now_stop INTEGER NOT NULL DEFAULT 0,
                    now_desc TEXT NOT NULL DEFAULT '',
                    next_title TEXT NOT NULL DEFAULT '',
                    next_start INTEGER NOT NULL DEFAULT 0,
                    next_stop INTEGER NOT NULL DEFAULT 0,
                    fetched_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    url TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    logo TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS history (
                    url TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    played_at TEXT NOT NULL
                );
                """
            )
            self._ensure_triggers(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _ensure_triggers(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS channels_ai AFTER INSERT ON channels BEGIN
                INSERT INTO channels_fts(rowid, name, group_name, tvg_name, tvg_id)
                VALUES (new.id, new.name, new.group_name, new.tvg_name, new.tvg_id);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS channels_ad AFTER DELETE ON channels BEGIN
                INSERT INTO channels_fts(channels_fts, rowid, name, group_name, tvg_name, tvg_id)
                VALUES ('delete', old.id, old.name, old.group_name, old.tvg_name, old.tvg_id);
            END
            """
        )

    def _drop_triggers(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TRIGGER IF EXISTS channels_ai")
        conn.execute("DROP TRIGGER IF EXISTS channels_ad")

    def _report(self, on_progress, count: int, phase: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(count, phase)
        except TypeError:
            on_progress(count)

    @contextmanager
    def _session(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def import_file(self, path: Path, origin: str, name: str, on_progress=None) -> int:
        """Replace the playlist stored at `origin`. Returns the source id."""
        if not looks_like_playlist(path):
            raise ValueError(
                "That file is not a channel list. Open Public and pick a category, country, or language, or choose an .m3u file."
            )
        with self._write_lock:
            conn = self._connect()
            try:
                # One FTS rebuild beats a trigger per channel on a multi-hundred-MB list.
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("PRAGMA cache_size=-65536")
                conn.execute("BEGIN IMMEDIATE")
                self._drop_triggers(conn)
                sid = self._upsert_source(conn, origin, name)
                conn.execute("DELETE FROM source_groups WHERE source_id = ?", (sid,))
                batch: list[tuple] = []
                group_counts: dict[str, int] = {}
                count = 0

                def flush() -> None:
                    nonlocal count
                    if not batch:
                        return
                    conn.executemany(_INSERT, batch)
                    count += len(batch)
                    batch.clear()
                    self._report(on_progress, count, "read")

                for channel in iter_channels(path, origin):
                    group = channel["group"] or "Ungrouped"
                    group_counts[group] = group_counts.get(group, 0) + 1
                    opts = channel["opts"]
                    batch.append(
                        (
                            sid,
                            channel["name"],
                            channel["url"],
                            group,
                            channel["tvg_id"],
                            channel["tvg_name"],
                            channel["logo"],
                            "[]" if not opts else json.dumps(opts, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                    if len(batch) >= 8000:
                        flush()
                flush()
                if count == 0:
                    raise ValueError("No channels found in that playlist")
                conn.executemany(
                    "INSERT INTO source_groups(source_id, group_name, n) VALUES (?, ?, ?)",
                    [(sid, group, n) for group, n in group_counts.items()],
                )
                conn.execute(
                    "UPDATE sources SET channel_count = ? WHERE id = ?",
                    (count, sid),
                )
                self._report(on_progress, count, "search")
                conn.execute("INSERT INTO channels_fts(channels_fts) VALUES('rebuild')")
                self._ensure_triggers(conn)
                conn.commit()
                self._report(on_progress, count, "done")
                return sid
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                try:
                    conn.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.Error:
                    pass
                conn.close()

    def import_epg(self, path: Path, on_progress=None) -> int:
        """Replace the NOW/NEXT guide. Returns how many channels got a programme."""
        rows, seen, channels = load_guide(path, on_progress)
        if seen == 0:
            raise ValueError("No programmes found in that guide")
        payload = []
        searchable = []
        for key, slot in rows.items():
            now = slot["now"] or {}
            nxt = slot["next"] or {}
            payload.append(
                (
                    key,
                    now.get("title") or "",
                    int(now.get("start") or 0),
                    int(now.get("stop") or 0),
                    now.get("desc") or "",
                    nxt.get("title") or "",
                    int(nxt.get("start") or 0),
                    int(nxt.get("stop") or 0),
                )
            )
            searchable.append((now.get("title") or "", nxt.get("title") or "", key))
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM epg_now")
                conn.execute("DELETE FROM epg_fts")
                conn.executemany(
                    """
                    INSERT INTO epg_now(
                        tvg_id, now_title, now_start, now_stop, now_desc,
                        next_title, next_start, next_stop
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                conn.executemany(
                    "INSERT INTO epg_fts(now_title, next_title, tvg_id) VALUES (?, ?, ?)",
                    searchable,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                try:
                    conn.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.Error:
                    pass
                conn.close()
        self._report(on_progress, channels, "done")
        return channels

    def save_profile(self, name: str, m3u: str, epg: str) -> None:
        label = " ".join(name.replace("\x00", "").split())[:120] or "Profile"
        with self._write_lock:
            with self._session() as conn:
                conn.execute(
                    """
                    INSERT INTO profiles(name, m3u, epg) VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET m3u = excluded.m3u, epg = excluded.epg
                    """,
                    (label, m3u[:2000], epg[:2000]),
                )

    def delete_profile(self, name: str) -> None:
        with self._write_lock:
            with self._session() as conn:
                conn.execute("DELETE FROM profiles WHERE name = ?", (name,))

    def profiles(self) -> list[dict]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT name, m3u, epg FROM profiles ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [{"name": row["name"], "m3u": row["m3u"], "epg": row["epg"]} for row in rows]

    def _upsert_source(self, conn: sqlite3.Connection, origin: str, name: str) -> int:
        row = conn.execute("SELECT id FROM sources WHERE origin = ?", (origin,)).fetchone()
        stamp = now_iso()
        if row:
            sid = int(row["id"])
            conn.execute("DELETE FROM channels WHERE source_id = ?", (sid,))
            conn.execute(
                "UPDATE sources SET name = ?, imported_at = ?, channel_count = 0 WHERE id = ?",
                (name, stamp, sid),
            )
            return sid
        cur = conn.execute(
            "INSERT INTO sources(name, origin, imported_at, channel_count) VALUES (?, ?, ?, 0)",
            (name, origin, stamp),
        )
        return int(cur.lastrowid)

    def sources(self) -> list[dict]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT id, name, origin, imported_at, channel_count FROM sources ORDER BY imported_at DESC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "origin": row["origin"],
                "imported_at": row["imported_at"],
                "channel_count": row["channel_count"],
                "demo": row["origin"] == "demo:iptv-rack",
            }
            for row in rows
        ]

    def delete_source(self, source_id: int) -> bool:
        with self._write_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if not row:
                    return False
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("BEGIN IMMEDIATE")
                self._drop_triggers(conn)
                conn.execute("DELETE FROM channels WHERE source_id = ?", (source_id,))
                conn.execute("DELETE FROM source_groups WHERE source_id = ?", (source_id,))
                conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
                conn.execute("INSERT INTO channels_fts(channels_fts) VALUES('rebuild')")
                self._ensure_triggers(conn)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                try:
                    conn.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.Error:
                    pass
                conn.close()
        return True

    def groups(self, source_id: int) -> dict:
        with self._session() as conn:
            total = conn.execute(
                "SELECT channel_count FROM sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT group_name, n
                FROM source_groups
                WHERE source_id = ?
                ORDER BY group_name COLLATE NOCASE
                """,
                (source_id,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT group_name, COUNT(*) AS n
                    FROM channels
                    WHERE source_id = ?
                    GROUP BY group_name
                    ORDER BY group_name COLLATE NOCASE
                    """,
                    (source_id,),
                ).fetchall()
            favorites = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM favorites f
                WHERE EXISTS (
                    SELECT 1 FROM channels c
                    WHERE c.source_id = ? AND c.url = f.url
                )
                """,
                (source_id,),
            ).fetchone()["n"]
            recent = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM history h
                WHERE EXISTS (
                    SELECT 1 FROM channels c
                    WHERE c.source_id = ? AND c.url = h.url
                )
                """,
                (source_id,),
            ).fetchone()["n"]
        return {
            "total": int(total["channel_count"]) if total else 0,
            "favorites": int(favorites),
            "recent": int(recent),
            "groups": [{"name": row["group_name"], "count": row["n"]} for row in rows],
        }

    def channels(
        self,
        source_id: int,
        *,
        group: str,
        query: str,
        offset: int,
        limit: int,
        view: str,
    ) -> dict:
        offset = max(0, offset)
        limit = min(300, max(1, limit))
        view = view if view in {"all", "favorites", "recent"} else "all"
        match = fts_query(query)
        where = ["c.source_id = ?"]
        params: list = [source_id]
        if view == "all" and group:
            where.append("c.group_name = ?")
            params.append(group)
        if view == "favorites":
            where.append("EXISTS (SELECT 1 FROM favorites f WHERE f.url = c.url)")
        if view == "recent":
            where.append("EXISTS (SELECT 1 FROM history h WHERE h.url = c.url)")

        # CROSS JOIN keeps SQLite from probing the full-text index once per channel.
        if match:
            from_sql = "channels_fts CROSS JOIN channels c ON c.id = channels_fts.rowid"
            where.insert(0, "channels_fts MATCH ?")
            params.insert(0, match)
            order = (
                "(SELECT played_at FROM history h WHERE h.url = c.url) DESC, c.id"
                if view == "recent"
                else "bm25(channels_fts), c.name COLLATE NOCASE, c.id"
            )
        else:
            from_sql = "channels c"
            order = (
                "(SELECT played_at FROM history h WHERE h.url = c.url) DESC, c.name COLLATE NOCASE, c.id"
                if view == "recent"
                else "c.name COLLATE NOCASE, c.id"
            )

        where_sql = " AND ".join(where)
        count_sql = f"SELECT COUNT(*) AS n FROM {from_sql} WHERE {where_sql}"
        page_sql = f"""
            SELECT c.id, c.name, c.group_name, c.logo, c.tvg_id, c.tvg_name,
                   EXISTS(SELECT 1 FROM favorites f WHERE f.url = c.url) AS favorite
            FROM {from_sql}
            WHERE {where_sql}
            ORDER BY {order}
            LIMIT ? OFFSET ?
        """
        with self._session() as conn:
            if not match and view == "all" and not group:
                total_row = conn.execute(
                    "SELECT channel_count AS n FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
                total = int(total_row["n"]) if total_row else 0
            else:
                total = int(conn.execute(count_sql, params).fetchone()["n"])
            rows = conn.execute(page_sql, [*params, limit, offset]).fetchall()
            if match and total == 0 and view == "all":
                group_sql = " AND ch.group_name = ?" if group else ""
                group_params = [group] if group else []
                id_sql = f"""
                    SELECT ch.id
                    FROM epg_fts
                    JOIN channels ch ON ch.source_id = ? AND ch.tvg_id = epg_fts.tvg_id
                    WHERE ch.tvg_id != '' AND epg_fts MATCH ?{group_sql}
                    UNION
                    SELECT ch.id
                    FROM epg_fts
                    JOIN channels ch ON ch.source_id = ? AND ch.tvg_name = epg_fts.tvg_id
                    WHERE ch.tvg_name != '' AND epg_fts MATCH ?{group_sql}
                """
                id_params = [source_id, match, *group_params, source_id, match, *group_params]
                total = int(
                    conn.execute(f"SELECT COUNT(*) AS n FROM ({id_sql})", id_params).fetchone()["n"]
                )
                guide_sql = f"""
                    SELECT c.id, c.name, c.group_name, c.logo, c.tvg_id, c.tvg_name,
                           EXISTS(SELECT 1 FROM favorites f WHERE f.url = c.url) AS favorite
                    FROM channels c
                    WHERE c.id IN ({id_sql})
                    ORDER BY c.name COLLATE NOCASE, c.id
                    LIMIT ? OFFSET ?
                """
                rows = conn.execute(guide_sql, [*id_params, limit, offset]).fetchall()
            guides = self._guides_for(conn, rows)
        return {
            "total": total,
            "offset": offset,
            "items": [self._channel_item(row, guides) for row in rows],
        }

    def _guides_for(self, conn: sqlite3.Connection, rows) -> dict:
        keys = []
        for row in rows:
            key = row["tvg_id"] or row["tvg_name"] or ""
            if key:
                keys.append(key)
        if not keys:
            return {}
        unique = list(dict.fromkeys(keys))
        found = conn.execute(
            f"SELECT * FROM epg_now WHERE tvg_id IN ({','.join('?' * len(unique))})",
            unique,
        ).fetchall()
        return {item["tvg_id"]: item for item in found}

    def _channel_item(self, row, guides: dict) -> dict:
        guide = guides.get(row["tvg_id"]) or guides.get(row["tvg_name"]) or None
        fields = guide_fields(guide)
        return {
            "id": row["id"],
            "name": row["name"],
            "group": row["group_name"],
            "logo": row["logo"],
            "tvg_id": row["tvg_id"],
            "favorite": bool(row["favorite"]) if "favorite" in row.keys() else False,
            "now": fields["now"],
            "next": fields["next"],
            "left": fields["left"],
            "desc": fields["desc"],
        }

    def _merge_globe_file(self, path: Path) -> None:
        rows, _seen, _count = load_guide(path)
        payload = []
        used: set[str] = set()
        for label, slot in rows.items():
            now = slot["now"] or {}
            nxt = slot["next"] or {}
            if not now and not nxt:
                continue
            record = (
                now.get("title") or "",
                int(now.get("start") or 0),
                int(now.get("stop") or 0),
                now.get("desc") or "",
                nxt.get("title") or "",
                int(nxt.get("start") or 0),
                int(nxt.get("stop") or 0),
            )
            for key in globe_keys(label):
                if key in used:
                    continue
                used.add(key)
                payload.append((key, *record))
        if not payload:
            return
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """
                    INSERT INTO epg_fallback(
                        name_key, now_title, now_start, now_stop, now_desc,
                        next_title, next_start, next_stop
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name_key) DO UPDATE SET
                        now_title = excluded.now_title,
                        now_start = excluded.now_start,
                        now_stop = excluded.now_stop,
                        now_desc = excluded.now_desc,
                        next_title = excluded.next_title,
                        next_start = excluded.next_start,
                        next_stop = excluded.next_stop
                    """,
                    payload,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _ensure_globe(self, country: str) -> None:
        fresh = time.time() - 12 * 3600
        with self._session() as conn:
            row = conn.execute(
                "SELECT fetched_at FROM epg_globe WHERE country = ?",
                (country,),
            ).fetchone()
        if row and int(row["fetched_at"] or 0) >= fresh:
            return
        dest = self.path.parent / "epg" / "globe" / country
        saved = download_country(country, dest)
        if not saved and dest.is_dir():
            saved = sorted(dest.glob("*.xml.gz"))
        if not saved:
            return
        for path in saved:
            self._merge_globe_file(path)
        with self._write_lock:
            with self._session() as conn:
                conn.execute(
                    """
                    INSERT INTO epg_globe(country, fetched_at) VALUES (?, ?)
                    ON CONFLICT(country) DO UPDATE SET fetched_at = excluded.fetched_at
                    """,
                    (country, int(time.time())),
                )

    def _apply_fallback(self, by_id: dict, meta: dict, channel_ids: list[int]) -> None:
        wanted: dict[int, list[str]] = {}
        keys: list[str] = []
        for channel_id in channel_ids:
            row = meta.get(channel_id)
            item = by_id.get(channel_id)
            if not row or not item or item.get("now"):
                continue
            found = channel_keys(row["name"], row["tvg_name"] or "", row["tvg_id"] or "")
            if not found:
                continue
            wanted[channel_id] = found
            keys.extend(found)
        if not keys:
            return
        found_rows: dict[str, sqlite3.Row] = {}
        unique_keys = list(dict.fromkeys(keys))
        with self._session() as conn:
            for start in range(0, len(unique_keys), 400):
                chunk = unique_keys[start : start + 400]
                matched = conn.execute(
                    f"SELECT * FROM epg_fallback WHERE name_key IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                for match in matched:
                    found_rows[match["name_key"]] = match
        for channel_id, names in wanted.items():
            for key in names:
                row = found_rows.get(key)
                if row and (row["now_title"] or row["next_title"]):
                    fields = guide_fields(row)
                    fields["id"] = channel_id
                    by_id[channel_id].update(fields)
                    break

    def _fill_fallback(self, items: list[dict], meta: dict, pending: list[int]) -> None:
        by_id = {item["id"]: item for item in items}
        self._apply_fallback(by_id, meta, pending)
        still = [channel_id for channel_id in pending if not by_id[channel_id].get("now")]
        if not still:
            return
        counts: dict[str, int] = {}
        for channel_id in still:
            row = meta.get(channel_id)
            if not row:
                continue
            country = guess_country(row["group_name"], row["name"], row["tvg_name"] or "")
            if country:
                counts[country] = counts.get(country, 0) + 1
        for country, _count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:2]:
            self._ensure_globe(country)
        if counts:
            self._apply_fallback(by_id, meta, still)

    def guide_for(self, ids: list[int]) -> list[dict]:
        """NOW/NEXT for a visible page. Portal guide first, Globe TV if that is empty."""
        unique: list[int] = []
        seen: set[int] = set()
        for raw in ids:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in seen:
                seen.add(number)
                unique.append(number)
            if len(unique) >= 40:
                break
        if not unique:
            return []
        with self._session() as conn:
            rows = conn.execute(
                f"""
                SELECT id, url, name, tvg_id, tvg_name, group_name
                FROM channels WHERE id IN ({','.join('?' * len(unique))})
                """,
                unique,
            ).fetchall()
            meta = {int(row["id"]): row for row in rows}
            url_by_id = {channel_id: row["url"] for channel_id, row in meta.items()}
            key_by_id = {channel_id: stream_key(url) for channel_id, url in url_by_id.items()}
            key_by_id = {channel_id: key for channel_id, key in key_by_id.items() if key}
            cached: dict[str, sqlite3.Row] = {}
            lookup = list(dict.fromkeys(key_by_id.values()))
            if lookup:
                found = conn.execute(
                    f"SELECT * FROM epg_cache WHERE stream_key IN ({','.join('?' * len(lookup))})",
                    lookup,
                ).fetchall()
                cached = {item["stream_key"]: item for item in found}
        fresh = time.time() - 600
        missing: list[tuple[str, str]] = []
        queued: set[str] = set()
        for channel_id, key in key_by_id.items():
            row = cached.get(key)
            if row is not None and int(row["fetched_at"] or 0) >= fresh:
                continue
            if key in queued:
                continue
            queued.add(key)
            missing.append((key, url_by_id[channel_id]))
        fetched: dict[str, dict] = {}
        if missing:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(fetch_short_epg, url): key for key, url in missing}
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        slot = future.result()
                    except Exception:
                        slot = None
                    if slot is not None:
                        fetched[key] = slot
            if fetched:
                with self._write_lock:
                    conn = self._connect()
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.executemany(
                            """
                            INSERT INTO epg_cache(
                                stream_key, now_title, now_start, now_stop, now_desc,
                                next_title, next_start, next_stop, fetched_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(stream_key) DO UPDATE SET
                                now_title = excluded.now_title,
                                now_start = excluded.now_start,
                                now_stop = excluded.now_stop,
                                now_desc = excluded.now_desc,
                                next_title = excluded.next_title,
                                next_start = excluded.next_start,
                                next_stop = excluded.next_stop,
                                fetched_at = excluded.fetched_at
                            """,
                            [
                                (
                                    key,
                                    slot["now_title"],
                                    slot["now_start"],
                                    slot["now_stop"],
                                    slot["now_desc"],
                                    slot["next_title"],
                                    slot["next_start"],
                                    slot["next_stop"],
                                    slot["fetched_at"],
                                )
                                for key, slot in fetched.items()
                            ],
                        )
                        conn.commit()
                    except Exception:
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                        raise
                    finally:
                        conn.close()
        items = []
        pending = []
        for channel_id in unique:
            key = key_by_id.get(channel_id)
            row = fetched.get(key) if key else None
            if row is None and key:
                row = cached.get(key)
            fields = guide_fields(row)
            fields["id"] = channel_id
            if not fields["now"]:
                pending.append(channel_id)
            items.append(fields)
        if pending:
            self._fill_fallback(items, meta, pending)
        return items

    def get_channel(self, channel_id: int) -> dict | None:
        with self._session() as conn:
            row = conn.execute(
                """
                SELECT id, name, url, group_name, logo, opts, tvg_id, tvg_name
                FROM channels WHERE id = ?
                """,
                (channel_id,),
            ).fetchone()
            guides = self._guides_for(conn, [row]) if row else {}
        if not row:
            return None
        try:
            opts = json.loads(row["opts"] or "[]")
        except json.JSONDecodeError:
            opts = []
        item = self._channel_item(row, guides)
        item["url"] = row["url"]
        item["opts"] = opts if isinstance(opts, list) else []
        return item

    def toggle_favorite(self, channel_id: int) -> bool:
        channel = self.get_channel(channel_id)
        if not channel:
            raise KeyError(channel_id)
        with self._write_lock:
            with self._session() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM favorites WHERE url = ?",
                    (channel["url"],),
                ).fetchone()
                if existing:
                    conn.execute("DELETE FROM favorites WHERE url = ?", (channel["url"],))
                    conn.commit()
                    return False
                conn.execute(
                    "INSERT INTO favorites(url, name, group_name, logo) VALUES (?, ?, ?, ?)",
                    (channel["url"], channel["name"], channel["group"], channel["logo"]),
                )
                conn.commit()
                return True

    def remember(self, channel: dict) -> None:
        with self._write_lock:
            with self._session() as conn:
                conn.execute(
                    """
                    INSERT INTO history(url, name, group_name, played_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        name = excluded.name,
                        group_name = excluded.group_name,
                        played_at = excluded.played_at
                    """,
                    (channel["url"], channel["name"], channel["group"], now_iso()),
                )
                conn.execute(
                    """
                    DELETE FROM history
                    WHERE url NOT IN (
                        SELECT url FROM history ORDER BY played_at DESC LIMIT 40
                    )
                    """
                )
                conn.commit()


def fts_query(text: str) -> str | None:
    tokens = _TOKEN.findall(text or "")[:8]
    if not tokens:
        return None
    return " AND ".join(f'"{token}"*' for token in tokens)


def write_demo(path: Path, per_group: int = 750) -> None:
    """Write a searchable stand-in list plus three public-domain sample films."""
    groups = [
        "News",
        "Sports",
        "Movies",
        "Kids",
        "Music",
        "Documentary",
        "Local",
        "Radio",
    ]
    samples = [
        ("Sample Flower", "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"),
        ("Sample Big Buck Bunny", "https://www.w3schools.com/html/mov_bbb.mp4"),
        ("Sample Sintel Trailer", "https://media.w3.org/2010/05/sintel/trailer.mp4"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        for name, url in samples:
            handle.write(f'#EXTINF:-1 tvg-id="sample" group-title="Samples",{name}\n{url}\n')
        for group in groups:
            for number in range(1, per_group + 1):
                title = f"{group} {number:04d}"
                handle.write(
                    f'#EXTINF:-1 tvg-id="{group.lower()}-{number}" group-title="{group}",{title}\n'
                    f"http://example.invalid/iptv/{group.lower()}/{number}\n"
                )
