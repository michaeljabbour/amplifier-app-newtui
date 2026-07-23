"""Segment renderers for the in-session ops commands.

``/model``, ``/status``, ``/tools``, ``/agents`` and ``/diff`` post an
:class:`~amplifier_app_newtui.model.blocks.Answer` to the transcript;
these pure functions turn the kernel result data into the flat
``Segment`` stream that block carries, matching the house style of
:func:`amplifier_app_newtui.ui.app_support.native_modes_segments`
(blue ``·`` marker, bright-bold header, dim/teal detail). Pure and
Textual-free so they unit-test as span tuples.
"""

from __future__ import annotations

from decimal import Decimal

from ..kernel.compaction import CompactionConfig
from ..kernel.session_manager import SessionSummary
from ..kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from ..model.blocks import Segment
from .live_tail import answer_spans

_DIFF_MAX_LINES = 400


def _header(label: str, detail: str) -> list[Segment]:
    return [
        Segment(text="· ", style_token="blue"),
        Segment(text=label, style_token="bright", bold=True),
        Segment(text=f"  {detail}\n", style_token="dim"),
    ]


def model_listing_spans(listing: ModelListing) -> tuple[Segment, ...]:
    """``/model`` (no arg): current model + the provider's advertised set."""
    if not listing.provider:
        return (Segment(text="  no provider mounted\n", style_token="dimmer"),)
    spans = _header("Model", f"provider {listing.provider} · /model <name> switches")
    current = listing.current or "(provider default)"
    if listing.available:
        for model in listing.available:
            is_current = model == listing.current
            spans.append(
                Segment(
                    text=f"  {'▸' if is_current else ' '} ",
                    style_token="green" if is_current else "dim",
                )
            )
            spans.append(
                Segment(
                    text=f"{model}\n",
                    style_token="green" if is_current else "teal",
                    bold=is_current,
                )
            )
    else:
        spans.append(Segment(text="  current  ", style_token="dim"))
        spans.append(Segment(text=f"{current}\n", style_token="green"))
        spans.append(Segment(text="  (provider advertises no model list)\n", style_token="dimmer"))
    return tuple(spans)


def status_spans(
    info: StatusInfo,
    *,
    mode: str,
    bundle: str,
    session_short: str,
    cost: Decimal,
    compaction: CompactionConfig,
) -> tuple[Segment, ...]:
    """``/status``: coordinator snapshot joined with app-side mode/cost."""
    session = session_short or (info.session_id[:6] if info.session_id else "—")
    spans = _header("Status", f"session {session}")
    if compaction.auto_compact is True:
        threshold = (
            f" · {compaction.compact_threshold:.0%}"
            if compaction.compact_threshold is not None
            else ""
        )
        compaction_label = f"on{threshold} · {compaction.max_tokens:,} token window"
    elif compaction.auto_compact is False:
        compaction_label = f"off · {compaction.max_tokens:,} token window"
    else:
        compaction_label = f"bundle default · {compaction.max_tokens:,} token window"
    compaction_label += f" · {compaction.accounting} accounting"
    rows: tuple[tuple[str, str], ...] = (
        ("bundle", bundle or "—"),
        ("mode", mode),
        ("provider", info.provider or "—"),
        ("model", info.model or "(default)"),
        ("effort", info.effort or "(default)"),
        ("messages", str(info.messages)),
        ("auto compact", compaction_label),
        ("tools", str(info.tools)),
        ("agents", str(len(info.agents))),
        ("cost", f"${cost:.2f}"),
    )
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        spans.append(Segment(text=f"  {label.ljust(width)}  ", style_token="dim"))
        spans.append(Segment(text=f"{value}\n", style_token="teal"))
    return tuple(spans)


def sessions_spans(
    summaries: tuple[SessionSummary, ...], *, current: str = ""
) -> tuple[Segment, ...]:
    """``/sessions``: the stored-session roster (name · id · msgs · age).

    The live session (its short id is a prefix of *current*) is marked with
    a green ▸; the rest read dim. Read-only — switching sessions is a fresh
    ``amplifier-newtui resume <id>`` (noted in the header), never an
    in-place teardown.
    """
    if not summaries:
        return (
            Segment(
                text="  no stored sessions · this project has no history yet\n",
                style_token="dimmer",
            ),
        )
    spans = list(
        _header(
            "Sessions",
            f"{len(summaries)} stored · resume: amplifier-newtui resume <id>",
        )
    )
    for summary in summaries:
        is_current = bool(current) and summary.session_id.startswith(current)
        spans.append(
            Segment(
                text="  ▸ " if is_current else "    ",
                style_token="green" if is_current else "dim",
            )
        )
        spans.append(
            Segment(
                text=f"{summary.short_id}  ",
                style_token="green" if is_current else "teal",
                bold=is_current,
            )
        )
        spans.append(
            Segment(
                text=(
                    f"{summary.name or '—'}  ·  {summary.bundle}  ·  "
                    f"{summary.messages} msgs  ·  {summary.time_ago}\n"
                ),
                style_token="dim",
            )
        )
    return tuple(spans)


