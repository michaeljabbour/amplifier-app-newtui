"""Offline integration: a REAL amplifier session driven by FAKE modules.

No API keys, no network. A real foundation lifecycle (``load_bundle`` →
``prepare`` → ``create_session``) runs against fake provider / context /
tool / orchestrator modules written to a temp dir and referenced by a
temp bundle via ``file://`` sources. One turn is driven end-to-end
through :class:`~amplifier_app_newtui.kernel.runtime.RealRuntime`'s
queue bridge and the normalized UIEvents are asserted:

- Channel A stream deltas (``llm:stream_block_*`` → ``stream_block_*``)
- Channel B tool records (``tool:pre/post`` → ``tool_pre/tool_post``)
- the governance ``ask_user`` approval path through the REAL Rust
  ``process_hook_result`` → ``ApprovalBroker.request_approval`` with the
  verbatim ``Allow once / Allow always / Deny`` options
- steering injection at the ``provider:request`` step boundary
- ``orchestrator:complete`` arrives normalized
- persistence side effects (transcript.jsonl / metadata.json /
  ui-events.jsonl) under the fake HOME.

The fake orchestrator mirrors amplifier-module-loop-streaming's hook
surface: it emits the same events and routes every aggregated HookResult
through ``coordinator.process_hook_result`` — so approvals, denials and
context injections exercise the real engine paths.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.approval import ALLOW_ONCE, DENY, STANDARD_OPTIONS
from amplifier_app_newtui.kernel.runtime import RealRuntime


# --------------------------------------------------------------------------
# Fake module + bundle workspace (written once per test session)
# --------------------------------------------------------------------------

_PROVIDER_MODULE = '''
"""Fake provider module (offline integration tests)."""


class FakeProvider:
    name = "fake"

    def __init__(self, config):
        self.config = dict(config or {})

    def get_info(self):
        from amplifier_core import ProviderInfo

        return ProviderInfo(id="fake", display_name="Fake Provider")

    async def list_models(self):
        from amplifier_core import ModelInfo

        return [ModelInfo(id="fake-model", display_name="Fake Model")]

    async def complete(self, request=None, **kwargs):
        return {
            "content": "Hello from the fake provider.",
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "model": "fake-model",
        }

    def parse_tool_calls(self, response):
        return []


async def mount(coordinator, config=None):
    await coordinator.mount("providers", FakeProvider(config), name="fake")
    return None
'''

_CONTEXT_MODULE = '''
"""Fake context-manager module (offline integration tests)."""


class FakeContext:
    def __init__(self, config):
        self.config = dict(config or {})
        self._messages = []

    async def add_message(self, message):
        self._messages.append(dict(message))

    async def get_messages(self):
        return list(self._messages)

    async def set_messages(self, messages):
        self._messages = [dict(m) for m in messages]

    async def get_messages_for_request(self):
        return list(self._messages)

    async def clear(self):
        self._messages = []


async def mount(coordinator, config=None):
    await coordinator.mount("context", FakeContext(config))
    return None
'''

_TOOL_MODULE = '''
"""Fake write_file tool module (offline integration tests)."""


class FakeWriteTool:
    name = "write_file"
    description = "Write a file (fake, records calls)."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path"],
    }

    def __init__(self, config):
        self.config = dict(config or {})

    async def execute(self, tool_input):
        return {"success": True, "output": f"wrote {tool_input.get('file_path', '')}"}


async def mount(coordinator, config=None):
    tool = FakeWriteTool(config)
    await coordinator.mount("tools", tool, name=tool.name)
    return None
'''

_LOOP_MODULE = '''
"""Fake streaming orchestrator (offline integration tests).

