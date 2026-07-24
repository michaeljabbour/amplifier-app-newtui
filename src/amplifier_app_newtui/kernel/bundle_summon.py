"""Agent-summonable deferred bundles: discovery catalog + a ``load_bundle`` tool.

PR #132 added ``bundle.deferred`` — overlays held back from boot for speed and
composed in-session via the ``/bundle load <name>`` *command*. The gap this
module closes: the **model** had no awareness those overlays exist and could
not summon one when a task needed it. Zero-regret deferral means the agent can
pull a deferred behavior bundle in mid-turn, not just the human.

Two halves, both app-level and both no-ops unless ``bundle.deferred`` is
configured (backward compatible — an unconfigured app injects nothing and
offers no tool):

1. **Discovery** — :func:`build_deferred_catalog` names each held-back overlay
   (registry name if known, else a name derived from its URI) and a one-line
   description read cheaply from the bundle's front matter when it resolves to
   a local file (remote URIs stay offline at boot — deferral must not pay a
   network cost to describe what it deferred). :class:`DeferredCatalogInjector`
   keeps that catalog present as ONE system message in the root context, using
   the same direct-context-edit seam as
   :class:`~amplifier_app_newtui.kernel.surface_hint.SurfaceHintInjector` (so it
   survives ``/clear`` / compaction and never collides with the steering
   bridge's persistent ``inject_context`` at ``provider:request``).

2. **Summon** — :class:`LoadBundleTool` is a host-provided
   :class:`~amplifier_core.interfaces.Tool` the model can call. Its
   ``execute`` routes straight to the runtime's ``load_deferred_bundle`` seam
   (the same one ``/bundle load`` drives), so a summon composes exactly what a
   manual load does — additive tools/hooks/agents mount live, while single-slot
   modules (providers/orchestrator/context) are reported as *available next
   session start* rather than pretending to hot-swap (foundation's
   ``initialize()`` boundary; the honesty is #132's, reused verbatim through the
   returned detail string).

Everything here is duck-typed over the coordinator's ``mount("tools", …)`` seam
(the exact contract foundation itself uses to mount a Python tool — see
``amplifier_core.loader_grpc``) and a plain ``load`` callback, so it unit-tests
with fakes: no real session, no network.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from amplifier_core import HookResult

from .config import added_bundle_uris, discover_bundle

logger = logging.getLogger(__name__)

LOAD_BUNDLE_TOOL_NAME = "load_bundle"
"""Mount name (and model-facing tool name) of the summon tool."""

DEFERRED_CATALOG_SOURCE = "newtui-deferred-catalog"
"""Metadata marker identifying the single managed catalog system message."""


# --------------------------------------------------------------------------
# Pure discovery: build the catalog from what is cheaply available at boot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeferredBundleEntry:
    """One summonable deferred overlay: how the model refers to it + why.

    ``name`` is the argument the model passes to ``load_bundle`` (and to
    ``resolve_deferred_bundle`` behind it): a ``bundle.added`` registry name
    when one maps to this URI, else a name derived from the URI itself.
    ``description`` is a one-line summary (bundle front matter when local,
    else empty — the name carries the meaning)."""

    name: str
    uri: str
    description: str = ""


def _name_from_uri(uri: str) -> str:
    """Derive a stable, human-ish name from an overlay URI.

    Strips the ``git+``/``zip+`` scheme prefix and any ``#fragment`` /
    ``@ref``, then takes the last path segment (``…/amplifier-bundle-x@main``
    → ``amplifier-bundle-x``). A bare name/path falls through as its own
    basename. Never raises — a summon name is always derivable."""
    cleaned = uri
    for prefix in ("git+", "zip+"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.split("#", 1)[0]
    cleaned = cleaned.rstrip("/")
    segment = cleaned.rsplit("/", 1)[-1] if "/" in cleaned else cleaned
    segment = segment.split("@", 1)[0]
    return segment or uri


def _reverse_added(settings: dict[str, Any]) -> dict[str, str]:
    """URI → registered ``bundle.added`` name (first registration wins)."""
    reverse: dict[str, str] = {}
    for name, uri in added_bundle_uris(settings).items():
        reverse.setdefault(uri, name)
    return reverse


def read_local_bundle_summary(path: Path) -> tuple[str, str]:
    """A ``(name, description)`` pair read from a local bundle file's front matter.

    Bundle ``.md`` files carry a YAML front-matter block delimited by ``---``
    lines with a ``bundle:`` section (``name`` / ``description``); a
    ``bundle.yaml`` is plain YAML. This reads only the front matter — no
    foundation load, no module install — so describing a deferred bundle never
    costs what deferring it saved. The description is flattened to its first
    non-empty line. Returns ``("", "")`` on any miss (unreadable, no front
    matter, no ``bundle`` section) — never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ("", "")
    front = text
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        rest = stripped[3:]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError:
        return ("", "")
    if not isinstance(data, dict):
        return ("", "")
    bundle = data.get("bundle")
    if not isinstance(bundle, dict):
        return ("", "")
    name = str(bundle.get("name") or "").strip()
    description = str(bundle.get("description") or "").strip()
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    return (name, first_line)


