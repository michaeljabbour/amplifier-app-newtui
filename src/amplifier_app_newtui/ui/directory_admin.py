"""In-session ``/allowed-dirs`` and ``/denied-dirs`` UI controller."""

from __future__ import annotations

from typing import Any, Protocol

from ..kernel.directory_permissions import DirectoryKind
from ..model.blocks import Answer, Segment


class DirectoryAdminHost(Protocol):
    adapter: Any
    allocator: Any

    def append_block(self, block: Any) -> None: ...

    def show_notice(self, text: str, duration: float | None = None) -> None: ...


def _spans(kind: DirectoryKind, entries: tuple[Any, ...]) -> tuple[Segment, ...]:
    title = "Allowed write directories" if kind == "allowed" else "Denied write directories"
    color = "green" if kind == "allowed" else "red"
    spans = [
        Segment(text="· ", style_token=color),
        Segment(text=title, style_token="bright", bold=True),
        Segment(text="\n", style_token="dim"),
    ]
    if not entries:
        spans.append(Segment(text="  none configured", style_token="dimmer"))
    else:
        for entry in entries:
            spans.append(
                Segment(
                    text=f"  {entry.path}  ({entry.scope})\n",
                    style_token="fg",
                )
            )
    spans.append(
        Segment(
            text=f"  /{kind}-dirs add <path> · remove <path>",
            style_token="dimmer",
        )
    )
    return tuple(spans)


async def manage(host: DirectoryAdminHost, kind: str, args: str) -> None:
    if kind not in ("allowed", "denied"):
        host.show_notice(f"unknown directory policy · {kind}")
        return
    typed_kind: DirectoryKind = kind
    parts = args.strip().split(maxsplit=1)
    operation = parts[0].lower() if parts else "list"
    if operation in ("", "list"):
        entries = await host.adapter.directory_entries(typed_kind)
        host.append_block(
            Answer(id=host.allocator.next_id(), spans=_spans(typed_kind, entries))
        )
        return
    if operation not in ("add", "remove") or len(parts) < 2:
        host.show_notice(f"usage: /{kind}-dirs list | add <path> | remove <path>")
        return
    ok, detail = await host.adapter.update_directory(typed_kind, operation, parts[1])
    host.show_notice(detail)
    if ok:
        entries = await host.adapter.directory_entries(typed_kind)
        host.append_block(
            Answer(id=host.allocator.next_id(), spans=_spans(typed_kind, entries))
        )


__all__ = ["manage"]