Mirrors loop-streaming's hook surface for one scripted turn:
prompt:submit -> provider:request (steer boundary) -> llm:stream_block_*
-> provider:response -> tool:pre (through process_hook_result, the REAL
approval path) -> tool execute -> tool:post -> content_block:end ->
orchestrator:complete.
"""


class FakeLoop:
    def __init__(self, config):
        self.config = dict(config or {})

    async def execute(self, prompt, context, providers, tools, hooks, coordinator):
        submit_result = await hooks.emit("prompt:submit", {"prompt": prompt})
        await coordinator.process_hook_result(submit_result, "prompt:submit", "prompt")
        await context.add_message({"role": "user", "content": prompt})

        request_result = await hooks.emit(
            "provider:request", {"provider": "fake", "model": "fake-model"}
        )
        await coordinator.process_hook_result(
            request_result, "provider:request", "provider"
        )

        provider = next(iter(providers.values()))
        response = await provider.complete({"messages": await context.get_messages()})
        text = response["content"]

        await hooks.emit(
            "llm:stream_block_start",
            {"request_id": "req-1", "block_index": 0, "block_type": "text"},
        )
        for i, chunk in enumerate((text[: len(text) // 2], text[len(text) // 2 :])):
            await hooks.emit(
                "llm:stream_block_delta",
                {
                    "request_id": "req-1",
                    "block_index": 0,
                    "block_type": "text",
                    "sequence": i,
                    "delta": chunk,
                },
            )
        await hooks.emit(
            "llm:stream_block_end",
            {"request_id": "req-1", "block_index": 0, "block_type": "text"},
        )
        # Real loop-streaming never fires provider:response; usage rides the
        # final content_block:end instead. The flag mirrors that surface so
        # the orchestrator_config seam and the bridge's usage synthesis are
        # both exercised (spawn passes usage_on_block_end through).
        if not self.config.get("usage_on_block_end"):
            await hooks.emit(
                "provider:response",
                {"usage": dict(response["usage"]), "model": response["model"]},
            )

        tool_note = ""
        tool = tools.get("write_file")
        if tool is not None:
            pre = await hooks.emit(
                "tool:pre",
                {
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                    "tool_input": {"file_path": "hello.txt", "content": "hi"},
                },
            )
            pre = await coordinator.process_hook_result(pre, "tool:pre", "write_file")
            if pre.action == "deny":
                tool_note = f"Denied by hook: {pre.reason}"
            else:
                result = await tool.execute({"file_path": "hello.txt", "content": "hi"})
                tool_note = str(result)
                post = await hooks.emit(
                    "tool:post",
                    {
                        "tool_name": "write_file",
                        "tool_call_id": "call-1",
                        "tool_input": {"file_path": "hello.txt", "content": "hi"},
                        "result": result,
                    },
                )
                await coordinator.process_hook_result(post, "tool:post", "write_file")

        final = f"{text} [{tool_note}]" if tool_note else text
        await context.add_message({"role": "assistant", "content": final})
        block_end = {
            "block_type": "text",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": final},
        }
        if self.config.get("usage_on_block_end"):
            block_end["usage"] = dict(response["usage"])
        await hooks.emit("content_block:end", block_end)
        await hooks.emit(
            "orchestrator:complete",
            {"orchestrator": "loop-fake", "turn_count": 1, "status": "success"},
        )
        return final


async def mount(coordinator, config=None):
    await coordinator.mount("orchestrator", FakeLoop(config))
    return None
'''

_MODULES = {
    "amplifier-module-provider-fake/amplifier_module_provider_fake": _PROVIDER_MODULE,
    "amplifier-module-context-fake/amplifier_module_context_fake": _CONTEXT_MODULE,
    "amplifier-module-tool-fake/amplifier_module_tool_fake": _TOOL_MODULE,
    "amplifier-module-loop-fake/amplifier_module_loop_fake": _LOOP_MODULE,
}

_BUNDLE_TEMPLATE = """\
---
bundle:
  name: offline
  version: 0.0.1
  description: Offline integration-test bundle with fake modules.

session:
  orchestrator:
    module: loop-fake
    source: file://{modules}/amplifier-module-loop-fake
  context:
    module: context-fake
    source: file://{modules}/amplifier-module-context-fake

providers:
  - module: provider-fake
    source: file://{modules}/amplifier-module-provider-fake
    config:
      default_model: fake-model

tools:
  - module: tool-fake
    source: file://{modules}/amplifier-module-tool-fake
---

Offline test bundle instruction: you are a fake session.
"""


@pytest.fixture(scope="session")
def offline_workspace(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One shared workspace: fake modules + project bundle + fake HOME.

    Session-scoped because the loader imports fake modules by name
    (``amplifier_module_*``); a single on-disk location keeps
    ``sys.modules`` consistent across tests in this file.
    """
    root = tmp_path_factory.mktemp("offline-runtime")
    modules = root / "modules"
    for rel, source in _MODULES.items():
        package = modules / rel
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")

    project = root / "proj"
    bundles = project / ".amplifier" / "bundles"
    bundles.mkdir(parents=True)
    (bundles / "offline.md").write_text(_BUNDLE_TEMPLATE.format(modules=modules), encoding="utf-8")

    home = root / "home"
    home.mkdir()
    return {"project": project, "home": home}


