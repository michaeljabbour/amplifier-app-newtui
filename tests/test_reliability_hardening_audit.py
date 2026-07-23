"""Reliability hardening pass — audit 2026-07-22 (issue #22).

One focused test per audited asymmetric-defense bug:

1. Compaction token-observe task keeps a strong ref (no mid-flight GC).
2. Runtime-cleanup teardown logs instead of being fully silent.
3. SteeringQueue / NeedsYouQueue mutate under a lock (two event loops).
4. A corrupt-on-resume transcript raises a recovery signal (not a silent []).
5. A malformed settings scope is reported so the boot can surface a notice.
6. The dead ``ApprovalBroker.defer`` path is gone.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.approval import ApprovalBroker, ApprovalTicket
from amplifier_app_newtui.kernel.config import (
    SettingsPaths,
    load_merged_settings,
    load_merged_settings_reporting,
    malformed_settings_notice,
)
from amplifier_app_newtui.kernel.events import ProviderResponseUsage
from amplifier_app_newtui.kernel.persistence import (
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    SessionStore,
)
from amplifier_app_newtui.kernel.runtime import RealRuntime
from amplifier_app_newtui.model.queues import NeedsYouQueue, SteeringQueue
from amplifier_app_newtui.ui.runtime_adapter import RealRuntimeAdapter


# -- Item 1: compaction task GC risk ----------------------------------------


@pytest.mark.asyncio
async def test_compaction_observe_task_is_kept_then_discarded() -> None:
    """The bridge tap must hold a strong ref to the fire-and-forget
    ``observe_input_tokens`` task; a bare ``create_task`` result is
    GC-eligible and could be collected mid-flight (contrast recipes.py)."""
    runtime = RealRuntime()
    observed: list[int] = []

    class _Binding:
        async def observe_input_tokens(self, tokens: int) -> None:
            observed.append(tokens)

    runtime._compaction_binding = _Binding()  # type: ignore[assignment]
    runtime._tap(ProviderResponseUsage(session_id="s", input_tokens=1234))

    # In flight: the ref is held (a bare create_task would leave it GC-able).
    assert len(runtime._background_tasks) == 1
    task = next(iter(runtime._background_tasks))
    await task
    assert observed == [1234]
    # The done-callback discards the ref so the set never leaks.
    assert runtime._background_tasks == set()


@pytest.mark.asyncio
async def test_compaction_observe_noop_without_binding() -> None:
    runtime = RealRuntime()
    runtime._tap(ProviderResponseUsage(session_id="s", input_tokens=10))
    assert runtime._background_tasks == set()


# -- Item 2: silent cleanup swallow -----------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failure_is_logged_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only fully-silent ``except: pass`` now logs (with traceback)."""
    adapter = RealRuntimeAdapter(bundle=None)

    class _BoomRuntime:
        async def cleanup(self) -> None:
            raise RuntimeError("teardown boom")

    with caplog.at_level(logging.DEBUG, logger="amplifier_app_newtui.ui.runtime_adapter"):
        await adapter._safe_cleanup(_BoomRuntime())

    assert any("cleanup failed" in record.getMessage() for record in caplog.records), (
        "cleanup crash must leave a debug-level trace, not vanish"
    )


# -- Item 3: queue races (one lock per queue) -------------------------------


def test_queues_expose_a_lock() -> None:
    assert hasattr(SteeringQueue()._lock, "acquire")
    assert hasattr(NeedsYouQueue()._lock, "acquire")


