"""Clipboard image capture + multimodal injection (kernel/clipboard.py)."""

from __future__ import annotations

import base64

import pytest

from amplifier_app_newtui.kernel.clipboard import (
    ClipboardImageInjector,
    ImageAttachment,
    build_image_message,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def test_image_attachment_validates_content_type() -> None:
    ImageAttachment(data=_PNG, media_type="image/png")  # ok
    with pytest.raises(ValueError):
        ImageAttachment(data=_PNG, media_type="image/jpeg")  # bytes ≠ declared
    with pytest.raises(ValueError):
        ImageAttachment(data=b"", media_type="image/png")  # empty


def test_build_image_message_shape() -> None:
    msg = build_image_message([ImageAttachment(_PNG, "image/png")], text="see this")
    assert msg["role"] == "user"
    assert msg["content"][0] == {"type": "text", "text": "see this"}
    src = msg["content"][1]["source"]
    assert src == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(_PNG).decode("ascii"),
    }
    assert msg["metadata"]["attachment_count"] == 1


class _FakeContext:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    async def get_messages(self) -> list[dict]:
        return list(self._messages)

    async def set_messages(self, messages: list[dict]) -> None:
        self._messages = messages


@pytest.mark.asyncio
async def test_injector_rewrites_matching_user_message_to_multimodal() -> None:
    ctx = _FakeContext([{"role": "user", "content": "look at this"}])
    injector = ClipboardImageInjector(ctx)
    injector.prepare("look at this", (ImageAttachment(_PNG, "image/png"),))
    result = await injector.handle_provider_request("provider:request", {})
    assert result.action == "continue"
    rewritten = ctx._messages[-1]
    assert isinstance(rewritten["content"], list)
    assert rewritten["content"][1]["type"] == "image"
    assert injector._pending is None  # cleared after inject


@pytest.mark.asyncio
async def test_injector_noop_without_pending() -> None:
    ctx = _FakeContext([{"role": "user", "content": "hi"}])
    result = await ClipboardImageInjector(ctx).handle_provider_request("provider:request", {})
    assert result.action == "continue"
    assert ctx._messages[-1]["content"] == "hi"  # untouched
