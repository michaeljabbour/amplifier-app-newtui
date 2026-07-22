"""Shared secret-scrubbing rules (``model.redaction``).

The single, stdlib-only home for the value-pattern redaction applied at the
transcript, ``/export``, ``/copy`` and metadata sinks (issue #23). These
tests pin the rule set so the sinks — which only re-use it — can trust it.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.model.redaction import (
    REDACTION_PLACEHOLDER,
    scrub_text,
    scrub_value,
)

# A fake AWS key + secret pair (the AWS docs example key — not a live secret).
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
BEARER = "Bearer eyJhbGciOi.J9pay.load-sig_nature123"


def test_aws_access_key_id_is_redacted() -> None:
    out = scrub_text(f"the key is {AWS_KEY} ok")
    assert AWS_KEY not in out
    assert out == f"the key is {REDACTION_PLACEHOLDER} ok"


def test_aws_secret_access_key_line_is_redacted() -> None:
    out = scrub_text(f"aws_secret_access_key = {AWS_SECRET}")
    assert AWS_SECRET not in out
    assert out == f"aws_secret_access_key = {REDACTION_PLACEHOLDER}"


def test_bearer_token_redacted_but_scheme_kept() -> None:
    out = scrub_text(f"Authorization: {BEARER}")
    assert "eyJhbGci" not in out
    assert out == f"Authorization: Bearer {REDACTION_PLACEHOLDER}"


@pytest.mark.parametrize(
    "secret",
    [
        # Fixtures are built by concatenation so repo secret scanners
        # (e.g. GitHub push protection) don't match the source literals.
        "ghp_" + "1234567890abcdefghij1234567890ABCDEF",
        "github_pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
        "AIzaSy" + "A1234567890abcdefghijklmnopqrstuvw",
        "xoxb-" + "1234567890-abcdefghijklmnop",
    ],
)
def test_provider_tokens_are_redacted(secret: str) -> None:
    out = scrub_text(f"token {secret} trailing")
    assert secret not in out
    assert REDACTION_PLACEHOLDER in out


def test_pem_private_key_block_is_redacted() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890\nabcdefgh\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = scrub_text(f"key:\n{pem}\ndone")
    assert "MIIEpAIBAAKCAQEA" not in out
    assert out == f"key:\n{REDACTION_PLACEHOLDER}\ndone"


def test_labeled_secret_assignment_is_redacted() -> None:
    out = scrub_text("api_key = sk-supersecretvalue123")
    assert "supersecret" not in out
    assert out == f"api_key = {REDACTION_PLACEHOLDER}"


def test_full_credentials_file_round_trips_redacted() -> None:
    creds = f"[default]\naws_access_key_id = {AWS_KEY}\naws_secret_access_key = {AWS_SECRET}\n"
    out = scrub_text(creds)
    assert AWS_KEY not in out
    assert AWS_SECRET not in out
    assert out.count(REDACTION_PLACEHOLDER) == 2


def test_benign_text_is_untouched() -> None:
    text = "Please fix the flaky test in app.py; the access_key parameter is fine."
    assert scrub_text(text) == text


def test_scrubbing_is_idempotent() -> None:
    once = scrub_text(f"key {AWS_KEY} and aws_secret_access_key = {AWS_SECRET}")
    assert scrub_text(once) == once


def test_scrub_value_recurses_nested_containers() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"my key is {AWS_KEY}"},
            {"type": "text", "text": "harmless"},
        ],
        "meta": ("tag", f"Bearer {AWS_KEY}"),
    }
    scrubbed = scrub_value(message)
    assert scrubbed["content"][0]["text"] == f"my key is {REDACTION_PLACEHOLDER}"
    assert scrubbed["content"][1]["text"] == "harmless"
    assert scrubbed["meta"] == ("tag", f"Bearer {REDACTION_PLACEHOLDER}")
    # role/keys preserved, non-str leaves pass through
    assert scrubbed["role"] == "user"


def test_scrub_value_passes_through_non_string_leaves() -> None:
    assert scrub_value({"n": 1, "ok": True, "x": None}) == {
        "n": 1,
        "ok": True,
        "x": None,
    }
