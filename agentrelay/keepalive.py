"""Cross-platform sleep prevention for `agentrelay run --keep-awake`.

When enabled, the OS won't put the machine to sleep while the AgentRelay
server is running — including when the laptop lid is closed. This lets
Claude Code sessions keep running while you walk away.

  - Windows: SetThreadExecutionState with ES_SYSTEM_REQUIRED
  - macOS:   spawns `caffeinate -i` (built-in command, no install needed)
  - Linux:   spawns `systemd-inhibit` (works on any systemd system)

The display is allowed to sleep on all platforms — only the *system*
stays awake. This conserves power vs. keeping the display on too.

The module is no-op on unsupported platforms. Callers should always
invoke `disable()` (or use as a context manager) so cleanup happens.
"""
from __future__ import annotations
import platform
import subprocess
import sys
from typing import Optional


class _NoopKeepAwake:
    name = "noop"
    note: str = ""

    def enable(self) -> None:
        pass

    def disable(self) -> None:
        pass


class _WindowsKeepAwake:
    name = "windows"
    # Keeps the system awake but allows the display to sleep.
    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        import ctypes

        self._SetThreadExecutionState = ctypes.windll.kernel32.SetThreadExecutionState
        self.note = "SetThreadExecutionState(ES_SYSTEM_REQUIRED) — laptop won't sleep until you stop agentrelay run"

    def enable(self) -> None:
        self._SetThreadExecutionState(self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED)

    def disable(self) -> None:
        # Clearing the flag lets the system go back to its normal sleep policy.
        self._SetThreadExecutionState(self._ES_CONTINUOUS)


class _SubprocessKeepAwake:
    """Backend for macOS (`caffeinate -i`) and Linux (`systemd-inhibit`)."""

    def __init__(self, name: str, argv: list[str], note: str) -> None:
        self.name = name
        self._argv = argv
        self.note = note
        self._proc: Optional[subprocess.Popen] = None

    def enable(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            print(
                f"[keepalive] {self._argv[0]} not found ({e}); keep-awake disabled.",
                file=sys.stderr,
            )
            self._proc = None

    def disable(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


def get_keepalive():
    """Pick a backend appropriate for the current platform.

    Always returns *something* — callers should not need to handle "unsupported".
    Unsupported platforms silently no-op so `agentrelay run --keep-awake` still
    runs (with a printed note that sleep prevention isn't active).
    """
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            return _WindowsKeepAwake()
        except Exception as e:
            print(f"[keepalive] Windows backend failed to init: {e}", file=sys.stderr)
            return _NoopKeepAwake()

    if sys_name == "darwin":
        # caffeinate is built into macOS.
        return _SubprocessKeepAwake(
            name="macos",
            argv=["caffeinate", "-i"],
            note="caffeinate -i — system won't idle-sleep while running",
        )

    if sys_name == "linux":
        # systemd-inhibit is present on most modern Linux distros. The trick
        # of running it with `sleep infinity` as the held command means it
        # blocks idle/sleep until we kill the process.
        return _SubprocessKeepAwake(
            name="linux",
            argv=[
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=agentrelay",
                "--why=Active coding agent session",
                "--mode=block",
                "sleep",
                "infinity",
            ],
            note="systemd-inhibit (idle:sleep) — requires systemd",
        )

    return _NoopKeepAwake()