def build_deferred_catalog(
    deferred_uris: tuple[str, ...] | list[str],
    settings: dict[str, Any],
    search_paths: tuple[Path, ...] | list[Path],
) -> tuple[DeferredBundleEntry, ...]:
    """Build the summonable-bundle catalog from the deferred overlay URIs.

    For each URI: the display name is the ``bundle.added`` registry name when
    one maps to it, else derived from the URI (:func:`_name_from_uri`). A local
    file resolution (via :func:`~amplifier_app_newtui.kernel.config.discover_bundle`)
    contributes a one-line description from front matter, and its own declared
    name wins over a URI-derived one; remote-only overlays keep the derived
    name and an empty description (cheap, offline). Order follows
    *deferred_uris* so the catalog matches the boot notice's listing."""
    reverse = _reverse_added(settings)
    entries: list[DeferredBundleEntry] = []
    for uri in deferred_uris:
        registered = reverse.get(uri)
        name = registered or _name_from_uri(uri)
        description = ""
        local = discover_bundle(uri, search_paths)
        if local is not None and Path(local).is_file():
            file_name, file_description = read_local_bundle_summary(Path(local))
            description = file_description
            if not registered and file_name:
                name = file_name
        entries.append(DeferredBundleEntry(name=name, uri=uri, description=description))
    return tuple(entries)


def catalog_instruction_text(
    entries: tuple[DeferredBundleEntry, ...] | list[DeferredBundleEntry],
) -> str:
    """The model-facing catalog block naming every summonable deferred bundle.

    Teaches the model that these overlays were held back for fast boot, lists
    each ``name`` (± one-line description), and points at the ``load_bundle``
    tool with the honest boundary: additive tools/hooks mount live, single-slot
    modules attach at the next session start. Empty string when nothing is
    deferred (caller injects nothing)."""
    if not entries:
        return ""
    lines = [
        "Deferred bundles are available to summon this session (held back from "
        "boot for speed). When a task needs one, call the `load_bundle` tool "
        "with its name to mount that bundle's additional tools and hooks into "
        "this live session:",
    ]
    for entry in entries:
        if entry.description:
            lines.append(f"- {entry.name}: {entry.description}")
        else:
            lines.append(f"- {entry.name}")
    lines.append(
        "Additive tools/hooks/agents attach immediately (visible next turn); "
        "provider/orchestrator/context modules a bundle carries cannot hot-swap "
        "and take effect only at the next session start."
    )
    return "\n".join(lines)


def _catalog_message(text: str) -> dict[str, Any]:
    return {
        "role": "system",
        "content": text,
        "metadata": {"source": DEFERRED_CATALOG_SOURCE},
    }


