"""Prompt-injection shape detector for untrusted tool output (issue #100).

Pure, offline text policy: imports neither amplifier-core nor Textual, does no
I/O and touches no network. It lives in ``model/`` (ADR-0007 layering) because
it is deterministic policy over strings with zero kernel dependencies;
``kernel/governance_hook.py`` wires it onto ``tool:post`` / ``tool:error`` and
turns a positive verdict into a data-only context note.

Tool output (``web_fetch`` bodies, file reads, ``bash`` stdout) is *untrusted*:
it can carry text SHAPED like instructions to the model. This scanner flags
five such shapes so a downstream system note can tell the model to treat the
flagged output as data, never as instructions:

- ``authority-override`` -- "ignore previous instructions", "disregard the
  system prompt".
- ``role-impersonation`` -- spoofed role markers like ``<system>`` or a
  ``System:`` / "developer message:" preamble.
- ``secret-extraction`` -- "reveal your system prompt", "print the API key".
- ``concealed-action`` -- "do not tell the user", "without informing the user".
- ``tool-directive`` -- "run the following command", "execute this tool".

Two invariants make it safe to run on every tool result:

- **Flag, never block.** Detection is advisory. Legitimate content (docs,
  security articles, this very module's tests) routinely quotes these phrases,
  so the safeguard annotates rather than denies -- the trust gate on
  ``tool:pre`` owns blocking, not this.
- **Fail-safe.** Malformed, huge or non-string input yields "no findings",
  never an exception; :func:`scan_for_injection` accepts ``object`` and
  swallows any internal error so a weird payload can never break a tool turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_MAX_SCAN_CHARS = 262_144
"""Only the first 256 KiB of a tool result is scanned -- bounded, offline work."""

_MAX_FINDINGS = 16
"""Stop after this many matches; a note only needs to name each shape once."""

_EXCERPT_RADIUS = 32
"""Characters of surrounding context kept on each side of a match."""

_MAX_EXCERPT_CHARS = 160
"""Hard cap on a single excerpt so a note stays bounded regardless of input."""


class InjectionShape(str, Enum):
    """The five injection-shaped text patterns flagged in tool output."""

    AUTHORITY_OVERRIDE = "authority-override"
    ROLE_IMPERSONATION = "role-impersonation"
    SECRET_EXTRACTION = "secret-extraction"
    CONCEALED_ACTION = "concealed-action"
    TOOL_DIRECTIVE = "tool-directive"


# Ordered so findings read shape-by-shape deterministically. Each pattern is
# deliberately narrow: it matches instruction-SHAPED phrasing, not every
# mention of a keyword, keeping benign prose (which names these ideas without
# commanding them) out of the results.
_PATTERNS: tuple[tuple[InjectionShape, re.Pattern[str]], ...] = (
    (
        # "ignore previous instructions", "disregard the above system prompt",
        # "forget all prior directions".
        InjectionShape.AUTHORITY_OVERRIDE,
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
            r"\b(?:previous|prior|earlier|above|preceding|all|any|your|the|these|those)\b"
            r"[^.\n]{0,40}?"
            r"\b(?:instruction|prompt|message|direction|rule|command|guideline|"
            r"policy|constraint)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        # Spoofed role markers: "<system>", a "System:" line preamble, or
        # "developer message:" / "system prompt:" framing.
        InjectionShape.ROLE_IMPERSONATION,
        re.compile(
            r"(?:<\s*/?\s*(?:system|developer|assistant)\b"
            r"|^\s*(?:system|developer|assistant)\s*:"
            r"|\b(?:system|developer)\s+(?:message|prompt|instruction)s?\s*:)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # "reveal your system prompt", "print the api key", "exfiltrate the
        # credentials" -- an extraction verb near a secret noun.
        InjectionShape.SECRET_EXTRACTION,
        re.compile(
            r"\b(?:reveal|print|show|share|send|leak|exfiltrate|disclose|repeat|"
            r"output|dump|expose|email|upload|forward)\b.{0,80}?"
            r"\b(?:system\s+prompt|secret|password|passphrase|credential|"
            r"api[\s_-]?key|access[\s_-]?token|auth(?:orization)?\s+token|"
            r"private\s+key|ssh\s+key|token)s?\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        # "do not tell the user", "without informing the user", "never mention
        # this to the user".
        InjectionShape.CONCEALED_ACTION,
        re.compile(
            r"\b(?:do\s+not|don'?t|never|without|avoid)\s+(?:ever\s+)?"
            r"(?:tell|telling|inform|informing|notify|notifying|mention|"
            r"mentioning|alert|alerting|warn|warning|report|reporting|show|"
            r"showing)\s+(?:this\s+)?(?:to\s+)?(?:the\s+)?"
            r"(?:user|human|operator|person)\b",
            re.IGNORECASE,
        ),
    ),
    (
        # "run the following command", "execute this tool", "invoke the bash
        # tool" -- a directive to act, not a description of one.
        InjectionShape.TOOL_DIRECTIVE,
        re.compile(
            r"\b(?:run|execute|invoke|call|issue)\s+"
            r"(?:the|this|these|those|a|an|following|below|next)\s+"
            r"(?:[a-z0-9._-]+\s+){0,4}?"
            r"(?:tool|command|shell\s+command|function|script|bash|curl|"
            r"subprocess)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class InjectionFinding:
    """One injection-shaped match: which shape, and a bounded text excerpt."""

    shape: InjectionShape
    excerpt: str


@dataclass(frozen=True, slots=True)
class InjectionReport:
    """Verdict for one scanned text: whether flagged, plus ordered findings."""

    flagged: bool
    findings: tuple[InjectionFinding, ...]

    @property
    def shapes(self) -> tuple[InjectionShape, ...]:
        """Distinct shapes present, in first-seen (pattern) order."""
        ordered: list[InjectionShape] = []
        for finding in self.findings:
            if finding.shape not in ordered:
                ordered.append(finding.shape)
        return tuple(ordered)


_CLEAN = InjectionReport(flagged=False, findings=())


def scan_for_injection(text: object) -> InjectionReport:
    """Scan *text* for injection-shaped phrases; return a structured report.

    Deterministic and offline. Accepts ``object`` and never raises: non-string
    input is coerced (bytes decoded, everything else ``str()``-ed), scanning is
    bounded to the first 256 KiB, and any internal error degrades to the
    findings gathered so far -- a pathological payload can never break the
    caller's tool turn.
    """
    content = _coerce(text)
    if not content:
        return _CLEAN
    findings: list[InjectionFinding] = []
    for shape, pattern in _PATTERNS:
        try:
            for match in pattern.finditer(content):
                findings.append(
                    InjectionFinding(shape, _excerpt(content, match.start(), match.end()))
                )
                if len(findings) >= _MAX_FINDINGS:
                    return InjectionReport(flagged=True, findings=tuple(findings))
        except Exception:  # noqa: BLE001 — fail-safe: one bad pattern/input must never break detection
            continue
    return InjectionReport(flagged=bool(findings), findings=tuple(findings))


def _coerce(text: object) -> str:
    try:
        if isinstance(text, str):
            content = text
        elif isinstance(text, (bytes, bytearray, memoryview)):
            content = bytes(text).decode("utf-8", "replace")
        else:
            content = str(text)
    except Exception:  # noqa: BLE001 — a hostile __str__ must not break detection
        return ""
    return content[:_MAX_SCAN_CHARS]


def _excerpt(content: str, start: int, end: int) -> str:
    left = max(0, start - _EXCERPT_RADIUS)
    right = min(len(content), end + _EXCERPT_RADIUS)
    return " ".join(content[left:right].split())[:_MAX_EXCERPT_CHARS]


__all__ = [
    "InjectionFinding",
    "InjectionReport",
    "InjectionShape",
    "scan_for_injection",
]
