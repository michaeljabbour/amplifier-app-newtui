"""Single-shot stdin and structured output contract."""

from __future__ import annotations

import asyncio
import json

from click.testing import CliRunner

from amplifier_app_newtui.kernel.events import PromptSubmit
from amplifier_app_newtui.main import main


class FakeRuntime:
    last_prompt = ""

    def __init__(self, *, bundle=None) -> None:
        self.bundle_name = bundle or "fake"
        self.model_name = "fake-model"
        self.session_id = "session-full-id"
        self.queue = asyncio.Queue()

    async def start(self) -> None:
        print("module startup diagnostic")

    async def submit(self, prompt: str) -> str:
        type(self).last_prompt = prompt
        self.queue.put_nowait(PromptSubmit(session_id=self.session_id, prompt=prompt))
        return "fake response"

    async def cleanup(self) -> None:
        return None


class FailingRuntime(FakeRuntime):
    async def start(self) -> None:
        print("diagnostic before failure")
        raise RuntimeError("offline setup failed")


def test_run_reads_stdin_and_prints_text(monkeypatch) -> None:
    monkeypatch.setattr("amplifier_app_newtui.kernel.runtime.RealRuntime", FakeRuntime)
    result = CliRunner().invoke(main, ["run"], input="piped prompt\n")
    assert result.exit_code == 0
    assert result.stdout.endswith("fake response\n")
    assert FakeRuntime.last_prompt == "piped prompt\n"


def test_json_stdout_is_one_parseable_document(monkeypatch) -> None:
    monkeypatch.setattr("amplifier_app_newtui.kernel.runtime.RealRuntime", FakeRuntime)
    result = CliRunner().invoke(
        main, ["run", "--output-format", "json"], input="piped prompt\n"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "success",
        "response": "fake response",
        "session_id": "session-full-id",
        "bundle": "fake",
        "model": "fake-model",
        "timestamp": payload["timestamp"],
    }
    assert "module startup diagnostic" not in result.stdout
    assert "module startup diagnostic" in result.stderr


def test_json_trace_contains_normalized_events(monkeypatch) -> None:
    monkeypatch.setattr("amplifier_app_newtui.kernel.runtime.RealRuntime", FakeRuntime)
    result = CliRunner().invoke(
        main,
        ["run", "prompt arg", "--output-format", "json-trace"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["execution_trace"][0]["kind"] == "prompt_submit"
    assert payload["metadata"]["event_count"] == 1


def test_json_failure_is_still_one_parseable_document(monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_app_newtui.kernel.runtime.RealRuntime", FailingRuntime
    )
    result = CliRunner().invoke(
        main, ["run", "prompt arg", "--output-format", "json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"] == "offline setup failed"
    assert payload["error_type"] == "RuntimeError"
    assert "diagnostic before failure" not in result.stdout
    assert "diagnostic before failure" in result.stderr


def test_run_without_prompt_or_pipe_is_usage_error() -> None:
    result = CliRunner().invoke(main, ["run"], input="")
    assert result.exit_code == 2
    assert "Prompt required" in result.stderr
