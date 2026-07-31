"""Portable session export / import round-trip (``kernel.session_transfer``).

Everything runs against a tmp-dir :class:`SessionStore`; nothing touches the
developer's real ``~/.amplifier``. These pin the honest round-trip contract:
full export restores content, sanitized export ships placeholders, and import
mints a fresh listed session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_tui.kernel import session_transfer as st
from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.model.redaction import REDACTION_PLACEHOLDER
from amplifier_app_tui.model.sanitize import TOOL_IO_PLACEHOLDER, USER_PLACEHOLDER

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def _seed(store: SessionStore, session_id: str = "src000001") -> None:
    transcript = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi back"},
        {"role": "tool", "tool_call_id": "t1", "content": "TOOLBODY output"},
    ]
    store.save(
        session_id, transcript, {"session_id": session_id, "bundle": "tui", "name": "src"}
    )


# -- export -----------------------------------------------------------------


def test_export_schema_and_payload(store: SessionStore) -> None:
    _seed(store)
    payload = st.export_session(store, "src000001")
    assert payload["schema"] == st.SCHEMA
    assert payload["sanitized"] is False
    assert payload["session_id"] == "src000001"
    assert isinstance(payload["transcript"], list) and len(payload["transcript"]) == 3


def test_export_missing_session_raises(store: SessionStore) -> None:
    with pytest.raises(FileNotFoundError):
        st.export_session(store, "nope")


def test_export_sanitize_redacts_paths_and_secrets(store: SessionStore) -> None:
    store.save(
        "s1",
        [{"role": "user", "content": f"open /Users/alice/x key {AWS_KEY}"}],
        {"session_id": "s1", "working_dir": "/Users/alice/proj"},
    )
    payload = st.export_session(store, "s1", sanitize=True, users=("alice",))
    body = payload["transcript"][0]["content"]
    assert "alice" not in body
    assert USER_PLACEHOLDER in body
    assert AWS_KEY not in body and REDACTION_PLACEHOLDER in body
    assert payload["metadata"]["working_dir"] == "/Users/[user]/proj"
    assert payload["sanitized"] is True


def test_export_tool_io_flag_implies_sanitize_and_blanks_tools(store: SessionStore) -> None:
    _seed(store)
    payload = st.export_session(store, "src000001", redact_tool_io=True)
    assert payload["sanitized"] is True and payload["tool_io_redacted"] is True
    tool_msg = next(m for m in payload["transcript"] if m.get("role") == "tool")
    assert tool_msg["content"] == TOOL_IO_PLACEHOLDER


# -- import -----------------------------------------------------------------


def test_import_round_trip_is_listed_and_restores_content(store: SessionStore) -> None:
    _seed(store)
    payload = st.export_session(store, "src000001")
    new_id = st.import_session(store, payload)
    assert new_id != "src000001"
    assert new_id in store.list_sessions()
    transcript, metadata = store.load(new_id)
    assert [m["role"] for m in transcript] == ["user", "assistant", "tool"]
    assert metadata["imported_from"] == "src000001"
    assert metadata["source_schema"] == st.SCHEMA
    assert metadata["session_id"] == new_id


def test_import_name_override(store: SessionStore) -> None:
    _seed(store)
    payload = st.export_session(store, "src000001")
    new_id = st.import_session(store, payload, name="shared copy")
    assert store.get_metadata(new_id)["name"] == "shared copy"


def test_import_sanitized_artifact_keeps_placeholders(store: SessionStore) -> None:
    store.save("s1", [{"role": "user", "content": "open /Users/alice/x"}], {"session_id": "s1"})
    payload = st.export_session(store, "s1", sanitize=True, users=("alice",))
    new_id = st.import_session(store, payload)
    transcript, _ = store.load(new_id)
    assert transcript[0]["content"] == "open /Users/[user]/x"


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"schema": "someone-else/v1", "transcript": []},
        {"schema": st.SCHEMA},  # missing transcript
        {"schema": st.SCHEMA, "transcript": "nope"},
    ],
)
def test_import_rejects_malformed(store: SessionStore, payload: object) -> None:
    with pytest.raises(st.SessionTransferError):
        st.import_session(store, payload)


# -- read_export_file / dumps ----------------------------------------------


def test_read_export_file_errors(tmp_path: Path) -> None:
    with pytest.raises(st.SessionTransferError):
        st.read_export_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(st.SessionTransferError):
        st.read_export_file(bad)


def test_dumps_round_trips_through_read(tmp_path: Path, store: SessionStore) -> None:
    _seed(store)
    payload = st.export_session(store, "src000001")
    path = tmp_path / "export.json"
    path.write_text(st.dumps(payload), encoding="utf-8")
    assert st.read_export_file(path)["schema"] == st.SCHEMA
