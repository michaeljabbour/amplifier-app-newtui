"""Thin stdlib client over ``amplifier-skill-forge``'s ``forge.py``.

The forge capability tier (:mod:`tests.forge`) drives the shipped
``amplifier-newtui`` binary through a real PTY via the forge terminal
daemon.  This module wraps the handful of ``forge.py`` subcommands the
tier needs (``doctor``/``new``/``type``/``key``/``screen``/``wait``/
``close``/``close-tag``) behind a small subprocess client so the test
modules stay declarative.

Design constraints (docs/plans/2026-07-22-forge-capability-tier.md):

- **No sleeps as synchronization.**  Every wait is a bounded
  ``forge wait <regex> --timeout`` looped past forge's ~30 s server-side
  cap (:meth:`ForgeSession.wait`) -- never ``time.sleep``.
- **Single-token anchors.**  ``forge grep``/``wait`` warn that ANSI can
  split phrases in the buffer, so callers anchor on single words/glyphs.
- **Raw-byte control chars.**  ``forge key`` only accepts a fixed key
  list (``ctrl+c``/``ctrl+u``/...); ``ctrl+o`` (cycle tail) and ``ctrl+t``
  (toggle lanes) are outside it, so :meth:`ForgeSession.press_ctrl`
  falls back to ``type`` with the raw control byte -- the documented
  forge pattern (SKILL.md Rules).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# forge wait is capped ~30 s server-side; keep each call under that.
_PER_WAIT_MS = 20_000
# Extra head-room over the forge --timeout so the subprocess never hangs.
_SUBPROCESS_SLACK_S = 15.0


class ForgeError(RuntimeError):
    """A ``forge.py`` invocation failed in a way the tier cannot recover from."""


def resolve_forge() -> Path | None:
    """Locate ``forge.py`` -- ``$FORGE`` first, then the known skill dirs.

    Returns ``None`` when the helper cannot be found so the fixture layer
    can *skip* the tier (never fail) on a machine without the skill.
    """
    env = os.environ.get("FORGE")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_file():
            return candidate
    for base in (
        Path.home() / ".claude" / "skills" / "amplifier-skill-forge",
        Path.home() / ".amplifier" / "skills" / "amplifier-skill-forge",
    ):
        candidate = base / "tools" / "forge.py"
        if candidate.is_file():
            return candidate
    return None


def _control_byte(letter: str) -> str:
    r""""o" -> "\x0f" (ctrl+o); "t" -> "\x14" (ctrl+t)."""
    if len(letter) != 1 or not letter.isalpha():
        raise ValueError(f"expected a single letter, got {letter!r}")
    return chr(ord(letter.lower()) - ord("a") + 1)


@dataclass
class ForgeSession:
    """A single PTY session opened via ``forge new``."""

    forge: ForgeClient
    session_id: str

    # -- observation --------------------------------------------------------

    def screen(self) -> str:
        """The rendered viewport (ANSI stripped by forge)."""
        return self.forge.run("screen", self.session_id).stdout

    def wait(self, regex: str, *, total_timeout_ms: int = 60_000) -> bool:
        """Bounded wait for *regex*, looping past forge's ~30 s cap.

        Returns ``True`` on match, ``False`` on deadline -- the caller
        turns a miss into an assertion so the failure names the anchor.
        """
        deadline = time.monotonic() + total_timeout_ms / 1000.0
        remaining_ms = total_timeout_ms
        while remaining_ms > 0:
            per_call = min(_PER_WAIT_MS, remaining_ms)
            result = self.forge.run(
                "wait",
                self.session_id,
                regex,
                "--timeout",
                str(per_call),
                check=False,
                timeout_s=per_call / 1000.0 + _SUBPROCESS_SLACK_S,
            )
            if result.returncode == 0:
                return True
            remaining_ms = int((deadline - time.monotonic()) * 1000)
        return False

    def screen_contains(self, *tokens: str) -> bool:
        screen = self.screen()
        return all(token in screen for token in tokens)

    # -- input --------------------------------------------------------------

    def type(self, text: str, *, newline: bool = False) -> None:
        args = ["type", self.session_id, text]
        if not newline:
            args.append("--no-newline")
        self.forge.run(*args)

    def key(self, name: str) -> None:
        self.forge.run("key", self.session_id, name)

    def submit(self, text: str) -> None:
        """Type *text* then Enter -- the composer clears on submit."""
        self.type(text, newline=False)
        self.key("enter")

    def press_ctrl(self, letter: str) -> None:
        """Send ctrl+<letter> as a raw control byte (forge ``key`` gap)."""
        self.type(_control_byte(letter), newline=False)

    # -- teardown -----------------------------------------------------------

    def close(self) -> None:
        self.forge.run("close", self.session_id, check=False)


class ForgeClient:
    """Top-level ``forge.py`` wrapper: doctor + session lifecycle."""

    def __init__(self, forge_path: Path) -> None:
        self.forge_path = forge_path

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout_s: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(self.forge_path), *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
            raise ForgeError(f"forge {args[0]} timed out after {timeout_s}s") from exc
        if check and result.returncode != 0:
            raise ForgeError(
                f"forge {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def doctor(self) -> bool:
        """``forge doctor`` -- starts the daemon if down; ``True`` when healthy."""
        try:
            result = self.run("doctor", check=False, timeout_s=90.0)
        except ForgeError:
            return False
        return result.returncode == 0 and "healthy" in result.stdout.lower()

    def new(
        self,
        *,
        program: str,
        args: tuple[str, ...] = (),
        cwd: str,
        cols: int = 120,
        rows: int = 40,
        tag: str,
        name: str = "newtui-cap",
    ) -> ForgeSession:
        cmd = [
            "new",
            "--name",
            name,
            "--cwd",
            cwd,
            "--program",
            program,
            "--cols",
            str(cols),
            "--rows",
            str(rows),
            "--tag",
            tag,
        ]
        for arg in args:
            # ``--arg=VALUE`` keeps leading-dash values (e.g. ``--demo``)
            # from being parsed as forge's own options.
            cmd.append(f"--arg={arg}")
        result = self.run(*cmd, timeout_s=30.0)
        session_id = _parse_session_id(result.stdout)
        if not session_id:
            raise ForgeError(f"forge new returned no session id: {result.stdout!r}")
        return ForgeSession(self, session_id)

    def close_tag(self, tag: str) -> None:
        self.run("close-tag", tag, check=False)


def _parse_session_id(stdout: str) -> str:
    """``forge new`` prints the bare id (or a small JSON envelope)."""
    text = stdout.strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text.splitlines()[-1].strip()
        for key in ("sessionId", "session_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    return text.splitlines()[-1].strip()
