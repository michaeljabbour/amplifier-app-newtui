from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from amplifier_foundation.bundle import Bundle, PreparedBundle

from amplifier_app_tui.kernel.bundle_content import (
    LIVE_BUNDLE_CONTENT_KIND,
    activate_bundle_content,
)


class _Context:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.add_calls = 0

    async def add_message(self, message: dict) -> None:
        self.add_calls += 1
        self.messages.append(message)

    async def get_messages(self) -> list[dict]:
        return list(self.messages)

    async def set_messages(self, messages: list[dict]) -> None:
        self.messages = list(messages)


class _Coordinator:
    def __init__(self, context: object | None = None) -> None:
        self.context = context
        self.hooks: object | None = None

    def get(self, mount: str):
        return self.context if mount == "context" else None


class _Prepared:
    def __init__(self, rendered: str = "Instruction\n\n<context>notes</context>") -> None:
        self.bundle = SimpleNamespace(name="review", instruction="Instruction")
        self.rendered = rendered
        self.calls: list[tuple[object, object, Path | None]] = []

    def _create_system_prompt_factory(
        self, bundle: object, session: object, session_cwd: Path | None = None
    ):
        self.calls.append((bundle, session, session_cwd))

        async def render() -> str:
            return self.rendered

        return render


def test_private_foundation_factory_renders_one_hook_message_and_cleans_up(
    tmp_path: Path,
) -> None:
    prepared = _Prepared()
    context = _Context()
    coordinator = _Coordinator(context)
    session = SimpleNamespace(coordinator=coordinator)

    activation = asyncio.run(activate_bundle_content(prepared, coordinator, session, tmp_path))

    assert activation.ok is True
    assert activation.added is True
    assert activation.rendered == "Instruction\n\n<context>notes</context>"
    assert context.add_calls == 1
    assert len(context.messages) == 1
    message = context.messages[0]
    assert message["role"] == "system"
    assert message["content"] == activation.rendered
    assert message["metadata"]["source"] == "hook"
    assert message["metadata"]["kind"] == LIVE_BUNDLE_CONTENT_KIND
    assert message["metadata"]["bundle"] == "review"
    assert prepared.calls == [(prepared.bundle, session, tmp_path)]

    assert activation.cleanup is not None
    asyncio.run(activation.cleanup())
    assert context.messages == []
    asyncio.run(activation.cleanup())  # idempotent
    assert context.messages == []