@pytest.fixture
def offline_env(
    offline_workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Redirect HOME so session storage and module cache stay hermetic."""
    monkeypatch.setenv("HOME", str(offline_workspace["home"]))
    return offline_workspace


async def _started_runtime(project: Path, mode: str = "chat") -> RealRuntime:
    runtime = RealRuntime(bundle="offline", project_dir=project, mode=lambda: mode)
    await runtime.start()
    _register_policy_hook(runtime)
    return runtime


def _register_policy_hook(runtime: RealRuntime) -> None:
    """Stand-in for the native ``hooks-approval`` bundle module.

    The app governance hook owns its trust posture and directory boundary;
    this fake native hook proves that bundle-defined asks remain additive and
    still route through the same real ``process_hook_result``/broker path.
    """
    from amplifier_core import HookResult

    async def policy(event: str, data: dict) -> HookResult:
        del event
        if data.get("tool_name") == "write_file":
            return HookResult(
                action="ask_user",
                approval_prompt=f"Allow write_file · {data.get('tool_input', {}).get('path', '')}?",
                approval_options=list(STANDARD_OPTIONS),
                approval_default="deny",
            )
        return HookResult(action="continue")

    assert runtime._initialized is not None
    runtime._initialized.coordinator.hooks.register(
        "tool:pre", policy, priority=1000, name="fake-hooks-approval"
    )


async def _answer_next_approval(runtime: RealRuntime, choice: str) -> None:
    """Wait for the broker's head ticket and resolve it with *choice*."""
    for _ in range(500):
        head = runtime.broker.head
        if head is not None:
            assert head.options[:3] == STANDARD_OPTIONS
            runtime.broker.answer(head.ticket_id, choice)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval ticket appeared")


def _drain_kinds(runtime: RealRuntime) -> list:
    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    return events


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_turn_end_to_end_with_approval_allow(offline_env) -> None:
    """One real turn: stream deltas, ask_user approval, tool pre/post,
    orchestrator complete — all normalized onto the UI queue."""
    runtime = await _started_runtime(offline_env["project"])
    try:
        assert runtime.bundle_name == "offline"
        assert runtime.model_name == "fake/fake-model"
        assert "Provider: fake" in runtime.banner[1]
        assert runtime.degraded_notice is None

        answer = asyncio.create_task(_answer_next_approval(runtime, ALLOW_ONCE))
        response = await runtime.submit("please write hello.txt with hi")
        await answer

        assert response == (
            "Hello from the fake provider. [{'success': True, 'output': 'wrote hello.txt'}]"
        )

        events = _drain_kinds(runtime)
        kinds = [event.kind for event in events]
        for expected in (
            "prompt_submit",
            "stream_block_start",
            "stream_block_delta",
            "stream_block_end",
            "provider_response_usage",
            "tool_pre",
            "tool_post",
            "content_block_end",
            "orchestrator_complete",
            "prompt_complete",
        ):
            assert expected in kinds, f"missing {expected} in {kinds}"

        # Channel A: deltas carry the streamed text in order.
        deltas = [e for e in events if e.kind == "stream_block_delta"]
        assert "".join(d.text for d in deltas) == "Hello from the fake provider."
        # Channel B: tool records correlate by tool_call_id.
        (tool_pre,) = [e for e in events if e.kind == "tool_pre"]
        (tool_post,) = [e for e in events if e.kind == "tool_post"]
        assert tool_pre.tool_call_id == tool_post.tool_call_id == "call-1"
        assert tool_pre.tool_name == "write_file"
        # Stream deltas precede the tool record; the synthesized close-out
        # (post git-snapshot) is guaranteed to land last on the queue.
        assert kinds.index("stream_block_delta") < kinds.index("tool_pre")
        assert kinds.index("prompt_complete") == len(kinds) - 1
        assert kinds.index("orchestrator_complete") == len(kinds) - 2
        (complete,) = [e for e in events if e.kind == "orchestrator_complete"]
        assert complete.status == "success"
        (closing,) = [e for e in events if e.kind == "prompt_complete"]
        # The temp project is not a git repo and no test commands ran.
        assert (closing.files_changed, closing.diffstat, closing.tests_ok) == (0, "", None)
        assert closing.response == response

        # Persistence: transcript + metadata (incremental save on tool:post)
        # and the append-only ui-events.jsonl (cost re-seed source).
        session_id = runtime.session_short
        store = runtime._store
        assert store is not None
        full_id = store.find_session(session_id)
        session_dir = store.session_dir(full_id)
        assert (session_dir / "transcript.jsonl").is_file()
        assert (session_dir / "metadata.json").is_file()
        events_lines = (session_dir / "ui-events.jsonl").read_text().splitlines()
        recorded_kinds = {json.loads(line)["kind"] for line in events_lines}
        assert "provider_response_usage" in recorded_kinds
        assert "tool_post" in recorded_kinds
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_spawn_child_telemetry_reaches_the_queue(offline_env) -> None:
    """The fan-out telemetry seam, end to end through the REAL SessionSpawner.

    Drives the registered ``session.spawn`` capability with the exact
    kwargs foundation's tool-delegate passes (ground truth: the pinned
    ``amplifier_module_tool_delegate._spawn_new_session``) and emits the
    module's spawn/completion events verbatim — the completion payload
    carries NO ``result`` field. Asserts on the UI queue:

    - child-stamped ``stream_block_delta`` (Channel A lives for lanes)
    - synthesized child usage (``orchestrator_config`` reached the child
      orchestrator, which rode usage on ``content_block:end`` exactly like
      real loop-streaming; the bridge synthesized the telemetry event)
    - ``agent_spawned`` + ``agent_completed`` with a non-empty result
      synthesized from the child output the spawner captured
    """
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    try:
        initialized = runtime._initialized
        assert initialized is not None
        root_id = initialized.session_id
        spawn = initialized.coordinator.get_capability("session.spawn")
        assert spawn is not None
        hooks = initialized.coordinator.hooks
        sub_id = f"{root_id}-deadbeefcafef00d_scout"

        # Payload shapes below are verbatim copies of tool-delegate's emits.
        await hooks.emit(
            "delegate:agent_spawned",
            {
                "agent": "scout",
                "sub_session_id": sub_id,
                "parent_session_id": root_id,
                "context_depth": "recent",
                "context_scope": "conversation",
                "tool_call_id": "call-7",
                "parallel_group_id": None,
                "model_role": None,
                "provider_preferences": None,
            },
        )
        result = await spawn(
            agent_name="scout",
            instruction="[YOUR TASK]\nplease write hello.txt with hi",
            parent_session=initialized.session,
            agent_configs={},
            sub_session_id=sub_id,
            tool_inheritance={"exclude_tools": ["tool-delegate"]},
            hook_inheritance={},
            orchestrator_config={"usage_on_block_end": True},
            provider_preferences=None,
            self_delegation_depth=0,
            session_metadata={"agent_name": "scout", "tool_call_id": "call-7"},
        )
        await hooks.emit(
            "delegate:agent_completed",
            {
                "agent": "scout",
                "sub_session_id": sub_id,
                "parent_session_id": root_id,
                "success": True,
                "tool_call_id": "call-7",
                "parallel_group_id": None,
            },
        )

        assert result["status"] == "success"
        assert result["session_id"] == sub_id
        assert "Hello from the fake provider." in str(result["output"])

        events = _drain_kinds(runtime)
        # Channel A: the child's live tail, stamped with the child id.
        child_deltas = [
            e for e in events if e.kind == "stream_block_delta" and e.session_id == sub_id
        ]
        assert "".join(d.text for d in child_deltas) == "Hello from the fake provider."
        assert all(d.parent_id == root_id for d in child_deltas)
        # Child usage synthesized from its final content_block:end — proof
        # the orchestrator_config kwarg reached the child's orchestrator.
        (child_usage,) = [
            e for e in events if e.kind == "provider_response_usage" and e.session_id == sub_id
        ]
        assert (child_usage.input_tokens, child_usage.output_tokens) == (12, 7)
        # Child tool records flow too (lane activity ticker source).
        assert any(e.kind == "tool_post" and e.session_id == sub_id for e in events)
        # Lifecycle: spawn event normalized; completion result synthesized
        # from the child output (the raw payload had no result field).
        (spawned,) = [e for e in events if e.kind == "agent_spawned"]
        assert spawned.sub_session_id == sub_id
        (completed,) = [e for e in events if e.kind == "agent_completed"]
        assert completed.sub_session_id == sub_id
        assert "Hello from the fake provider." in completed.result
        # Real lane seed: the delegate brief is recorded for the adapter.
        assert runtime.agent_brief("scout") == "please write hello.txt with hi"
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_turn_approval_deny_is_deny_and_continue(offline_env) -> None:
    """Human Deny: the real engine synthesizes a denied tool result; the
    turn still completes (deny-and-continue, no tool_post)."""
    runtime = await _started_runtime(offline_env["project"])
    try:
        answer = asyncio.create_task(_answer_next_approval(runtime, DENY))
        response = await runtime.submit("please write hello.txt with hi")
        await answer

        assert "Denied by hook:" in response
        kinds = [event.kind for event in _drain_kinds(runtime)]
        assert "tool_pre" in kinds
        assert "tool_post" not in kinds
        assert "orchestrator_complete" in kinds
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_steer_injected_at_provider_request_boundary(offline_env) -> None:
    """A queued steer is consumed at ``provider:request`` and lands as ONE
    persistent user-role context message via the real inject_context path."""
    runtime = await _started_runtime(offline_env["project"])
    try:
        runtime.steering.enqueue("prefer short answers", kind="steer")

        answer = asyncio.create_task(_answer_next_approval(runtime, ALLOW_ONCE))
        await runtime.submit("please write hello.txt with hi")
        await answer

        assert runtime.steering.pending == ()

        events = _drain_kinds(runtime)
        kinds = [event.kind for event in events]
        assert "context_injected" in kinds
        narrations = [
            e.block.get("text")
            for e in events
            if e.kind == "content_block_end" and e.block.get("demo_role") == "narration"
        ]
        assert narrations == ["Applying steer: prefer short answers"]

        assert runtime._initialized is not None
        context = runtime._initialized.coordinator.get("context")
        messages = await context.get_messages()
        injected = [
            m
            for m in messages
            if m["role"] == "user" and "prefer short answers" in str(m["content"])
        ]
        assert len(injected) == 1
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_resume_restores_transcript_and_turn_base(offline_env) -> None:
    """Resume: the stored transcript is restored into the live context,
    ``turn_base`` counts the restored user messages (DESIGN-SPEC §9), and
    the stored UIEvents come back typed for transcript replay — with
    foreign/unparseable event-log lines skipped and the per-answer
    evidence map rebuilt (DESIGN-SPEC §3/§10/§11)."""
    from amplifier_app_newtui.kernel.persistence import SessionStore

    first = await _started_runtime(offline_env["project"])
    try:
        answer = asyncio.create_task(_answer_next_approval(first, ALLOW_ONCE))
        response = await first.submit("please write hello.txt with hi")
        await answer
        assert first._initialized is not None
        session_id = first._initialized.session_id
    finally:
        await first.cleanup()

    # Other apps' hook events share this file today — replay must skip
    # anything that is not one of our own persisted UIEvent records.
    store = SessionStore(project_dir=offline_env["project"])
    with store.events_path(session_id).open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write(json.dumps({"event": "tool:pre", "foreign": True}) + "\n")
        handle.write(json.dumps({"kind": "mystery_kind"}) + "\n")

    resumed = RealRuntime(
        bundle="offline",
        resume_id=session_id[:8],
        project_dir=offline_env["project"],
        mode=lambda: "chat",
    )
    await resumed.start()
    try:
        assert resumed.turn_base == 1
        assert resumed._initialized is not None
        context = resumed._initialized.coordinator.get("context")
        messages = await context.get_messages()
        roles = [m["role"] for m in messages]
        assert roles.count("user") == 1
        assert roles.count("assistant") == 1
        assert any(m["role"] == "system" for m in messages)

        kinds = [event.kind for event in resumed.restored_events]
        for expected in ("prompt_submit", "tool_pre", "tool_post", "prompt_complete"):
            assert expected in kinds, f"missing {expected} in {kinds}"
        # Channel A is dropped at load time (the durable content blocks
        # carry the text); foreign lines never parse into events.
        assert "stream_block_delta" not in kinds
        # The evidence map is rebuilt from the same stored stream, so the
        # restored final answer stays clickable after resume (spec §10).
        assert resumed.evidence.links_for(response.strip()) != ()
    finally:
        await resumed.cleanup()


def _resume_bundle_project(
    offline_workspace: dict[str, Path],
    name: str,
    *,
    active: str | None = None,
    use_active: bool = False,
) -> Path:
    """A fresh project dir with offline + offline2 bundles and settings.

    Separate from the shared ``proj`` so per-test settings (active bundle,
    resume override) never leak into the other offline tests.
    """
    import yaml

    root = offline_workspace["project"].parent
    modules = root / "modules"
    project = root / name
    bundles = project / ".amplifier" / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    template = _BUNDLE_TEMPLATE.format(modules=modules)
    (bundles / "offline.md").write_text(template, encoding="utf-8")
    (bundles / "offline2.md").write_text(template, encoding="utf-8")
    settings: dict = {}
    if active:
        settings["bundle"] = {"active": active}
    if use_active:
        settings["resume"] = {"use_active_bundle": True}
    if settings:
        (project / ".amplifier" / "settings.yaml").write_text(
            yaml.safe_dump(settings), encoding="utf-8"
        )
    return project


def _store_session(project: Path, session_id: str, bundle: str) -> None:
    from amplifier_app_newtui.kernel.persistence import SessionStore

    SessionStore(project_dir=project).save(
        session_id,
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        {"bundle": bundle, "session_id": session_id},
    )


def _notifications(runtime: RealRuntime) -> list[str]:
    return [e.message for e in _drain_kinds(runtime) if e.kind == "notification"]


@pytest.mark.asyncio
async def test_resume_attaches_under_the_stored_bundle_by_default(offline_env) -> None:
    """Contract: session stored under bundle X, active bundle Y — resume
    runs under X (resolved through the normal resolve_config/foundation
    path) and one notice names both bundles. Previously the session
    reattached under Y: a silent module-stack swap."""
    project = _resume_bundle_project(offline_env, "proj-resume-stored", active="offline2")
    _store_session(project, "resumestored01", "offline")

    runtime = RealRuntime(
        resume_id="resumestored01", project_dir=project, mode=lambda: "chat"
    )
    await runtime.start()
    try:
        assert runtime.bundle_name == "offline"
        notices = _notifications(runtime)
        assert any("offline" in m and "offline2" in m for m in notices), notices
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_resume_override_setting_attaches_the_active_bundle(offline_env) -> None:
    """Contract: with ``resume.use_active_bundle: true`` the session runs
    under the ACTIVE bundle and the notice says what happened."""
    project = _resume_bundle_project(
        offline_env, "proj-resume-active", active="offline2", use_active=True
    )
    _store_session(project, "resumeactive01", "offline")

    runtime = RealRuntime(
        resume_id="resumeactive01", project_dir=project, mode=lambda: "chat"
    )
    await runtime.start()
    try:
        assert runtime.bundle_name == "offline2"
        notices = _notifications(runtime)
        assert any(
            "offline" in m and "offline2" in m and "resume.use_active_bundle" in m
            for m in notices
        ), notices
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_resume_missing_stored_bundle_falls_back_loudly(offline_env) -> None:
    """A stored bundle that no longer discovers must not kill the resume:
    the boot continues on the active bundle with an explicit notice."""
    project = _resume_bundle_project(offline_env, "proj-resume-ghost", active="offline2")
    _store_session(project, "resumeghost01", "ghost-bundle")

    runtime = RealRuntime(
        resume_id="resumeghost01", project_dir=project, mode=lambda: "chat"
    )
    await runtime.start()
    try:
        assert runtime.bundle_name == "offline2"
        notices = _notifications(runtime)
        assert any("ghost-bundle" in m and "not found" in m for m in notices), notices
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_session_directory_capability_is_live_and_restored(offline_env) -> None:
    """TUI add/remove writes session settings and updates the live policy;
    a resumed session folds the same capability in before mounting tools."""
    shared = offline_env["project"].parent / "shared"
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    try:
        ok, detail = await runtime.update_session_directory("allowed", "add", str(shared))
        assert ok and "session scope" in detail
        assert runtime.directory_policy is not None
        assert runtime.directory_policy.check_write(shared / "ok.txt")[0]
        session_id = runtime.session_id
        assert runtime._store is not None
        settings = runtime._store.session_dir(session_id) / "settings.yaml"
        assert str(shared.resolve()) in settings.read_text(encoding="utf-8")
    finally:
        await runtime.cleanup()

    resumed = RealRuntime(
        bundle="offline",
        resume_id=session_id[:8],
        project_dir=offline_env["project"],
        mode=lambda: "auto",
    )
    await resumed.start()
    try:
        assert resumed.directory_policy is not None
        assert resumed.directory_policy.check_write(shared / "restored.txt")[0]
        assert any(
            entry.path == str(shared.resolve()) and entry.scope == "session"
            for entry in resumed.directory_entries("allowed")
        )
    finally:
        await resumed.cleanup()


def test_apply_hook_suppression_strips_and_notifies() -> None:
    """App overlays can drag in stdout printers (hooks-streaming-ui et al);
    raw ANSI under the full-screen TUI corrupts the screen (found live).
    Stripping is no longer silent - exactly one Notification lists what
    was removed so it's never a silent surprise."""
    from amplifier_app_newtui.kernel.events import Notification
    from amplifier_app_newtui.kernel.runtime import _apply_hook_suppression

    plan = {
        "hooks": [
            {"module": "hooks-streaming-ui"},
            {"module": "hooks-approval"},
            {"module": "hooks-logging"},
            {"module": "hooks-mode"},
        ]
    }
    emitted: list[Notification] = []
    removed = _apply_hook_suppression(plan, emitted.append)

    # hooks-logging is NOT suppressed: the app's UIEvent log moved to
    # ui-events.jsonl, so hooks-logging owns the canonical events.jsonl.
    assert removed == ["hooks-streaming-ui"]
    assert plan["hooks"] == [
        {"module": "hooks-approval"},
        {"module": "hooks-logging"},
        {"module": "hooks-mode"},
    ]
    assert len(emitted) == 1
    assert isinstance(emitted[0], Notification)
    assert "hooks-streaming-ui" in emitted[0].message
    assert "hooks-logging" not in emitted[0].message


def test_apply_hook_suppression_with_user_suppress_setting() -> None:
    """A caller-supplied ``suppressed`` set (e.g. from ``hooks.suppress``)
    overrides the implicit default, so user-added hooks can be stripped too."""
    from amplifier_app_newtui.kernel.runtime import (
        _SUPPRESSED_HOOKS_DEFAULT,
        _apply_hook_suppression,
    )

    plan = {
        "hooks": [
            {"module": "hooks-streaming-ui"},
            {"module": "hooks-custom"},
            {"module": "hooks-mode"},
        ]
    }
    suppressed = _SUPPRESSED_HOOKS_DEFAULT | frozenset({"hooks-logging", "hooks-custom"})
    emitted: list[object] = []
    removed = _apply_hook_suppression(plan, emitted.append, suppressed)

    assert "hooks-custom" in removed
    assert "hooks-streaming-ui" in removed
    assert plan["hooks"] == [{"module": "hooks-mode"}]


def test_suppressed_hooks_setting_defaults_and_union() -> None:
    """Copies the ``write_boundary_setting`` resolver pattern: the built-in
    default set is always present, and a user ``hooks.suppress`` list is
    unioned in (junk shapes fall back to defaults, blanks are stripped)."""
    from amplifier_app_newtui.kernel.runtime import (
        _SUPPRESSED_HOOKS_DEFAULT,
        suppressed_hooks_setting,
    )

    assert _SUPPRESSED_HOOKS_DEFAULT == frozenset(
        {
            "hooks-streaming-ui",
            "hooks-todo-display",
            "hooks-notify",
        }
    )
    # hooks-logging is NOT suppressed: it owns the canonical events.jsonl;
    # the app's UIEvent log writes ui-events.jsonl (no double-write left).
    assert "hooks-logging" not in _SUPPRESSED_HOOKS_DEFAULT
    # hooks-insight-blocks / hooks-inline-blocks are NOT suppressed: recon
    # of the cached modules shows they are inject_context instruction hooks
    # (session:start / prompt:submit) with zero stdout — suppressing them
    # severed the insight/MJ callout channel, it never protected the screen.
    assert "hooks-insight-blocks" not in _SUPPRESSED_HOOKS_DEFAULT
    assert "hooks-inline-blocks" not in _SUPPRESSED_HOOKS_DEFAULT
    assert suppressed_hooks_setting({}) == _SUPPRESSED_HOOKS_DEFAULT
    assert suppressed_hooks_setting({"hooks": "junk"}) == _SUPPRESSED_HOOKS_DEFAULT
    assert (
        suppressed_hooks_setting({"hooks": {"suppress": "not-a-list"}}) == _SUPPRESSED_HOOKS_DEFAULT
    )

    resolved = suppressed_hooks_setting({"hooks": {"suppress": ["hooks-custom", ""]}})
    assert "hooks-custom" in resolved
    assert "" not in resolved
    assert _SUPPRESSED_HOOKS_DEFAULT <= resolved


def test_resume_bundle_plan_defaults_to_stored() -> None:
    """A session's module stack is part of its identity: with no explicit
    --bundle and no override setting, resume boots the STORED bundle."""
    from amplifier_app_newtui.kernel.runtime import _plan_resume_bundle

    assert _plan_resume_bundle("offline", None, use_active=False) == ("offline", "stored")
    # Explicit --bundle: the caller asked for it by name — it wins.
    assert _plan_resume_bundle("offline", "other", use_active=False) == ("other", "explicit")
    # Settings override: attach under whatever is currently active.
    assert _plan_resume_bundle("offline", None, use_active=True) == (None, "active")
    # No stored bundle recorded (older metadata): nothing to honor.
    assert _plan_resume_bundle(None, None, use_active=False) == (None, "active")


def test_resume_use_active_bundle_setting_shapes() -> None:
    """Junk-shaped settings fall back to the default (honor stored)."""
    from amplifier_app_newtui.kernel.runtime import resume_use_active_bundle

    assert resume_use_active_bundle({}) is False
    assert resume_use_active_bundle({"resume": "junk"}) is False
    assert resume_use_active_bundle({"resume": {"use_active_bundle": "yes"}}) is False
    assert resume_use_active_bundle({"resume": {"use_active_bundle": True}}) is True


def test_resume_bundle_notice_names_both_on_divergence() -> None:
    """Every non-default resume-bundle outcome is said out loud, naming
    both the stored and the attached bundle."""
    from amplifier_app_newtui.kernel.events import Notification
    from amplifier_app_newtui.kernel.runtime import _resume_bundle_notice

    # Stored honored while a different bundle is active.
    emitted: list[Notification] = []
    _resume_bundle_notice("offline", "stored", "offline", "newtui", emitted.append)
    assert len(emitted) == 1
    assert "offline" in emitted[0].message and "newtui" in emitted[0].message

    # Override attached the active bundle over the stored one.
    emitted.clear()
    _resume_bundle_notice("offline", "active", "newtui", "newtui", emitted.append)
    assert len(emitted) == 1
    assert "offline" in emitted[0].message and "newtui" in emitted[0].message
    assert "resume.use_active_bundle" in emitted[0].message

    # Explicit --bundle override.
    emitted.clear()
    _resume_bundle_notice("offline", "explicit", "other", "newtui", emitted.append)
    assert len(emitted) == 1
    assert "--bundle" in emitted[0].message

    # Stored bundle no longer discoverable — fallback said loudly.
    emitted.clear()
    _resume_bundle_notice("ghost", "stored-missing", "newtui", "newtui", emitted.append)
    assert len(emitted) == 1
    assert "ghost" in emitted[0].message and "not found" in emitted[0].message


def test_resume_bundle_notice_silent_on_common_cases() -> None:
    """Quiet when stored and attached agree, and when nothing was stored."""
    from amplifier_app_newtui.kernel.runtime import _resume_bundle_notice

    emitted: list[object] = []
    _resume_bundle_notice("newtui", "stored", "newtui", "newtui", emitted.append)
    _resume_bundle_notice("newtui", "explicit", "newtui", "newtui", emitted.append)
    _resume_bundle_notice("newtui", "active", "newtui", "newtui", emitted.append)
    _resume_bundle_notice(None, "active", "newtui", "newtui", emitted.append)
    assert emitted == []


def test_restored_history_extracts_prose_and_skips_tool_traffic() -> None:
    from amplifier_app_newtui.kernel.runtime import restored_history

    transcript = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Reply with exactly: OK"},
        {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "<system-reminder>injected steer</system-reminder>"},
        {"role": "user", "content": [{"type": "text", "text": "and again"}]},
        {"role": "assistant", "tool_calls": [{}], "content": ""},
    ]
    assert restored_history(transcript) == (
        ("user", "Reply with exactly: OK"),
        ("assistant", "OK"),
        ("user", "and again"),
    )


