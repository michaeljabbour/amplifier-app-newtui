"""The one public home for token-count display formatting.

Two DISTINCT display contracts live here, pinned by different tests for
different surfaces. They are deliberately NOT merged -- each serves a
different part of the UI and renders the same count differently:

- :func:`format_tokens_k` -- fixed one-decimal thousands (``0.0k`` /
  ``3.2k`` / ``1200.0k``). The turn-telemetry / lanes / demo-mockup
  surface: always ``(tokens/1000).1f + "k"``, sub-1k counts included,
  never switches to ``m`` units.
- :func:`format_tokens_compact` -- compact human count (``742`` /
  ``4.1k`` / ``52k`` / ``1.2m``). The ``/context`` and ``/doctor``
  surface: bare integer under 1k, adaptive-decimal ``k``, ``m`` above a
  million.

Pure arithmetic -- no imports, no side effects -- so it sits cleanly at
the bottom of the ADR-0007 layering (imports neither Textual nor
amplifier-core) and every layer above can share it.
"""

from __future__ import annotations


def format_tokens_k(tokens: int) -> str:
    """Fixed one-decimal thousands: ``0.0k`` / ``3.2k`` / ``1200.0k``.

    The turn-telemetry surface (``TurnTelemetry`` suffix/label, the
    lanes-panel down-arrow ``X.Xk tokens`` figure, and the demo mockup's
    rule labels). Always ``(tokens/1000).toFixed(1) + "k"`` per the
    mockup -- sub-1k counts are shown (``0.0k`` at turn start) and it
    never switches to ``m`` units, so 1.2M tokens reads ``1200.0k``.
    """
    return f"{tokens / 1_000:.1f}k"


def format_tokens_compact(tokens: int) -> str:
    """Compact human count: ``742`` / ``4.1k`` / ``52k`` / ``1.2m``.

    The ``/context`` and ``/doctor`` surface. Bare integer below 1k;
    ``k`` above that with a decimal only when it adds information
    (``4.1k`` but ``8k``); ``m`` above a million.
    """
    if tokens < 1_000:
        return str(tokens)
    if tokens < 1_000_000:
        thousands = tokens / 1_000
        if thousands < 10 and round(thousands, 1) != round(thousands):
            return f"{thousands:.1f}k"
        return f"{round(thousands)}k"
    return f"{tokens / 1_000_000:.1f}m"


DIGEST_MAX_CHARS = 48
"""Hard cap for :func:`command_digest` output. Chosen so the full blocked
line (``  ⊘ blocked · <digest> · needs your ok — ctrl+y to review``)
stays inside the transcript's 100-cell reading measure."""

_HEREDOC_MARKER = "<<"
_QUOTES = "'\""


def _truncate_chars(text: str, width: int) -> str:
    """``text`` unchanged when it fits, else hard-cut with a ``…``."""
    return text if len(text) <= width else f"{text[: width - 1]}…"


def _redirect_target(tokens: list[str]) -> str:
    """The path a ``>`` / ``>>`` redirection writes, or ``""``.

    Whitespace-token based on purpose (matches the reducer's shell-command
    handling): both the spaced form (``> path``) and the attached form
    (``>path`` / ``>>path``) are recognized; ``2>`` etc. are not writes we
    can name a target for and fall through.
    """
    for index, token in enumerate(tokens):
        if token in (">", ">>"):
            if index + 1 < len(tokens):
                return tokens[index + 1].strip(_QUOTES)
        elif token.startswith(">>") and len(token) > 2:
            return token[2:].strip(_QUOTES)
        elif token.startswith(">") and len(token) > 1 and not token.startswith(">>"):
            return token[1:].strip(_QUOTES)
    return ""


def command_digest(command: str, width: int = DIGEST_MAX_CHARS) -> str:
    """Verb-noun digest of a (possibly multi-line) shell command.

    The blocked-line / needs-you display summary (deferred-decision UX):
    a raw heredoc sprawling across the row becomes
    ``write /tmp/diag/build2.py (heredoc, 14 lines)``. Deterministic and
    dependency-free so the Python and Rust apps render byte-identical
    digests:

    - heredoc (``<<TAG`` on the first line): ``write <redirect target>
      (heredoc, N lines)`` — N is the body line count between the marker
      line and the closing tag; without a redirect target the command's
      first word stands in for ``write <target>``.
    - other multi-line commands: first line + `` (+N lines)``.
    - single line with a ``>`` / ``>>`` redirect: ``write <target>``.
    - anything else: the whitespace-collapsed first line.

    Every shape is hard-truncated at *width* characters.
    """
    lines = [line for line in str(command).splitlines() if line.strip()]
    if not lines:
        return "(command)"
    first = " ".join(lines[0].split())
    tokens = first.split()
    if _HEREDOC_MARKER in first:
        target = _redirect_target(tokens)
        head = f"write {target}" if target else (tokens[0] if tokens else "(command)")
        if len(lines) > 1:
            body_lines = max(len(lines) - 2, 0)
            plural = "s" if body_lines != 1 else ""
            return _truncate_chars(f"{head} (heredoc, {body_lines} line{plural})", width)
        # Whitespace-collapsed heredoc (queue-sanitized actions lose their
        # newlines): the body length is unknowable — say so honestly.
        return _truncate_chars(f"{head} (heredoc)", width)
    if len(lines) > 1:
        suffix = f" (+{len(lines) - 1} lines)"
        return _truncate_chars(_truncate_chars(first, max(width - len(suffix), 1)) + suffix, width)
    target = _redirect_target(tokens)
    if target:
        return _truncate_chars(f"write {target}", width)
    return _truncate_chars(first, width)


__all__ = ["DIGEST_MAX_CHARS", "command_digest", "format_tokens_compact", "format_tokens_k"]