def test_real_foundation_prepared_bundle_renders_instruction_and_context(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "GUIDE.md"
    guide.write_text("Real context payload.", encoding="utf-8")
    bundle = Bundle(
        name="real-overlay",
        instruction="Follow the live overlay.",
        context={"guide": guide},
        base_path=tmp_path,
    )
    prepared = PreparedBundle(mount_plan={}, resolver=object(), bundle=bundle)  # type: ignore[arg-type]

    class Hooks:
        async def emit(self, event: str, data: dict) -> None:
            del event, data

    context = _Context()
    coordinator = _Coordinator(context)
    coordinator.hooks = Hooks()
    session = SimpleNamespace(coordinator=coordinator)

    activation = asyncio.run(activate_bundle_content(prepared, coordinator, session, tmp_path))

    assert activation.ok is True
    assert activation.added is True
    assert activation.rendered.startswith("Follow the live overlay.")
    assert "Real context payload." in activation.rendered
    assert context.messages[0]["metadata"]["source"] == "hook"


def test_public_factory_is_preferred_over_private_factory(tmp_path: Path) -> None:
    class PublicPrepared(_Prepared):
        def create_system_prompt_factory(
            self, bundle: object, session: object, *, session_cwd: Path
        ):
            del bundle, session, session_cwd

            async def render() -> str:
                return "from public factory"

            return render

        def _create_system_prompt_factory(self, *args, **kwargs):
            raise AssertionError("private compatibility factory should not be used")

    context = _Context()
    activation = asyncio.run(
        activate_bundle_content(
            PublicPrepared(),
            _Coordinator(context),
            SimpleNamespace(),
            tmp_path,
        )
    )

    assert activation.ok is True
    assert context.messages[0]["content"] == "from public factory"


def test_sync_factory_and_add_message_are_supported(tmp_path: Path) -> None:
    class SyncPrepared:
        bundle = SimpleNamespace(name="sync")

        def create_system_prompt_factory(self, bundle, session):
            del bundle, session
            return lambda: "sync content"

    class SyncContext:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        def add_message(self, message: dict) -> None:
            self.messages.append(message)

    context = SyncContext()
    activation = asyncio.run(
        activate_bundle_content(SyncPrepared(), _Coordinator(context), SimpleNamespace(), tmp_path)
    )

    assert activation.ok is True
    assert activation.added is True
    assert activation.reason == "live context does not support message cleanup"
    assert activation.cleanup is None
    assert context.messages[0]["content"] == "sync content"


def test_hook_source_survives_context_simple_factory_filter(tmp_path: Path) -> None:
    context = _Context()
    context.messages = [
        {"role": "system", "content": "stale root"},
        {"role": "user", "content": "question"},
    ]
    activation = asyncio.run(
        activate_bundle_content(
            _Prepared("live overlay"),
            _Coordinator(context),
            SimpleNamespace(),
            tmp_path,
        )
    )
    assert activation.added is True

    # This is context-simple's documented factory-mode filter: replace normal
    # static system messages while retaining hook-origin injections.
    conversation = [
        message
        for message in context.messages
        if message.get("role") != "system"
        or (message.get("metadata") or {}).get("source") == "hook"
    ]
    working = [{"role": "system", "content": "fresh root"}, *conversation]

    assert [message["content"] for message in working] == [
        "fresh root",
        "question",
        "live overlay",
    ]


def test_empty_render_is_a_successful_noop(tmp_path: Path) -> None:
    context = _Context()
    activation = asyncio.run(
        activate_bundle_content(
            _Prepared("  \n"), _Coordinator(context), SimpleNamespace(), tmp_path
        )
    )

    assert activation.ok is True
    assert activation.added is False
    assert activation.reason == "bundle has no instruction or context content"
    assert context.messages == []


def test_missing_factory_fails_honestly(tmp_path: Path) -> None:
    prepared = SimpleNamespace(bundle=SimpleNamespace(name="legacy"))
    context = _Context()

    activation = asyncio.run(
        activate_bundle_content(prepared, _Coordinator(context), SimpleNamespace(), tmp_path)
    )

    assert activation.ok is False
    assert activation.added is False
    assert "no compatible system-prompt factory" in activation.reason
    assert context.messages == []


def test_missing_live_context_fails_before_render(tmp_path: Path) -> None:
    prepared = _Prepared()
    activation = asyncio.run(
        activate_bundle_content(prepared, _Coordinator(), SimpleNamespace(), tmp_path)
    )

    assert activation.ok is False
    assert activation.added is False
    assert activation.reason == "live context cannot accept messages"
    assert prepared.calls == []


def test_render_and_add_failures_are_reported_without_false_success(
    tmp_path: Path,
) -> None:
    class BrokenPrepared(_Prepared):
        def _create_system_prompt_factory(self, bundle, session, session_cwd=None):
            del bundle, session, session_cwd

            async def render() -> str:
                raise RuntimeError("mention resolution broke")

            return render

    context = _Context()
    rendered_failure = asyncio.run(
        activate_bundle_content(
            BrokenPrepared(), _Coordinator(context), SimpleNamespace(), tmp_path
        )
    )
    assert rendered_failure.ok is False
    assert "mention resolution broke" in rendered_failure.reason
    assert context.messages == []

    class RejectingContext(_Context):
        async def add_message(self, message: dict) -> None:
            del message
            raise RuntimeError("context closed")

    add_failure = asyncio.run(
        activate_bundle_content(
            _Prepared(),
            _Coordinator(RejectingContext()),
            SimpleNamespace(),
            tmp_path,
        )
    )
    assert add_failure.ok is False
    assert add_failure.added is False
    assert "context closed" in add_failure.reason
    assert add_failure.rendered.startswith("Instruction")