def test_native_modes_go_through_the_mounted_mode_tool() -> None:
    """User directive: action modes through amplifier-foundation (the
    bundle-mounted mode tool), never an app-local mode engine. Covers the
    hooks-mode warn gate: a denied first ``set`` is retried once."""
    import asyncio
    from types import SimpleNamespace

    from amplifier_app_newtui.kernel.runtime import RealRuntime

    class FakeModeTool:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.gate_armed = True

        async def execute(self, payload: dict):
            self.calls.append(payload)
            if payload.get("operation") == "list":
                return SimpleNamespace(success=True, output="superpowers:\n  debug ...")
            if self.gate_armed:
                self.gate_armed = False  # warn gate: deny once, confirm on retry
                return SimpleNamespace(success=False, output=None, error="confirm transition")
            return SimpleNamespace(success=True, output={"message": "mode set: debug"})

    async def run() -> None:
        runtime = RealRuntime()
        tool = FakeModeTool()
        runtime._initialized = SimpleNamespace(  # type: ignore[assignment]
            coordinator=SimpleNamespace(get=lambda point: {"mode": tool})
        )
        catalog = await runtime.list_native_modes()
        assert "superpowers" in catalog
        ok, detail = await runtime.set_native_mode("debug")
        assert ok and detail == "mode set: debug"
        # deny-once gate consumed exactly one retry
        assert [c.get("operation") for c in tool.calls] == ["list", "set", "set"]

        bare = RealRuntime()
        catalog_coro = bare.list_native_modes()
        assert asyncio.iscoroutine(catalog_coro)
        assert (await catalog_coro) == ""
        ok, detail = await bare.set_native_mode("debug")
        assert not ok and "no native mode system" in detail

    asyncio.run(run())


