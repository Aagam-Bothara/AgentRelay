"""Cloudflare quick-tunnel wrapper.

Downloads `cloudflared` on first run if needed, then runs:
    cloudflared tunnel --url http://localhost:<port>
which assigns a free https://*.trycloudflare.com URL with no signup, no
account, no token. The URL is ephemeral — it changes on every run. For
production with a stable URL, deploy the server somewhere (Fly.io, Render)
and use that hostname instead. See README for the upgrade path.
"""
from __future__ import annotations
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from queue import Empty, Queue
from typing import Optional


# Latest cloudflared download URLs by platform.
_DOWNLOADS = {
    ("windows", "amd64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-windows-amd64.exe",
        "cloudflared.exe",
    ),
    ("windows", "arm64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-windows-arm64.exe",
        "cloudflared.exe",
    ),
    ("darwin", "amd64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-darwin-amd64.tgz",
        "cloudflared",
    ),
    ("darwin", "arm64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-darwin-arm64.tgz",
        "cloudflared",
    ),
    ("linux", "amd64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-linux-amd64",
        "cloudflared",
    ),
    ("linux", "arm64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-linux-arm64",
        "cloudflared",
    ),
}

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _platform_key() -> tuple[str, str]:
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        machine = "amd64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64"
    return (sys_name, machine)


def _bin_dir() -> Path:
    d = Path.home() / ".agentrelay" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_or_install_cloudflared() -> Path:
    """Return path to a working cloudflared binary, downloading if needed."""
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)

    key = _platform_key()
    if key not in _DOWNLOADS:
        raise RuntimeError(
            f"No cloudflared binary available for {key}. "
            f"Install manually from https://github.com/cloudflare/cloudflared/releases"
        )
    url, exe_name = _DOWNLOADS[key]
    target = _bin_dir() / exe_name
    if target.exists():
        return target

    print(f"Downloading cloudflared from {url} ...", file=sys.stderr)
    if url.endswith(".tgz"):
        import tarfile, tempfile

        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            with tarfile.open(tmp.name, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith(exe_name) or member.name == exe_name:
                        member.name = exe_name
                        tar.extract(member, _bin_dir())
                        break
        os.chmod(target, target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        urllib.request.urlretrieve(url, target)
        if not sys.platform.startswith("win"):
            os.chmod(target, target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Installed cloudflared to {target}", file=sys.stderr)
    return target


class Tunnel:
    """A managed cloudflared quick-tunnel subprocess."""

    def __init__(self, port: int = 8000) -> None:
        self.port = port
        self.url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._url_queue: Queue[str] = Queue()

    def start(self, timeout: float = 30.0) -> str:
        """Start cloudflared and return the assigned trycloudflare URL.

        Blocks until the URL is captured from cloudflared's output (or timeout).
        """
        binary = find_or_install_cloudflared()
        self._proc = subprocess.Popen(
            [
                str(binary),
                "tunnel",
                "--url",
                f"http://localhost:{self.port}",
                "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _drain():
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                if self.url is None:
                    m = _URL_RE.search(line)
                    if m:
                        self.url = m.group(0)
                        self._url_queue.put(self.url)

        self._reader_thread = threading.Thread(target=_drain, daemon=True)
        self._reader_thread.start()

        try:
            url = self._url_queue.get(timeout=timeout)
        except Empty:
            self.stop()
            raise RuntimeError(
                "cloudflared did not report a tunnel URL within "
                f"{timeout}s. Check your network."
            )
        return url

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> "Tunnel":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
