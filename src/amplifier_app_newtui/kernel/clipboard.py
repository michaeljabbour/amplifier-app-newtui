"""Clipboard image capture + multimodal message injection.

Ported from amplifier-app-cli (``ui/clipboard.py``): the kernel's
``session.execute(prompt: str)`` is text-only, so pasted images can't ride
the prompt. Instead ``read_clipboard_image`` pulls image bytes straight
from the system clipboard (no temp file), and :class:`ClipboardImageInjector`
registers a ``provider:request`` hook that rewrites the just-submitted user
message to multimodal content (text + base64 image blocks) in the context
right before the provider call — so ``execute`` never changes.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import selectors
import shutil
import subprocess  # nosec B404 — fixed clipboard helpers, never a shell
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, TypeAlias

from amplifier_core import HookResult

ImageMediaType: TypeAlias = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]

DEFAULT_CLIPBOARD_TIMEOUT_SECONDS = 2.0
MAX_CLIPBOARD_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CLIPBOARD_ATTACHMENTS = 4
MAX_CLIPBOARD_TOTAL_BYTES = 32 * 1024 * 1024

_MACOS_PNG_DATA_RE = re.compile(rb"PNGf([0-9a-fA-F]+)")


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """Validated image bytes read from the system clipboard."""

    data: bytes
    media_type: ImageMediaType

    def __post_init__(self) -> None:
        if not self.data or len(self.data) > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("image attachment exceeds the allowed size")
        if _detect_image_media_type(self.data) != self.media_type:
            raise ValueError("image attachment type does not match its content")


def build_image_message(
    attachments: Iterable[ImageAttachment],
    *,
    text: str = "Clipboard images attached to the next user message.",
) -> dict[str, Any]:
    """Build a provider-neutral multimodal user message for images."""
    images = tuple(attachments)
    if not images:
        raise ValueError("at least one image attachment is required")
    if len(images) > MAX_CLIPBOARD_ATTACHMENTS:
        raise ValueError("too many image attachments")
    if sum(len(image.data) for image in images) > MAX_CLIPBOARD_TOTAL_BYTES:
        raise ValueError("image attachments exceed the aggregate size limit")

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content.extend(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            },
        }
        for image in images
    )
    return {
        "role": "user",
        "content": content,
        "metadata": {"source": "newtui-clipboard", "attachment_count": len(images)},
    }


class ClipboardImageInjector:
    """Upgrade the next matching user prompt to multimodal content.

    Registered as a ``provider:request`` hook. ``prepare(prompt, images)``
    stashes the pending images; when the provider request fires, the hook
    finds the matching text-only user message in the context and replaces
    its content with the multimodal version, so the model receives the
    image without ``execute`` ever carrying it.
    """

    def __init__(self, context: Any) -> None:
        self._context = context
        self._pending: tuple[str, tuple[ImageAttachment, ...]] | None = None

    def prepare(self, prompt: str, attachments: Iterable[ImageAttachment]) -> None:
        images = tuple(attachments)
        if not images:
            return
        if not all(hasattr(self._context, m) for m in ("get_messages", "set_messages")):
            raise RuntimeError("Session context cannot accept image attachments")
        if self._pending is not None:
            raise RuntimeError("An image submission is already pending")
        self._pending = (prompt, images)

    def clear(self) -> None:
        self._pending = None

    async def handle_provider_request(self, _event: str, _data: dict[str, Any]) -> HookResult:
        if self._pending is None:
            return HookResult(action="continue")
        prompt, images = self._pending
        messages = list(await self._context.get_messages())
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "user" and message.get("content") == prompt:
                image_message = build_image_message(images, text=prompt)
                metadata = message.get("metadata")
                messages[index] = {
                    **message,
                    "content": image_message["content"],
                    "metadata": {
                        **(metadata if isinstance(metadata, dict) else {}),
                        **image_message["metadata"],
                    },
                }
                await self._context.set_messages(messages)
                self.clear()
                return HookResult(action="continue")
        return HookResult(
            action="deny",
            reason="Could not attach clipboard images to the submitted prompt",
        )


def read_clipboard_image(
    *,
    timeout_seconds: float = DEFAULT_CLIPBOARD_TIMEOUT_SECONDS,
    max_bytes: int = MAX_CLIPBOARD_IMAGE_BYTES,
) -> ImageAttachment | None:
    """Read an image from the system clipboard without writing it to disk.

    Returns ``None`` when the clipboard has no supported image, the platform
    or required command is unavailable, or extraction fails.
    """
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("timeout_seconds and max_bytes must be positive")
    command = _clipboard_command()
    if command is None:
        return None
    raw_limit = max_bytes * 2 + 1024 if sys.platform == "darwin" else max_bytes
    output = _read_command_output(command, timeout_seconds=timeout_seconds, max_bytes=raw_limit)
    if not output:
        return None
    if sys.platform == "darwin":
        data = _decode_macos_png(output, max_bytes=max_bytes)
    else:
        data = output if len(output) <= max_bytes else None
    if not data:
        return None
    media_type = _detect_image_media_type(data)
    if media_type is None:
        return None
    return ImageAttachment(data=data, media_type=media_type)


def _read_command_output(
    command: list[str], *, timeout_seconds: float, max_bytes: int
) -> bytes | None:
    """Read a fixed clipboard helper with hard time and output bounds."""
    try:
        process = subprocess.Popen(  # nosec B603
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
    except (FileNotFoundError, OSError):
        return None
    selector = selectors.DefaultSelector()
    data = bytearray()
    deadline = monotonic() + timeout_seconds
    try:
        if process.stdout is None:
            return None
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            if not selector.select(remaining):
                return None
            chunk = os.read(process.stdout.fileno(), min(65_536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
        remaining = max(0.0, deadline - monotonic())
        return bytes(data) if process.wait(timeout=remaining) == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _clipboard_command() -> list[str] | None:
    if sys.platform == "darwin":
        return ["osascript", "-e", "get the clipboard as «class PNGf»"]
    if not sys.platform.startswith("linux"):
        return None
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))
    if (wayland or not x11) and shutil.which("wl-paste"):
        return ["wl-paste", "-t", "image"]
    if (x11 or not wayland) and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    return None


def _decode_macos_png(output: bytes, *, max_bytes: int) -> bytes | None:
    match = _MACOS_PNG_DATA_RE.search(output)
    if match is None:
        return None
    encoded = match.group(1)
    if len(encoded) % 2 or len(encoded) // 2 > max_bytes:
        return None
    try:
        return binascii.unhexlify(encoded)
    except (binascii.Error, ValueError):
        return None


def _detect_image_media_type(data: bytes) -> ImageMediaType | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = [
    "ClipboardImageInjector",
    "ImageAttachment",
    "ImageMediaType",
    "MAX_CLIPBOARD_ATTACHMENTS",
    "MAX_CLIPBOARD_IMAGE_BYTES",
    "MAX_CLIPBOARD_TOTAL_BYTES",
    "build_image_message",
    "read_clipboard_image",
]
