"""Shared secret-scrubbing rules for the persistence sinks.

One home for the value-pattern redaction that every persistence sink must
apply. ADR-0007 forbids ``commands/`` from importing amplifier-core, so the
rules cannot live behind that import; instead the pattern set lives here in
``model/`` (stdlib only) where both ``kernel/`` (transcript + metadata) and
``commands/`` (``/export`` + ``/copy``) can share the *same* definition
rather than fork four copies.

Two complementary layers cover secrets on disk / clipboard:

- **Key-based** redaction (amplifier-core's ``redact_secrets``) scrubs
  structured *metadata* by sensitive KEY name. It is kernel-only and stays in
  ``kernel/persistence.py``.
- **Value-pattern** redaction (this module) scrubs secret-shaped *values*
  (AWS keys, bearer tokens, private-key blocks, provider tokens) out of free
  text — the transcript bodies, exported markdown and copied answers that key
  redaction never sees. The metadata path runs this too, so the two layers
  are shared, not forked.

Redaction is idempotent: the placeholder never matches a rule, so scrubbing
already-scrubbed text is a no-op (safe to re-run on resume/re-export).
"""

from __future__ import annotations

import re
from typing import Any

REDACTION_PLACEHOLDER = "[REDACTED]"
"""Marker written in place of a matched secret. Deliberately identical to
amplifier-core's key-based placeholder so metadata and free text read the
same on disk."""


# Each rule is ``(pattern, replacement)``; ``pattern.sub(replacement, text)``
# runs in order. Replacements that keep a non-secret prefix (auth scheme,
# assignment ``key =``) capture it so surrounding context survives the scrub.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private-key blocks (any label) — redact header..footer as a unit.
    (
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTION_PLACEHOLDER,
    ),
    # AWS access key IDs (AKIA/ASIA/… + 16 base32 chars), incl. the AWS docs
    # example key AKIAIOSFODNN7EXAMPLE.
    (
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])[A-Z0-9]{16}\b"),
        REDACTION_PLACEHOLDER,
    ),
    # AWS secret access keys are 40 base64 chars — too generic to match on
    # shape alone, so only when introduced by their canonical key name.
    (
        re.compile(r"(?i)(aws_secret_access_key\s*[:=]\s*)['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
        r"\1" + REDACTION_PLACEHOLDER,
    ),
    # GitHub tokens (PAT/OAuth/app/refresh + fine-grained pat).
    (re.compile(r"\bgh[posur]_[A-Za-z0-9]{36,}\b"), REDACTION_PLACEHOLDER),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), REDACTION_PLACEHOLDER),
    # Google API keys.
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b"), REDACTION_PLACEHOLDER),
    # Slack tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), REDACTION_PLACEHOLDER),
    # Bearer / Token auth credentials — keep the scheme, drop the credential.
    (
        re.compile(r"\b(Bearer|Token)\s+[A-Za-z0-9._~+/\-]{8,}=*", re.IGNORECASE),
        r"\1 " + REDACTION_PLACEHOLDER,
    ),
    # Labeled secret assignments: ``api_key = …``, ``password: …``,
    # ``client_secret=…`` — the catch-all for named credentials whose value
    # has no distinctive shape. Value must be >=6 non-space chars.
    (
        re.compile(
            r"(?im)^(?P<pre>[^\n:=]*"
            r"(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|credential)"
            r"[^\n:=]*\s*[:=]\s*)"
            r"['\"]?(?P<val>[^\s'\"]{6,})['\"]?[ \t]*$"
        ),
        r"\g<pre>" + REDACTION_PLACEHOLDER,
    ),
)


def scrub_text(text: str) -> str:
    """Return *text* with every secret-shaped substring replaced.

    Idempotent: the placeholder matches no rule, so re-scrubbing is a no-op.
    """
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def scrub_value(value: Any) -> Any:
    """Recursively scrub every string leaf of *value*.

    Walks dicts/lists/tuples (the shape of a sanitized transcript message or
    redacted metadata dict) and applies :func:`scrub_text` to each ``str``
    leaf. Non-string, non-container leaves pass through unchanged. Keys are
    left as-is — key-based redaction owns those.
    """
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {key: scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item) for item in value)
    return value


__all__ = ["REDACTION_PLACEHOLDER", "scrub_text", "scrub_value"]
