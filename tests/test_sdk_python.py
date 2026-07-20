from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SDK_SRC = Path(__file__).parents[1] / "sdk" / "python" / "src"
sys.path.insert(0, str(SDK_SRC))

from amplifier_newtui_sdk import (  # noqa: E402
    AmplifierClient,
    AmplifierRunError,
    ProtocolError,
)


def _fake_cli(tmp_path: Path, records: list[dict[str, object]], *, exit_code: int = 0) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "print('diagnostic', file=sys.stderr)\n"
        f"records = {records!r}\n"
        "for record in records:\n"
        "    if record.get('type') == 'turn.completed':\n"
        "        record['response'] = prompt\n"
        "    print(json.dumps(record), flush=True)\n"
        f"raise SystemExit({exit_code})\n"
    )
    return script


def _records() -> list[dict[str, object]]:
    return [
        {
            "schema_version": 1,
            "sequence": 1,
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session.started",
            "session_id": "s1",
            "bundle": "newtui",
            "model": "test-model",
        },
        {
            "schema_version": 1,
            "sequence": 2,
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "runtime.event",
            "event": {"kind": "prompt_submit", "session_id": "s1"},
        },
        {
            "schema_version": 1,
            "sequence": 3,
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "turn.completed",
            "session_id": "s1",
            "response": "replaced by prompt",
            "duration_ms": 12.5,
        },
    ]


def test_python_sdk_runs_cli_over_stdin_and_collects_events(tmp_path: Path) -> None:
    script = _fake_cli(tmp_path, _records())
    client = AmplifierClient((sys.executable, str(script)), cwd=tmp_path)
    result = client.run("private prompt\nwith newline")
    assert result.response == "private prompt\nwith newline"
    assert result.session_id == "s1"
    assert result.bundle == "newtui"
    assert result.model == "test-model"
    assert [event["event"]["kind"] for event in result.events] == ["prompt_submit"]


def test_python_sdk_rejects_sequence_drift(tmp_path: Path) -> None:
    records = _records()
    records[1]["sequence"] = 9
    script = _fake_cli(tmp_path, records)
    with pytest.raises(ProtocolError, match="expected JSONL sequence 2"):
        list(AmplifierClient((sys.executable, str(script))).stream("hi"))


def test_python_sdk_raises_structured_run_error(tmp_path: Path) -> None:
    record = {
        "schema_version": 1,
        "sequence": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "error",
        "session_id": "",
        "error": "offline",
        "error_type": "RuntimeError",
        "duration_ms": 1,
    }
    script = _fake_cli(tmp_path, [record], exit_code=1)
    with pytest.raises(AmplifierRunError) as caught:
        AmplifierClient((sys.executable, str(script))).run("hi")
    assert caught.value.record["error"] == "offline"


def test_python_sdk_rejects_record_after_terminal(tmp_path: Path) -> None:
    records = _records()
    records.append(
        {
            "schema_version": 1,
            "sequence": 4,
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "runtime.event",
            "event": {"kind": "late"},
        }
    )
    script = _fake_cli(tmp_path, records)
    with pytest.raises(ProtocolError, match="after terminal"):
        list(AmplifierClient((sys.executable, str(script))).stream("hi"))


def test_python_sdk_records_are_json_serializable(tmp_path: Path) -> None:
    script = _fake_cli(tmp_path, _records())
    records = list(AmplifierClient((sys.executable, str(script))).stream("hi"))
    assert json.loads(json.dumps(records))[-1]["type"] == "turn.completed"
