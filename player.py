"""Drive the installed VLC through its remote-control socket.

VLC receives one stream at a time. The channel list stays in the catalog.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

ALLOWED_SCHEMES = {
    "http",
    "https",
    "rtsp",
    "rtsps",
    "rtmp",
    "rtmps",
    "rtmpe",
    "rtp",
    "udp",
    "mms",
    "mmsh",
    "mmst",
    "file",
}
ALLOWED_OPTS = {"http-user-agent", "http-referrer", "http-reconnect"}
_CREATE_NO_WINDOW = 0x08000000


def find_vlc() -> Path | None:
    for candidate in (
        Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
        Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
    ):
        if candidate.is_file():
            return candidate
    found = shutil.which("vlc")
    return Path(found) if found else None


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


class VlcPlayer:
    def __init__(self):
        self.path = find_vlc()
        self.version = _version(self.path) if self.path else ""
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._buf = b""
        self._volume = 256
        self._state = {
            "playing": False,
            "paused": False,
            "title": "",
            "group": "",
            "channel_id": 0,
            "error": "",
        }

    def snapshot(self) -> dict:
        with self._lock:
            running = self._alive()
            state = dict(self._state)
        return {
            "available": self.path is not None,
            "path": str(self.path or ""),
            "version": self.version,
            "running": running,
            "playing": state["playing"] and running,
            "paused": state["paused"] and running,
            "title": state["title"],
            "group": state["group"],
            "channel_id": state["channel_id"],
            "volume": round(self._volume * 100 / 256),
            "error": state["error"],
        }

    def refresh(self) -> dict:
        with self._lock:
            if self._sock is not None:
                try:
                    self._talk("status", 0.6)
                except OSError:
                    self._drop()
        return self.snapshot()

    def play(self, channel: dict, volume_percent: int | None = None) -> dict:
        url = str(channel.get("url") or "")
        _validate_url(url)
        options = _format_options(url, channel.get("opts") or [])
        with self._lock:
            self._ensure()
            if volume_percent is not None:
                self._apply_volume(volume_percent)
            self._state["error"] = ""
            self._talk("clear", 0.8)
            lines = self._talk("add " + options, 2.5)
            text = " ".join(lines).lower()
            if "error" in text and "no error" not in text:
                self._state["error"] = "VLC could not add this stream"
            self._state["title"] = channel.get("name") or ""
            self._state["group"] = channel.get("group") or ""
            self._state["channel_id"] = int(channel.get("id") or 0)
            # `clear` reports a stop before the new stream opens. Trust an
            # explicit VLC error, otherwise treat a successful add as playing.
            self._state["playing"] = not self._state["error"]
            self._state["paused"] = False
        return self.snapshot()

    def command(self, action: str, value: int | None = None) -> dict:
        with self._lock:
            if action == "volume" and value is not None:
                self._volume = _clamp_volume(value)
                if self._sock is not None:
                    self._apply_volume(value)
                return self.snapshot()
            if action == "quit":
                self._quit()
                return self.snapshot()
            if self._sock is None:
                raise RuntimeError("VLC is not open yet")
            if action == "stop":
                self._talk("stop", 0.8)
                self._state["playing"] = False
                self._state["paused"] = False
            elif action == "pause":
                self._talk("pause", 0.8)
                self._state["paused"] = not self._state["paused"]
                self._state["playing"] = not self._state["paused"]
            elif action == "fullscreen":
                self._talk("fullscreen", 0.8)
            else:
                raise ValueError("Unknown player action")
        return self.snapshot()

    def _apply_volume(self, percent: int) -> None:
        self._volume = _clamp_volume(percent)
        self._talk(f"volume {self._volume}", 0.5)

    def _ensure(self) -> None:
        if self._sock is not None and self._alive():
            return
        self._drop()
        adopted = self._adopt()
        if adopted is not None:
            self._sock = adopted
            self._buf = b""
            return
        self._launch()

    def _adopt(self) -> socket.socket | None:
        for port in range(4212, 4222):
            sock = _probe(port)
            if sock is not None:
                return sock
        return None

    def _launch(self) -> None:
        if self.path is None:
            raise RuntimeError("VLC was not found. Install it from videolan.org, then start this tool again.")
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        port = _free_port()
        self._proc = subprocess.Popen(
            [
                str(self.path),
                "--no-one-instance",
                "--intf",
                "qt",
                "--extraintf",
                "rc",
                "--rc-host",
                f"127.0.0.1:{port}",
                "--no-video-title-show",
                "--no-qt-privacy-ask",
                "--no-qt-error-dialogs",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.path.parent),
            **_popen_kwargs(),
        )
        deadline = time.time() + 6
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("VLC exited before the control port opened")
            sock = _probe(port)
            if sock is not None:
                self._sock = sock
                self._buf = b""
                return
            time.sleep(0.12)
        raise RuntimeError("VLC did not open its control port")

    def _talk(self, line: str, wait: float) -> list[str]:
        if self._sock is None:
            raise OSError("VLC is not connected")
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
            incoming = self._read(wait)
        except OSError:
            self._drop()
            raise
        return self._consume(incoming)

    def _read(self, wait: float) -> list[str]:
        assert self._sock is not None
        lines: list[str] = []
        self._sock.settimeout(0.12)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                chunk = self._sock.recv(8192)
            except socket.timeout:
                if lines:
                    break
                continue
            if not chunk:
                raise OSError("VLC closed the control connection")
            self._buf += chunk
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                text = raw.decode("utf-8", "replace").strip("\r")
                if text:
                    lines.append(text)
            deadline = min(deadline, time.time() + 0.2)
        return lines

    def _consume(self, lines: list[str]) -> list[str]:
        messages = []
        for line in lines:
            if line.startswith("status change:"):
                self._apply_status(line)
                continue
            if line.strip() == ">":
                continue
            messages.append(line)
            if line in {"0", "1"}:
                self._state["playing"] = line == "1"
                self._state["paused"] = False
                if line == "1":
                    self._state["error"] = ""
        return messages

    def _apply_status(self, line: str) -> None:
        start = line.find("(")
        end = line.rfind(")")
        if start < 0 or end <= start:
            return
        body = line[start + 1 : end]
        if ":" not in body:
            return
        key, value = body.split(":", 1)
        key, value = key.strip(), value.strip()
        if key == "play state":
            self._state["playing"] = value == "3"
            self._state["paused"] = value == "4"
            if value == "7":
                self._state["error"] = "VLC could not open this stream"
            elif value == "3":
                self._state["error"] = ""
        elif key == "stop state":
            self._state["playing"] = False
            self._state["paused"] = False
        elif key == "audio volume":
            try:
                self._volume = int(value)
            except ValueError:
                pass

    def _quit(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(b"quit\n")
            except OSError:
                pass
        self._drop()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
        self._state["playing"] = False
        self._state["paused"] = False
        self._state["title"] = ""
        self._state["group"] = ""
        self._state["error"] = ""
        self._state["channel_id"] = 0

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    def _alive(self) -> bool:
        if self._sock is None:
            return False
        if self._proc is not None and self._proc.poll() is not None:
            return False
        return True


def _validate_url(url: str) -> None:
    if not url or any(char in url for char in "\r\n\x00"):
        raise ValueError("That channel has no playable address")
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("This address type cannot be opened in VLC")


def _format_options(url: str, opts: list) -> str:
    """Return the RC `add` argument: one MRL plus allowlisted VLC options."""
    mrl = url.replace(" ", "%20")
    parts = [mrl]
    for item in opts:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        key, value = str(item[0]).lower(), str(item[1])
        if key not in ALLOWED_OPTS:
            continue
        value = value.replace("\r", "").replace("\n", "").replace('"', "").strip()
        if not value:
            continue
        parts.append(f':{key}={value}')
    return " ".join(parts)


def _clamp_volume(percent: int) -> int:
    percent = max(0, min(150, int(percent)))
    return round(percent * 256 / 100)


def _free_port() -> int:
    for port in range(4212, 4222):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    raise RuntimeError("No free local port for VLC")


def _probe(port: int) -> socket.socket | None:
    try:
        sock = socket.create_connection(("127.0.0.1", port), 0.25)
    except OSError:
        return None
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(0.45)
    try:
        sock.sendall(b"status\n")
        data = b""
        deadline = time.time() + 0.7
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if b"returned" in data or b"status change" in data:
                deadline = min(deadline, time.time() + 0.15)
    except OSError:
        sock.close()
        return None
    if b"returned" not in data and b"status change" not in data:
        sock.close()
        return None
    return sock


def _version(path: Path) -> str:
    try:
        out = subprocess.check_output(
            [str(path), "--version"],
            stderr=subprocess.STDOUT,
            timeout=5,
            cwd=str(path.parent),
            **_popen_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    line = out.decode("utf-8", "replace").splitlines()
    return line[0].strip() if line else ""