def test_set_model_refreshes_the_footer_model_name() -> None:
    """Codex review on PR #14: a successful ``/model`` switch mutated the
    live provider but left ``model_name`` at its boot-time value, so the
    footer kept showing the old model until restart."""
    import asyncio
    from types import SimpleNamespace

    from amplifier_app_newtui.kernel.runtime import RealRuntime

    async def run() -> None:
        runtime = RealRuntime()
        runtime.model_name = "anthropic/m1"
        provider = SimpleNamespace(default_model="m1", config={"default_model": "m1"})
        coordinator = SimpleNamespace(
            get=lambda point: {"providers": {"anthropic": provider}}.get(point),
            session_state={},
        )
        runtime._initialized = SimpleNamespace(coordinator=coordinator)  # type: ignore[assignment]

        ok, detail = await runtime.set_model("m2")
        assert ok and detail == "anthropic · m2"
        assert provider.default_model == "m2"
        assert runtime.model_name == "anthropic/m2"

        # a failed switch must not clobber the live name
        ok, _detail = await runtime.set_model("")
        assert not ok
        assert runtime.model_name == "anthropic/m2"

    asyncio.run(run())


def test_broker_approval_provider_adapts_native_requests() -> None:
    """hooks-approval asks its registered ApprovalProvider — the adapter
    presents through the broker and reports remember for Allow always
    (native module owns the persistence; user directive)."""
    import asyncio
    from types import SimpleNamespace

    from amplifier_app_newtui.kernel.approval import ALLOW_ALWAYS, ApprovalBroker
    from amplifier_app_newtui.kernel.runtime import _BrokerApprovalProvider

    async def run() -> None:
        broker = ApprovalBroker()
        provider = _BrokerApprovalProvider(broker)
        request = SimpleNamespace(
            tool_name="bash",
            action="rm newtui-native-test.txt",
            details={"command": "rm newtui-native-test.txt"},
            risk_level="high",
            timeout=None,
        )
        task = asyncio.ensure_future(provider.request_approval(request))
        for _ in range(100):
            if broker.head is not None:
                break
            await asyncio.sleep(0.01)
        head = broker.head
        assert head is not None
        assert head.prompt == "Allow rm newtui-native-test.txt?"
        assert head.detail.tool_name == "bash"
        broker.answer(head.ticket_id, ALLOW_ALWAYS)
        response = await task
        assert response.approved is True
        assert response.remember is True

    asyncio.run(run())
