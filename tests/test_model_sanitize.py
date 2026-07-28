"""Broader export sanitization (``model.sanitize``).

Table-driven pins for the path + tool-IO redaction the opt-in ``--sanitize``
export mode adds on top of the always-on secret scrub. These are the donor's
wider sanitization scope re-expressed natively; the sinks that reuse them
(``kernel.session_transfer``) trust these rules.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.model.redaction import REDACTION_PLACEHOLDER
from amplifier_app_newtui.model.sanitize import (
    TOOL_IO_PLACEHOLDER,
    USER_PLACEHOLDER,
    redact_home_paths,
    redact_tool_io,
    sanitize_metadata,
    sanitize_transcript,
    sanitize_value,
)

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


# -- redact_home_paths ------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/Users/alice/src/app.py", "/Users/[user]/src/app.py"),
        ("/home/bob/notes.txt", "/home/[user]/notes.txt"),
        (r"C:\Users\carol\file", r"C:\Users\[user]\file"),
        ("see /Users/alice/a and /home/bob/b", "see /Users/[user]/a and /home/[user]/b"),
        ("no path here", "no path here"),
        ("", ""),
    ],
)
def test_redact_home_paths_table(text: str, expected: str) -> None:
    assert redact_home_paths(text) == expected


def test_redact_home_paths_is_idempotent() -> None:
    once = redact_home_paths("/Users/alice/x")
    assert redact_home_paths(once) == once == "/Users/[user]/x"


def test_redact_home_paths_extra_users_word_bounded() -> None:
    # A supplied username is scrubbed whole-word (catches emails/URLs)...
    assert redact_home_paths("hi alice ok", users=("alice",)) == f"hi {USER_PLACEHOLDER} ok"
    # ...but never as a substring of a longer token.
    assert redact_home_paths("alicefoo", users=("alice",)) == "alicefoo"


def test_redact_home_paths_username_with_dot() -> None:
    # Real usernames like ``jane.doe`` are one segment (stops at ``/``).
    assert redact_home_paths("/Users/jane.doe/x") == "/Users/[user]/x"


# -- sanitize_value (path redaction + secret scrub, recursive) --------------


def test_sanitize_value_combines_path_and_secret_redaction() -> None:
    value = {
        "path": "/Users/alice/creds",
        "blob": f"key {AWS_KEY} end",
        "nested": ["/home/bob/x", 7, None, True],
    }
    out = sanitize_value(value)
    assert out["path"] == "/Users/[user]/creds"
    assert AWS_KEY not in out["blob"]
    assert REDACTION_PLACEHOLDER in out["blob"]
    assert out["nested"] == ["/home/[user]/x", 7, None, True]


def test_sanitize_value_leaves_non_strings_and_keys() -> None:
    # Dict keys are not rewritten (key-based redaction owns those elsewhere).
    assert sanitize_value({"/Users/alice": 1}) == {"/Users/alice": 1}
    assert sanitize_value(42) == 42


# -- redact_tool_io ---------------------------------------------------------


def test_redact_tool_io_openai_tool_calls() -> None:
    msg = {
        "role": "assistant",
        "content": "calling",
        "tool_calls": [
            {"id": "c1", "function": {"name": "read", "arguments": '{"file":"/Users/alice/x"}'}}
        ],
    }
    out = redact_tool_io(msg)
    assert out["tool_calls"][0]["function"]["arguments"] == TOOL_IO_PLACEHOLDER
    assert out["tool_calls"][0]["function"]["name"] == "read"  # name kept
    assert msg["tool_calls"][0]["function"]["arguments"] != TOOL_IO_PLACEHOLDER  # input unmutated


def test_redact_tool_io_anthropic_blocks() -> None:
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "reading the file"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {"file": "secret"}},
        ],
    }
    out = redact_tool_io(msg)
    assert out["content"][0] == {"type": "text", "text": "reading the file"}  # prose kept
    assert out["content"][1]["input"] == TOOL_IO_PLACEHOLDER


def test_redact_tool_io_tool_result_and_tool_role() -> None:
    result_block = {"role": "user", "content": [{"type": "tool_result", "content": "BIG OUTPUT"}]}
    assert redact_tool_io(result_block)["content"][0]["content"] == TOOL_IO_PLACEHOLDER
    tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "BIG OUTPUT"}
    assert redact_tool_io(tool_msg)["content"] == TOOL_IO_PLACEHOLDER


def test_redact_tool_io_passthrough_for_plain_messages() -> None:
    msg = {"role": "user", "content": "just a prompt"}
    assert redact_tool_io(msg) == msg
    assert redact_tool_io("not a dict") == "not a dict"


# -- sanitize_transcript / sanitize_metadata --------------------------------


def test_sanitize_transcript_paths_only_by_default() -> None:
    messages = [
        {"role": "user", "content": "open /Users/alice/x"},
        {"role": "tool", "tool_call_id": "t", "content": "TOOLBODY"},
    ]
    out = sanitize_transcript(messages)
    assert out[0]["content"] == "open /Users/[user]/x"
    assert out[1]["content"] == "TOOLBODY"  # tool-IO preserved unless opted in


def test_sanitize_transcript_with_tool_io() -> None:
    messages = [
        {"role": "user", "content": "open /Users/alice/x"},
        {"role": "tool", "tool_call_id": "t", "content": "TOOLBODY"},
    ]
    out = sanitize_transcript(messages, redact_tool_io=True, users=("alice",))
    assert out[0]["content"] == "open /Users/[user]/x"
    assert out[1]["content"] == TOOL_IO_PLACEHOLDER


def test_sanitize_metadata_redacts_working_dir() -> None:
    out = sanitize_metadata({"working_dir": "/Users/alice/proj", "bundle": "newtui"})
    assert out["working_dir"] == "/Users/[user]/proj"
    assert out["bundle"] == "newtui"