def _is_catalog(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and metadata.get("source") == DEFERRED_CATALOG_SOURCE


# --------------------------------------------------------------------------
# Discovery injector: keep ONE catalog system message in the root context
# --------------------------------------------------------------------------


class DeferredCatalogInjector:
    """Keep one deferred-bundle catalog message present in the root context.

    Mirrors :class:`~amplifier_app_newtui.kernel.surface_hint.SurfaceHintInjector`:
    a root-only ``provider:request`` hook that edits the context directly and
    returns ``continue`` (never ``inject_context`` — that would collide with the
    steering bridge's persistent injection under one ephemeral flag). The
    catalog text is static per session, so the reconcile is a presence check:
    insert it once, re-insert it if ``/clear`` or compaction dropped it, and
    otherwise leave the context untouched."""

    EVENTS = ("provider:request",)

    def __init__(self, root_session_id: str, text: str, context: Any) -> None:
        self._root_session_id = root_session_id
        self._text = text
        self._context = context

    def _can_edit(self) -> bool:
        return all(hasattr(self._context, m) for m in ("get_messages", "set_messages"))

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        if event != "provider:request" or not self._text or not self._can_edit():
            return HookResult(action="continue")
        session_id = str(data.get("session_id") or self._root_session_id)
        if session_id != self._root_session_id:
            # Subagents run their own bundle stack; the catalog is a root-only
            # affordance (the human-facing session summons, children inherit).
            return HookResult(action="continue")
        messages = list(await self._context.get_messages())
        if any(_is_catalog(message) for message in messages):
            return HookResult(action="continue")  # already present: no write
        # Insert right after the leading system block, before the dialogue.
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].get("role") == "system":
            insert_at += 1
        messages.insert(insert_at, _catalog_message(self._text))
        await self._context.set_messages(messages)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 930) -> Callable[[], None]:
        unregister = hooks.register(
            "provider:request",
            self.handle_event,
            priority=priority,
            name="newtui-deferred-catalog",
        )
        if not callable(unregister):
            return lambda: None

        def unregister_hook() -> None:
            unregister()

        return unregister_hook


# --------------------------------------------------------------------------
# Summon: a host-provided tool the model calls to load a deferred bundle
# --------------------------------------------------------------------------


class LoadBundleTool:
    """Host-provided ``load_bundle`` tool routing to ``load_deferred_bundle``.

    Satisfies the :class:`~amplifier_core.interfaces.Tool` protocol (``name`` /
    ``description`` / ``input_schema`` / ``execute``) so it mounts onto the live
    coordinator's ``tools`` point exactly like a module-loaded tool. ``execute``
    delegates to the *load* callback (the runtime's ``load_deferred_bundle``,
    ``async (name) -> (ok, detail)``) and surfaces its result — including #132's
    honest "attach at next session start" boundary for single-slot modules —
    straight back to the model as the tool result."""

    def __init__(
        self,
        load: Callable[[str], Awaitable[tuple[bool, str]]],
        catalog: tuple[DeferredBundleEntry, ...] | list[DeferredBundleEntry],
    ) -> None:
        self._load = load
        self._catalog = tuple(catalog)

    @property
    def name(self) -> str:
        return LOAD_BUNDLE_TOOL_NAME

    @property
    def description(self) -> str:
        names = ", ".join(entry.name for entry in self._catalog) or "none"
        return (
            "Summon a deferred bundle into this live session, mounting its "
            "additional tools and hooks so you can use them for the current "
            "task. Deferred bundles were held back from session boot for speed. "
            f"Available to summon: {names}. Additive tools/hooks take effect on "
            "the next turn; provider/orchestrator/context modules a bundle "
            "carries attach only at the next session start."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the deferred bundle to summon, as listed in the "
                        "deferred-bundle catalog / this tool's description."
                    ),
                }
            },
            "required": ["name"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:  # noqa: A002 — Tool protocol arg name
        from amplifier_core.models import ToolResult

        name = str((input or {}).get("name", "")).strip()
        if not name:
            available = ", ".join(entry.name for entry in self._catalog) or "none"
            message = f"load_bundle requires a 'name' · deferred bundles: {available}"
            return ToolResult(success=False, error={"message": message}, output=message)
        try:
            ok, detail = await self._load(name)
        except Exception as error:  # noqa: BLE001 — a summon failure is a tool miss, never a crash
            logger.warning("load_bundle summon failed for %s", name, exc_info=True)
            message = f"could not summon '{name}': {error or type(error).__name__}"
            return ToolResult(success=False, error={"message": message}, output=message)
        if ok:
            return ToolResult(success=True, output=detail)
        return ToolResult(success=False, error={"message": detail}, output=detail)


__all__ = [
    "DEFERRED_CATALOG_SOURCE",
    "LOAD_BUNDLE_TOOL_NAME",
    "DeferredBundleEntry",
    "DeferredCatalogInjector",
    "LoadBundleTool",
    "build_deferred_catalog",
    "catalog_instruction_text",
    "read_local_bundle_summary",
]