def names_spans(label: str, names: tuple[str, ...], empty: str) -> tuple[Segment, ...]:
    """A simple bulleted roster for ``/tools`` and ``/agents``."""
    if not names:
        return (Segment(text=f"  {empty}\n", style_token="dimmer"),)
    spans = _header(label, f"{len(names)} mounted")
    for name in names:
        spans.append(Segment(text="  • ", style_token="dim"))
        spans.append(Segment(text=f"{name}\n", style_token="teal"))
    return tuple(spans)


def skills_spans(skills: tuple[SkillInfo, ...]) -> tuple[Segment, ...]:
    """``/skills``: the available-skills roster (name + one-line description)."""
    if not skills:
        return (
            Segment(
                text="  no skills · add sources under .amplifier/skills/ or ~/.amplifier/skills/\n",
                style_token="dimmer",
            ),
        )
    spans = _header("Skills", f"{len(skills)} available · /skill <name> loads one")

    def label(s: SkillInfo) -> str:
        # A shortcut alias reads as its slash trigger (story #1: /cosam).
        return f"{s.name} (/{s.shortcut})" if s.shortcut else s.name

    width = max(len(label(s)) for s in skills)
    for skill in skills:
        spans.append(Segment(text=f"  {label(skill).ljust(width)}  ", style_token="teal"))
        desc = " ".join(skill.description.split())[:90]
        spans.append(Segment(text=f"{desc}\n", style_token="dim"))
    return tuple(spans)


def skill_loaded_spans(name: str, content: str) -> tuple[Segment, ...]:
    """``/skill <name>``: a loaded-skill header + the skill body (markdown)."""
    header = [
        Segment(text="· ", style_token="blue"),
        Segment(text="Skill loaded", style_token="bright", bold=True),
        Segment(text=f"  {name}\n", style_token="dim"),
    ]
    return tuple(header) + tuple(answer_spans(content))


def mcp_spans(servers: dict[str, str], live_tools: tuple[str, ...]) -> tuple[Segment, ...]:
    """``/mcp``: configured servers (mcp.json) + live-connected MCP tools."""
    spans = _header(
        "MCP",
        f"{len(servers)} server(s) · {len(live_tools)} tool(s) connected · /mcp add|remove",
    )
    if servers:
        width = max(len(n) for n in servers)
        for name, summary in servers.items():
            spans.append(Segment(text=f"  {name.ljust(width)}  ", style_token="teal"))
            spans.append(Segment(text=f"{summary}\n", style_token="dim"))
    else:
        spans.append(
            Segment(
                text="  no servers in mcp.json · /mcp add <name> <cmd> [args…]\n",
                style_token="dimmer",
            )
        )
    if live_tools:
        spans.append(Segment(text=f"  connected: {', '.join(live_tools)}\n", style_token="dimmer"))
    return tuple(spans)


def diff_spans(patch: str | None, *, staged: bool) -> tuple[Segment, ...]:
    """``/diff``: a compact, theme-token-only git patch.

    ``None`` (git unavailable / not a repo) and a clean tree each get a
    plain dim line; long patches truncate to :data:`_DIFF_MAX_LINES` with
    a note (never flood the transcript). Additions and deletions use the
    active theme's green/red foreground on its tab background, so the
    highlight follows runtime theme switches without embedding colors."""
    scope = "staged " if staged else ""
    if patch is None:
        return (
            Segment(
                text=f"  no {scope}diff · not a git repo or git unavailable\n",
                style_token="dimmer",
            ),
        )
    if not patch.strip():
        return (Segment(text=f"  working tree clean · no {scope}changes\n", style_token="dim"),)
    lines = patch.splitlines()
    truncated = len(lines) > _DIFF_MAX_LINES
    spans: list[Segment] = []
    for line in lines[:_DIFF_MAX_LINES]:
        token = "dim"
        background = None
        bold = False
        if line.startswith("@@"):
            token, bold = "blue", True
        elif line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            token = "teal"
        elif line.startswith("+"):
            token, background = "green", "bg-tab"
        elif line.startswith("-"):
            token, background = "red", "bg-tab"
        spans.append(
            Segment(
                text=f"  {line}\n",
                style_token=token,
                bold=bold,
                bg_token=background,
            )
        )
    if truncated:
        spans.append(
            Segment(
                text=f"\n  … +{len(lines) - _DIFF_MAX_LINES} more lines · /diff shows the head\n",
                style_token="dimmer",
            )
        )
    return tuple(spans)


__all__ = [
    "diff_spans",
    "mcp_spans",
    "model_listing_spans",
    "names_spans",
    "sessions_spans",
    "skill_loaded_spans",
    "skills_spans",
    "status_spans",
]
