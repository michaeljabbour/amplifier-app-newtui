"""``RealRuntimeAdapter`` threading harness: the runtime lives on its OWN
thread + event loop, and every interaction marshals across.

Seam: ``_thread_body`` lazily does ``from ..kernel.runtime import
RealRuntime`` — monkeypatching
``amplifier_app_newtui.kernel.runtime.RealRuntime`` therefore swaps in
:class:`FakeRealRuntime` at thread-boot time, so the REAL marshalling
paths (``call_soon_threadsafe`` / ``run_coroutine_threadsafe``) run
against a recording fake.

Covers start happy path + identity copy, boot-failure exception
marshalling, all 20 proxies on the runtime thread, pre-boot neutral
guards, broker presentation dedupe, approval ``KeyError`` swallow, and
live-loop shutdown. Timing-tolerant: synchronization is via
``asyncio.wait_for``, ``threading.Event`` and bounded polling — never
bare sleeps.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio

from amplifier_app_newtui.kernel.compaction import CompactionConfig
from amplifier_app_newtui.kernel.events import Notification, PromptSubmit
from amplifier_app_newtui.kernel.rewind import RewindError
from amplifier_app_newtui.kernel.session_ops import ModelListing, StatusInfo
from amplifier_app_newtui.model.config import default_config_state
from amplifier_app_newtui.model.trust import CapabilityClass, TrustDecision
from amplifier_app_newtui.ui.runtime_adapter import RealRuntimeAdapter, _AppLoopQueue

SEAM = "amplifier_app_newtui.kernel.runtime.RealRuntime"

# ---------------------------------------------------------------------------
# Fakes (surfaces pinned by the test spec §3)
# ---------------------------------------------------------------------------

# Coroutine methods the adapter proxies verbatim into the runtime loop.
# ``set_model`` and the sync ``directory_entries`` are defined explicitly
# on FakeRealRuntime (they carry extra behavior).
_PROXIED: tuple[str, ...] = (
    "submit",
    "interrupt",
    "list_native_modes",
    "set_native_mode",
    "list_models",
    "get_effort",
    "set_effort",
    "compact",
    "clear_context",
    "status",
    "list_tools",
    "list_agents",
    "diff",
    "workspace_files",
    "list_skills",
    "load_skill",
    "mcp_tools",
    "update_session_directory",
    "fork",
)

SENTINELS: dict[str, object] = {
    name: object() for name in (*_PROXIED, "set_model", "directory_entries")
}


@dataclass(frozen=True)
class FakeTicket:
    ticket_id: str
    prompt: str
    options: tuple[str, ...]


class FakeBroker:
    def __init__(self) -> None:
        self.listeners: list[Callable[[], None]] = []
        self.head: FakeTicket | None = None
        self.answers: list[tuple[str, str]] = []
        self.answered = threading.Event()
        self.raise_key_error = False

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: None

    def answer(self, ticket_id: str, choice: str) -> None:
        self.answers.append((ticket_id, choice))
        self.answered.set()
        if self.raise_key_error:
            raise KeyError(ticket_id)


class FakeEvidence:
    def __init__(self) -> None:
        self.sentinel = (object(),)
        self.calls: list[str] = []

    def links_for(self, text: str) -> tuple[object, ...]:
        self.calls.append(text)
        return self.sentinel


class FakeRealRuntime:
    """Stands in for ``kernel.runtime.RealRuntime`` behind the lazy-import
    seam. Keyword-only constructor pins the exact kwargs the adapter
    passes — construction drift fails loudly here."""

    def __init__(
        self,
        *,
        bundle: str | None,
        resume_id: str | None,
        queue: Any,
        steering: Any,
        needs_you: Any,
        denial_log: Any,
        surface: Any,
        mode: Callable[[], str],
        permission_resolver: Callable[..., Any],
        capability_resolver: Callable[..., Any],
        on_progress: Callable[[str, str], None],
    ) -> None:
        self.kwargs: dict[str, Any] = {
            "bundle": bundle,
            "resume_id": resume_id,
            "queue": queue,
            "steering": steering,
            "needs_you": needs_you,
            "denial_log": denial_log,
            "surface": surface,
            "mode": mode,
            "permission_resolver": permission_resolver,
            "capability_resolver": capability_resolver,
            "on_progress": on_progress,
        }
        self.bundle_name = "fake-bundle"
        self.model_name = "fake/model-1"
        self.session_short = "abc12345"
        self.banner = ("Fake Banner", "subtitle")
        self.session_cost_start = Decimal("1.25")
        self.turn_base = 3
        self.restored_history = (("user", "hi"), ("assistant", "hey"))
        self.restored_events = (PromptSubmit(session_id="stored", prompt="hi"),)
        self.compaction = CompactionConfig(auto_compact=False, compact_threshold=0.5)
        self.degraded_notice = ""
        self.broker = FakeBroker()
        self.evidence = FakeEvidence()
        self.calls: list[tuple[str, tuple[Any, ...], int]] = []
        self.started_loop: asyncio.AbstractEventLoop | None = None
        self.cleanup_called = threading.Event()
        self.next_model_name: str | None = None
        self.project_dir = Path("/fake/project")

    def record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args, threading.get_ident()))

    async def start(self) -> None:
        self.started_loop = asyncio.get_running_loop()
        self.record("start", ())

    async def cleanup(self) -> None:
        self.cleanup_called.set()
        self.record("cleanup", ())

    def agent_brief(self, agent_name: str) -> str:
        self.record("agent_brief", (agent_name,))
        return "fix the flaky test" if agent_name == "scout" else ""

    async def set_model(self, model: str) -> object:
        self.record("set_model", (model,))
        if self.next_model_name is not None:
            self.model_name = self.next_model_name
        return SENTINELS["set_model"]

    def directory_entries(self, kind: str) -> object:
        # Sync on the real runtime — the adapter wraps it in a coroutine.
        self.record("directory_entries", (kind,))
        return SENTINELS["directory_entries"]

    def config_state(self) -> object:
        # Sync seed read at start() (like agent_brief); a real plan-derived
        # SessionConfigState. The adapter stores it for /config ops.
        self.record("config_state", ())
        return default_config_state(self.bundle_name)


def _make_proxy(name: str) -> Callable[..., Any]:
    async def proxy(self: FakeRealRuntime, *args: Any) -> Any:
        self.record(name, args)
        return SENTINELS[name]

    proxy.__name__ = name
    return proxy


for _name in _PROXIED:
    setattr(FakeRealRuntime, _name, _make_proxy(_name))


class FakePermissions:
    def __init__(self) -> None:
        self.call_decision = TrustDecision(
            decision="allow", capability=CapabilityClass.READ, reason="fake call"
        )
        self.capability_decision = TrustDecision(
            decision="deny", capability=CapabilityClass.EXEC, reason="fake capability"
        )
        self.resolve_calls: list[tuple[str, Any]] = []
        self.capability_calls: list[CapabilityClass] = []

    def resolve_call(self, tool_name: str, tool_input: Any) -> TrustDecision:
        self.resolve_calls.append((tool_name, tool_input))
        return self.call_decision

    def resolve_capability(self, capability: CapabilityClass) -> TrustDecision:
        self.capability_calls.append(capability)
        return self.capability_decision


class FakeApp:
    def __init__(self) -> None:
        self.mode_id = "auto"
        self.permissions = FakePermissions()
        self.progress: list[tuple[str, str]] = []
        self.approvals: list[tuple[str, str, tuple[str, ...]]] = []

    def boot_progress(self, action: str, detail: str) -> None:
        self.progress.append((action, detail))

    def present_approval(self, ticket_id: str, prompt: str, options: tuple[str, ...]) -> None:
        self.approvals.append((ticket_id, prompt, options))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Booted:
    adapter: RealRuntimeAdapter
    fake: FakeRealRuntime
    app: FakeApp
    ready_calls: list[int]


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Bounded poll — timing-tolerant stand-in for 'yield the app loop'."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


@pytest_asyncio.fixture
async def booted(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Booted]:
    """Patch the seam, boot the adapter, ALWAYS shut the thread down."""
    monkeypatch.setattr(SEAM, FakeRealRuntime)
    adapter = RealRuntimeAdapter(bundle="x")
    app = FakeApp()
    adapter.attach(app)
    ready_calls: list[int] = []
    try:
        await asyncio.wait_for(adapter.start(lambda: ready_calls.append(1)), timeout=10)
        fake = cast(FakeRealRuntime, adapter._runtime)
        yield Booted(adapter=adapter, fake=fake, app=app, ready_calls=ready_calls)
    finally:
        adapter.shutdown()  # never leak the real-runtime thread


# ---------------------------------------------------------------------------
# T5 — _AppLoopQueue marshals puts from a worker thread to the app loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_loop_queue_hops_threads() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    shim = _AppLoopQueue(queue, asyncio.get_running_loop())
    event = Notification(message="hop")
    worker = threading.Thread(target=shim.put_nowait, args=(event,))
    worker.start()
    received = await asyncio.wait_for(queue.get(), timeout=5)
    worker.join(timeout=5)
    assert received is event


# ---------------------------------------------------------------------------
# T6/T7 — start happy path, identity copy, degraded notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_happy_path_copies_identity(booted: Booted) -> None:
    adapter, fake = booted.adapter, booted.fake
    assert booted.ready_calls == [1]

    # Identity attrs copied off the runtime after its start() finished.
    assert adapter.bundle_name == "fake-bundle"
    assert adapter.model_name == "fake/model-1"
    assert adapter.session_short == "abc12345"
    assert adapter.banner == ("Fake Banner", "subtitle")
    assert adapter.session_cost_start == Decimal("1.25")
    assert adapter.turn_base == 3
    assert adapter.restored_history == fake.restored_history
    assert adapter.restored_events == fake.restored_events
    assert adapter.compaction is fake.compaction
    assert adapter.startup_notices == ()  # no degraded notice

    # Broker listener registered for approval presentation.
    assert fake.broker.listeners == [adapter._on_broker_change]

    # Constructed with the adapter's own queues, resolvers and hooks.
    assert fake.kwargs["bundle"] == "x"
    assert fake.kwargs["resume_id"] is None
    assert isinstance(fake.kwargs["queue"], _AppLoopQueue)
    assert fake.kwargs["steering"] is adapter.steering
    assert fake.kwargs["needs_you"] is adapter.needs_you
    assert fake.kwargs["denial_log"] is adapter.denial_log
    assert fake.kwargs["surface"] is adapter.terminal
    assert fake.kwargs["mode"] == adapter._current_mode
    assert fake.kwargs["permission_resolver"] == adapter._resolve_permission
    assert fake.kwargs["capability_resolver"] == adapter._resolve_capability
    assert fake.kwargs["on_progress"] == adapter._boot_progress

    # start() ran on the runtime thread's OWN loop, not the app loop.
    assert fake.started_loop is not None
    assert fake.started_loop is not asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_start_surfaces_degraded_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    class DegradedFake(FakeRealRuntime):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.degraded_notice = "d"

    monkeypatch.setattr(SEAM, DegradedFake)
    adapter = RealRuntimeAdapter(bundle="x")
    adapter.attach(FakeApp())
    try:
        await asyncio.wait_for(adapter.start(lambda: None), timeout=10)
        assert adapter.startup_notices == ("d",)
    finally:
        adapter.shutdown()


# ---------------------------------------------------------------------------
# T8 — boot failure marshals the EXACT exception instance to the app loop
# ---------------------------------------------------------------------------


class _BootError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_boot_failure_marshalled_to_app_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _BootError("boot went sideways")

    class FailingFake(FakeRealRuntime):
        async def start(self) -> None:
            raise marker

    monkeypatch.setattr(SEAM, FailingFake)
    adapter = RealRuntimeAdapter(bundle="x")
    adapter.attach(FakeApp())
    ready_calls: list[int] = []
    try:
        with pytest.raises(_BootError) as excinfo:
            await asyncio.wait_for(adapter.start(lambda: ready_calls.append(1)), timeout=10)
        # The `failure = error` rebinding survives except-name unbinding:
        # the app loop re-raises the exact instance.
        assert excinfo.value is marker
        assert ready_calls == []
        # _thread_body returned early; asyncio.run closes the loop and
        # the thread exits on its own.
        assert adapter._thread is not None
        adapter._thread.join(timeout=8)
        assert not adapter._thread.is_alive()
    finally:
        adapter.shutdown()  # clean no-op on the already-closed loop


# ---------------------------------------------------------------------------
# T9 — every proxy runs its coroutine on the runtime thread
# ---------------------------------------------------------------------------

_LEDGER = object()

# (adapter method, call args, runtime method it lands on, returns sentinel?)
PROXIES: tuple[tuple[str, tuple[Any, ...], str, bool], ...] = (
    ("submit", ("hello", ()), "submit", False),
    ("interrupt", (), "interrupt", True),
    ("list_native_modes", (), "list_native_modes", True),
    ("set_native_mode", ("plan",), "set_native_mode", True),
    ("list_models", (), "list_models", True),
    ("set_model", ("m2",), "set_model", True),
    ("get_effort", (), "get_effort", True),
    ("set_effort", ("high",), "set_effort", True),
    ("compact", ("focus",), "compact", True),
    ("clear_context", (), "clear_context", True),
    ("status", (), "status", True),
    ("list_tools", (), "list_tools", True),
    ("list_agents", (), "list_agents", True),
    ("diff", (True,), "diff", True),
    ("workspace_files", (), "workspace_files", True),
    ("list_skills", (), "list_skills", True),
    ("load_skill", ("s",), "load_skill", True),
    ("mcp_tools", (), "mcp_tools", True),
    ("update_directory", ("allowed", "add", "/p"), "update_session_directory", True),
    ("fork", ("cp-1", _LEDGER), "fork", False),
)


def test_proxy_table_is_complete() -> None:
    assert len(PROXIES) == 20  # spec §4 T9: all twenty proxies


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "args", "runtime_name", "returns_sentinel"), PROXIES)
async def test_proxies_run_on_runtime_thread(
    booted: Booted,
    method: str,
    args: tuple[Any, ...],
    runtime_name: str,
    returns_sentinel: bool,
) -> None:
    adapter, fake = booted.adapter, booted.fake
    result = await asyncio.wait_for(getattr(adapter, method)(*args), timeout=5)
    if returns_sentinel:
        assert result is SENTINELS[runtime_name]
    else:
        assert result is None
    recorded = [call for call in fake.calls if call[0] == runtime_name]
    assert len(recorded) == 1
    _, recorded_args, thread_ident = recorded[0]
    assert recorded_args == args
    assert adapter._thread is not None
    assert thread_ident == adapter._thread.ident  # the _in_runtime spine
    assert thread_ident != threading.get_ident()


# ---------------------------------------------------------------------------
# T10 — before boot, every proxy answers neutrally without a runtime
# ---------------------------------------------------------------------------

PREBOOT_NEUTRALS: tuple[tuple[str, tuple[Any, ...], Any], ...] = (
    ("submit", ("x", ()), None),
    ("interrupt", (), False),
    ("list_native_modes", (), ""),
    ("set_native_mode", ("m",), (False, "session still starting")),
    ("list_models", (), ModelListing(provider="", current="")),
    ("set_model", ("m",), (False, "session still starting")),
    ("get_effort", (), None),
    ("set_effort", ("high",), (False, "session still starting")),
    ("compact", ("f",), (False, "session still starting")),
    ("clear_context", (), (False, 0)),
    ("status", (), StatusInfo()),
    ("list_tools", (), ()),
    ("list_agents", (), ()),
    ("diff", (True,), None),
    ("workspace_files", (), ()),
    ("list_skills", (), ()),
    ("load_skill", ("s",), (False, "session still starting")),
    ("mcp_tools", (), ()),
    ("directory_entries", ("allowed",), ()),
    ("update_directory", ("allowed", "add", "/p"), (False, "session still starting")),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "args", "expected"), PREBOOT_NEUTRALS)
async def test_proxies_neutral_before_boot(
    method: str, args: tuple[Any, ...], expected: Any
) -> None:
    adapter = RealRuntimeAdapter(bundle="x")  # never started: _runtime is None
    result = await getattr(adapter, method)(*args)
    assert result == expected


@pytest.mark.asyncio
async def test_neutral_guards_before_boot() -> None:
    adapter = RealRuntimeAdapter(bundle="x")
    with pytest.raises(RewindError):
        await adapter.fork("cp-1", _LEDGER)
    assert adapter.evidence_links("answer") == ()
    assert adapter.answer_approval("t1", "allow") is None  # silent return
    assert adapter.lane_seed("scout") is None


@pytest.mark.asyncio
async def test_lane_seed_uses_the_delegate_brief(booted: Booted) -> None:
    """Real lanes seed from the spawner-recorded delegate brief; the
    telemetry fields stay zero (they accrue from child-stamped events)."""
    seed = booted.adapter.lane_seed("scout")
    assert seed is not None
    assert seed.activity == "fix the flaky test"
    assert (seed.elapsed, seed.tokens, seed.cost, seed.state) == (
        0.0,
        0,
        Decimal("0"),
        "running",
    )
    assert booted.adapter.lane_seed("never-spawned") is None


# ---------------------------------------------------------------------------
# T11/T12 — set_model footer refresh; directory_entries sync wrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_model_refreshes_footer_copy(booted: Booted) -> None:
    booted.fake.next_model_name = "new"
    result = await asyncio.wait_for(booted.adapter.set_model("new"), timeout=5)
    assert result is SENTINELS["set_model"]
    assert booted.adapter.model_name == "new"  # footer copy kept live


@pytest.mark.asyncio
async def test_directory_entries_wraps_sync_read(booted: Booted) -> None:
    result = await asyncio.wait_for(booted.adapter.directory_entries("allowed"), timeout=5)
    assert result is SENTINELS["directory_entries"]
    recorded = [c for c in booted.fake.calls if c[0] == "directory_entries"]
    assert booted.adapter._thread is not None
    assert recorded == [("directory_entries", ("allowed",), booted.adapter._thread.ident)]


# ---------------------------------------------------------------------------
# T13/T14 — answer_approval hops into the runtime loop; KeyError swallowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_approval_hops_into_runtime_loop(booted: Booted) -> None:
    booted.adapter.answer_approval("t1", "allow")
    assert await asyncio.to_thread(booted.fake.broker.answered.wait, 5.0)
    assert booted.fake.broker.answers == [("t1", "allow")]


@pytest.mark.asyncio
async def test_answer_approval_swallows_keyerror(booted: Booted) -> None:
    booted.fake.broker.raise_key_error = True
    booted.adapter.answer_approval("gone", "deny")
    assert await asyncio.to_thread(booted.fake.broker.answered.wait, 5.0)
    # The runtime loop survived the KeyError: a later proxy still works.
    result = await asyncio.wait_for(booted.adapter.interrupt(), timeout=5)
    assert result is SENTINELS["interrupt"]


# ---------------------------------------------------------------------------
# T15 — broker-change presentation dedupes per ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_broker_change_presents_once(booted: Booted) -> None:
    adapter, fake, app = booted.adapter, booted.fake, booted.app
    ticket_a = FakeTicket("t-a", "Allow the thing?", ("Allow", "Deny"))
    fake.broker.head = ticket_a

    def fire(times: int) -> None:
        for _ in range(times):
            adapter._on_broker_change()  # fires on the runtime thread

    # Dedupe is synchronous inside _on_broker_change: the second call
    # sees _presented already set and never schedules a presentation.
    await asyncio.to_thread(fire, 2)
    await _wait_until(lambda: len(app.approvals) >= 1)
    assert app.approvals == [("t-a", "Allow the thing?", ("Allow", "Deny"))]
    assert adapter._presented == "t-a"

    fake.broker.head = None
    await asyncio.to_thread(fire, 1)
    assert adapter._presented is None  # cleared synchronously, no present

    ticket_b = FakeTicket("t-b", "Again?", ("Allow",))
    fake.broker.head = ticket_b
    await asyncio.to_thread(fire, 1)
    await _wait_until(lambda: len(app.approvals) >= 2)
    assert app.approvals[1] == ("t-b", "Again?", ("Allow",))


# ---------------------------------------------------------------------------
# T16 — boot progress hops to the app loop; no-op without app/loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_progress_hops_to_app(booted: Booted) -> None:
    adapter, app = booted.adapter, booted.app
    await asyncio.to_thread(adapter._boot_progress, "modules", "preparing")
    await _wait_until(lambda: len(app.progress) >= 1)
    assert app.progress == [("modules", "preparing")]


def test_boot_progress_noop_without_app_or_loop() -> None:
    # No app, no loop (never started): silently drops.
    RealRuntimeAdapter(bundle="x")._boot_progress("a", "d")

    # App attached but no app loop yet: also drops, synchronously.
    adapter = RealRuntimeAdapter(bundle="x")
    app = FakeApp()
    adapter.attach(app)
    adapter._boot_progress("a", "d")
    assert app.progress == []


# ---------------------------------------------------------------------------
# T17 — mode + permission resolvers delegate to the app, else fall back
# ---------------------------------------------------------------------------


def test_mode_and_resolvers_delegate_to_app() -> None:
    adapter = RealRuntimeAdapter(bundle="x")
    app = FakeApp()
    app.mode_id = "plan"
    adapter.attach(app)

    assert adapter._current_mode() == "plan"

    decision = adapter._resolve_permission("bash", {"command": "ls"})
    assert decision is app.permissions.call_decision
    assert app.permissions.resolve_calls == [("bash", {"command": "ls"})]

    capability = adapter._resolve_capability(CapabilityClass.EXEC)
    assert capability is app.permissions.capability_decision
    assert app.permissions.capability_calls == [CapabilityClass.EXEC]


def test_mode_and_resolvers_fall_back_without_app() -> None:
    adapter = RealRuntimeAdapter(bundle="x")  # app is None
    assert adapter._current_mode() == "auto"

    decision = adapter._resolve_permission("read_file", None)
    assert isinstance(decision, TrustDecision)

    capability = adapter._resolve_capability(CapabilityClass.READ)
    assert isinstance(capability, TrustDecision)
    assert capability.decision == "allow"  # auto mode statically allows reads


# ---------------------------------------------------------------------------
# T18 — live-loop shutdown joins the thread and runs cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_live_loop_joins_and_cleans_up(booted: Booted) -> None:
    adapter, fake = booted.adapter, booted.fake
    adapter.shutdown()
    assert adapter._thread is not None
    assert not adapter._thread.is_alive()  # joined within the 8s bound
    assert fake.cleanup_called.is_set()


@pytest.mark.asyncio
async def test_shutdown_swallows_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingCleanup(FakeRealRuntime):
        async def cleanup(self) -> None:
            self.cleanup_called.set()
            raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(SEAM, ExplodingCleanup)
    adapter = RealRuntimeAdapter(bundle="x")
    adapter.attach(FakeApp())
    try:
        await asyncio.wait_for(adapter.start(lambda: None), timeout=10)
        fake = cast(FakeRealRuntime, adapter._runtime)
        adapter.shutdown()
        assert fake.cleanup_called.is_set()
        assert adapter._thread is not None
        assert not adapter._thread.is_alive()  # error swallowed, thread exits
    finally:
        adapter.shutdown()  # idempotent


# ---------------------------------------------------------------------------
# T19 — evidence links delegate to the runtime's collector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_links_delegates(booted: Booted) -> None:
    result = booted.adapter.evidence_links("the answer")
    assert result is booted.fake.evidence.sentinel
    assert booted.fake.evidence.calls == ["the answer"]