def test_needs_you_concurrent_defer_ids_unique_and_no_loss() -> None:
    """100 concurrent defers (== the queue bound) across 10 threads: the
    lock keeps id allocation atomic (no duplicate ids) and every append
    lands (no lost items). Without the lock the read-modify-write on
    ``_next_id`` / ``_items`` can duplicate ids or drop items."""
    queue = NeedsYouQueue()
    threads_n, per_thread = 10, 10
    barrier = threading.Barrier(threads_n)
    collected: list[str] = []
    collected_lock = threading.Lock()

    def worker(thread_id: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            try:
                item = queue.defer(f"q-{thread_id}-{i}")
            except ValueError:
                continue
            with collected_lock:
                collected.append(item.decision_id)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(collected) == threads_n * per_thread
    assert len(collected) == len(set(collected))  # atomic id allocation
    assert len(queue.items) == len(collected)  # no lost appends


def test_steering_concurrent_enqueue_ids_unique_and_no_loss() -> None:
    queue = SteeringQueue()
    threads_n, per_thread = 4, 8  # 32 == MAX_QUEUE_ITEMS
    barrier = threading.Barrier(threads_n)
    collected: list[str] = []
    collected_lock = threading.Lock()

    def worker(thread_id: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            try:
                message = queue.enqueue(f"s-{thread_id}-{i}")
            except ValueError:
                continue
            with collected_lock:
                collected.append(message.message_id)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(collected) == threads_n * per_thread
    assert len(collected) == len(set(collected))
    assert len(queue.pending) == len(collected)


# -- Item 4: empty-resume notice --------------------------------------------


def _session_dir(store: SessionStore, session_id: str) -> Path:
    session_dir = store.session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def test_transcript_recovery_failed_on_corrupt_main_and_backup(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session_dir = _session_dir(store, "s1")
    (session_dir / TRANSCRIPT_FILENAME).write_text("{ not json", encoding="utf-8")
    (session_dir / (TRANSCRIPT_FILENAME + ".backup")).write_text("also broken", encoding="utf-8")

    transcript, _metadata = store.load("s1")

    assert transcript == []
    assert store.transcript_recovery_failed is True


def test_transcript_recovery_flag_false_when_valid(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    store.save("s1", [{"role": "user", "content": "hi"}], {"session_id": "s1"})

    transcript, _metadata = store.load("s1")

    assert transcript == [{"role": "user", "content": "hi"}]
    assert store.transcript_recovery_failed is False


def test_transcript_recovery_flag_false_when_no_transcript(tmp_path: Path) -> None:
    """A fresh session with no transcript file is NOT a recovery failure."""
    store = SessionStore(base_dir=tmp_path / "sessions")
    session_dir = _session_dir(store, "s1")
    (session_dir / METADATA_FILENAME).write_text('{"session_id": "s1"}', encoding="utf-8")

    transcript, _metadata = store.load("s1")

    assert transcript == []
    assert store.transcript_recovery_failed is False


def test_transcript_recovers_from_backup_without_flag(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session_dir = _session_dir(store, "s1")
    (session_dir / TRANSCRIPT_FILENAME).write_text("{ corrupt", encoding="utf-8")
    (session_dir / (TRANSCRIPT_FILENAME + ".backup")).write_text(
        json.dumps({"role": "user", "content": "ok"}) + "\n", encoding="utf-8"
    )

    transcript, _metadata = store.load("s1")

    assert transcript == [{"role": "user", "content": "ok"}]
    assert store.transcript_recovery_failed is False


# -- Item 5: malformed-settings notice --------------------------------------


def test_malformed_settings_scope_is_reported(tmp_path: Path) -> None:
    good = tmp_path / "settings.yaml"
    good.write_text("valid: true\n", encoding="utf-8")
    bad = tmp_path / "project.yaml"
    bad.write_text("key: [1, 2\n", encoding="utf-8")  # unterminated flow → parse error
    missing = tmp_path / "local.yaml"
    paths = SettingsPaths(global_settings=good, project_settings=bad, local_settings=missing)

    merged, malformed = load_merged_settings_reporting(paths)

    assert merged == {"valid": True}  # the good scope still merges
    assert malformed == (bad,)  # only the broken scope is reported
    notice = malformed_settings_notice(malformed)
    assert notice is not None
    assert "project.yaml" in notice
    # The back-compat wrapper drops the report but returns the same dict.
    assert load_merged_settings(paths) == {"valid": True}


def test_no_settings_notice_when_all_valid(tmp_path: Path) -> None:
    good = tmp_path / "settings.yaml"
    good.write_text("a: 1\n", encoding="utf-8")
    paths = SettingsPaths(
        global_settings=good,
        project_settings=tmp_path / "missing1.yaml",
        local_settings=tmp_path / "missing2.yaml",
    )

    merged, malformed = load_merged_settings_reporting(paths)

    assert merged == {"a": 1}
    assert malformed == ()
    assert malformed_settings_notice(malformed) is None


# -- Item 6: delete dead ApprovalBroker.defer -------------------------------


def test_approval_broker_defer_is_deleted() -> None:
    """The dead broker-side defer path (no production callers) is gone."""
    assert not hasattr(ApprovalBroker, "defer")


def test_approval_ticket_has_no_deferred_fields() -> None:
    fields = ApprovalTicket.__dataclass_fields__
    assert "deferred" not in fields
    assert "decision_id" not in fields
