"""External-editor compose: open ``$VISUAL``/``$EDITOR`` on a temp markdown
file seeded with the composer draft, then read it back (normalized).

Behavioral port of opencode ``packages/tui/src/editor.ts`` (``openEditor`` /
``normalizePromptContent``) -- NO opencode source is imported, vendored, or
copied; this is a from-scratch reimplementation of the observed contract
(see ``.ai/oc_donor.md``).

Kernel layer per ADR-0007: pure logic + stdlib subprocess I/O, and it never
imports Textual (the UI owns ``App.suspend``; this module owns the file
dance). The editor invocation is injected as a ``runner`` callback so the
temp-file seeding / read-back / normalization / cleanup stays unit-testable
with no real editor and no terminal -- the UI supplies a runner that
suspends the TUI around a real subprocess; tests inject a fake.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

EditorRunner = Callable[[list[str], "str | None"], int]
"""Run the editor argv in ``cwd`` (process cwd when ``None``); return its exit
code. Raise ``OSError`` when the editor binary cannot be spawned."""

EditorStatus = Literal["ok", "no_editor", "empty", "exit_error", "spawn_error"]


@dataclass(frozen=True, slots=True)
class EditorOutcome:
    """The result of a compose attempt (a tagged union over ``status``).

    - ``ok``: editor exited 0 and the file was non-empty; ``text`` holds the
      normalized content the composer adopts.
    - ``no_editor`` / ``empty`` / ``exit_error`` / ``spawn_error``: the draft
      is left untouched; ``detail`` explains why (surfaced as a notice).
    """

    status: EditorStatus
    text: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def resolve_editor(environ: Mapping[str, str] | None = None) -> str | None:
    """``$VISUAL`` then ``$EDITOR``; ``None`` if neither is set.

    Matches the donor exactly -- there is NO hardcoded ``vi``/``nano``
    fallback. The caller turns ``None`` into a user-facing notice rather than
    a silent no-op.
    """
    env = os.environ if environ is None else environ
    for name in ("VISUAL", "EDITOR"):
        value = env.get(name)
        if value and value.strip():
            return value
    return None


def normalize_prompt_content(content: str) -> str:
    """Strip exactly one trailing newline (CRLF or LF) IFF the remaining body
    is a single line; multi-line content keeps its trailing newline.

    Byte-for-byte port of the donor ``normalizePromptContent`` -- kills the
    newline an editor auto-appends to a one-line prompt while preserving
    intentional multi-line structure (see ``.ai/oc_donor.md`` for vectors).
    """
    if content.endswith("\r\n"):
        body = content[:-2]
        return body if ("\n" not in body and "\r" not in body) else content
    if content.endswith("\n"):
        body = content[:-1]
        return body if ("\n" not in body and "\r" not in body) else content
    return content


def compose_in_editor(
    draft: str,
    *,
    runner: EditorRunner,
    environ: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> EditorOutcome:
    """Seed a temp ``.md`` file with *draft*, run the editor via *runner*, and
    read the file back normalized.

    Pure orchestration: no terminal, no Textual. The temp file is ALWAYS
    removed (donor ``finally``). See ``.ai/oc_donor.md`` for the contract and
    the deliberate ``mkstemp`` deviation from the donor's ``Date.now()`` name.
    """
    editor = resolve_editor(environ)
    if editor is None:
        return EditorOutcome("no_editor", detail="set $VISUAL or $EDITOR to compose externally")
    run_cwd = cwd if cwd and os.path.isdir(cwd) else None
    fd, path = tempfile.mkstemp(suffix=".md", prefix="amplifier-compose-")
    try:
        # newline="" -> no translation: the draft is seeded byte-for-byte.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(draft)
        argv = [*shlex.split(editor), path]
        try:
            code = runner(argv, run_cwd)
        except OSError as error:  # editor binary missing / not executable
            return EditorOutcome("spawn_error", detail=str(error))
        if code != 0:
            return EditorOutcome("exit_error", detail=f"editor exited with code {code}")
        # newline="" on read too -> a real CRLF survives to normalize().
        with open(path, encoding="utf-8", newline="") as handle:
            content = handle.read()
        if not content:  # donor: empty read-back is falsy -> "no content"
            return EditorOutcome("empty")
        return EditorOutcome("ok", text=normalize_prompt_content(content))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


__all__ = [
    "EditorOutcome",
    "EditorRunner",
    "EditorStatus",
    "compose_in_editor",
    "normalize_prompt_content",
    "resolve_editor",
]
