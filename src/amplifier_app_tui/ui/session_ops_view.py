"""Segment renderers for the in-session ops commands.

``/model``, ``/status``, ``/tools``, ``/agents`` and ``/diff`` post an
:class:`~amplifier_app_tui.model.blocks.Answer` to the transcript;
these pure functions turn the kernel result data into the flat
``Segment`` stream that block carries, matching the house style of
:func:`amplifier_app_tui.ui.app_support.native_modes_segments`
(blue ``·`` marker, bright-bold header, dim/teal detail). Pure and
Textual-free so they unit-test as span tuples.
"""

from __future__ import annotations

from decimal import Decimal

from ..kernel.compaction import CompactionConfig
from ..kernel.session_manager import SessionState, SessionSummary
from ..kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from ..model.blocks import Segment, StyleToken
from .live_tail import answer_spans

_DIFF_MAX_LINES = 400

STATE_LABELS: dict[SessionState, str] = {
    "recovered": "recovered",
    "corrupt": "corrupt",
    "transcript_lost": "transcript lost",
    "indexing": "indexing",
}
"""Display label for a non-``"ok"`` :class:`SessionState` (S2 compliance:
a damaged session is labeled explicitly, never dropped or shown as healthy)."""

STATE_STYLE_TOKENS: dict[SessionState, StyleToken] = {
    "recovered": "orange",
    "corrupt": "red",
    "transcript_lost": "orange",
    "indexing": "red",
}
"""Theme token per non-``"ok"`` state -- orange (warning) for a session that
is still identifiable/nameable (a patched-together metadata shell, or one
whose only loss is the transcript), red (error) for one with no
trustworthy identity at all (the listing could not summarize it, or there
is no catalog entry to read a name/bundle from)."""


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
    ``amplifier-tui resume SESSION_ID`` (noted in the header), never an
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
            f"{len(summaries)} stored · resume: amplifier-tui resume SESSION_ID",
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
        detail = (
            f"{summary.name or '—'}  ·  {summary.bundle}  ·  "
            f"{summary.messages} msgs  ·  {summary.time_ago}"
        )
        # Trailing chips: dim ``#tag`` chips (the donor has no first-class
        # session tags, so this follows the house dim-metadata convention
        # rather than any donor widget) and, when the session is damaged, a
        # bold state chip (S2 compliance) so a recovered/corrupt session is
        # never rendered as if it were healthy.
        trailer: list[Segment] = []
        if summary.tags:
            chips = " ".join(f"#{tag}" for tag in summary.tags)
            trailer.append(Segment(text=chips, style_token="dimmer"))
        if summary.state != "ok":
            if trailer:
                trailer.append(Segment(text="  ", style_token="dim"))
            trailer.append(
                Segment(
                    text=f"⚠ {STATE_LABELS[summary.state]}",
                    style_token=STATE_STYLE_TOKENS[summary.state],
                    bold=True,
                )
            )
        if trailer:
            spans.append(Segment(text=f"{detail}  ", style_token="dim"))
            spans.extend(trailer)
            spans.append(Segment(text="\n", style_token="dim"))
        else:
            spans.append(Segment(text=f"{detail}\n", style_token="dim"))
    return tuple(spans)


STATE_EXPLANATIONS: dict[SessionState, str] = {
    "recovered": "metadata.json could not be parsed; showing a recovered shell (name/bundle/tags may be missing)",
    "corrupt": "this session's files could not be summarized at all; only the id below is trustworthy",
    "transcript_lost": "metadata is intact, but the transcript (and its backup) could not be read; conversation history for this session is gone",
    "indexing": "transcript content exists but no metadata record was ever written (interrupted mid-save, or created by another tool); name, bundle and turn count are unknown",
}
"""One-line, plain-language reason shown under a damaged session's state
chip in :func:`session_detail_spans` (S2 compliance: explain, don't just
label)."""


def session_detail_spans(summary: SessionSummary) -> tuple[Segment, ...]:
    """Full-id detail surface for one session (S2 compliance gap 1).

    The table/roster shows only ``short_id`` (8 chars); this is the
    "detail view" a row's Enter/click opens
    (:class:`~amplifier_app_tui.ui.sessions_strip.SessionsStrip`). The full
    id renders alone on its own dim-labelled line, in the bright/bold
    token, so a terminal's own mouse-drag text selection always finds
    exactly it and nothing else -- terminal clipboard access is
    environment-dependent, so this display path is the reliable one; the
    app additionally best-effort copies it via the existing clipboard
    helper when this block is opened (see ``TuiApp.copy_to_clipboard``).

    A damaged session (``state != "ok"``) still shows its full id -- often
    the only handle a user has to go find/delete the directory by hand --
    plus an explicit state chip and a plain-language explanation instead of
    silently-empty or misleading metadata.
    """
    spans = [
        Segment(text="· ", style_token="blue"),
        Segment(text="Session detail", style_token="bright", bold=True),
        Segment(text=f"  {summary.short_id}\n", style_token="dim"),
        Segment(text="  full id  ", style_token="dim"),
        Segment(text=f"{summary.session_id}\n", style_token="bright", bold=True),
    ]
    if summary.state != "ok":
        spans.append(
            Segment(
                text=f"  ⚠ {STATE_LABELS[summary.state]}  ",
                style_token=STATE_STYLE_TOKENS[summary.state],
                bold=True,
            )
        )
        spans.append(Segment(text=f"{STATE_EXPLANATIONS[summary.state]}\n", style_token="dim"))
    rows: tuple[tuple[str, str], ...] = (
        ("name", summary.name or "—"),
        ("bundle", summary.bundle),
        ("messages", str(summary.messages)),
        ("turns", "—" if summary.turns is None else str(summary.turns)),
        ("age", summary.time_ago),
    )
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        spans.append(Segment(text=f"  {label.ljust(width)}  ", style_token="dim"))
        spans.append(Segment(text=f"{value}\n", style_token="teal"))
    if summary.tags:
        chips = " ".join(f"#{tag}" for tag in summary.tags)
        spans.append(Segment(text=f"  {'tags'.ljust(width)}  ", style_token="dim"))
        spans.append(Segment(text=f"{chips}\n", style_token="dimmer"))
    spans.append(
        Segment(
            text="  select the full id above to copy it · /copy re-copies this detail\n",
            style_token="dimmer",
        )
    )
    return tuple(spans)


def resume_command_for(summary: SessionSummary) -> str:
    """The exact, ready-to-run command that resumes *summary* (S2 gap 2).

    Single source for this string -- the keyboard-resume block below and
    the app's clipboard copy both call this rather than hand-formatting
    ``f"amplifier-tui resume {short_id}"`` a second time.
    """
    return f"amplifier-tui resume {summary.short_id}"


def session_resume_spans(summary: SessionSummary) -> tuple[Segment, ...]:
    """Keyboard-resume block for one session (S2 compliance gap 2).

    ``/sessions`` has always been read-only here: switching sessions is a
    fresh ``amplifier-tui resume SESSION_ID`` process, never an in-place
    teardown of the one currently running (the picker's own docstring;
    :func:`sessions_spans`'s header note). That contract is NOT changed by
    this block -- it exists so a keyboard-only user reaches the SAME
    resume path a mouse user gets from reading the id off the detail view
    and typing it in by hand, without leaving the keyboard or transcribing
    an 8-char id. Posting this block also best-effort copies the command
    to the clipboard (``TuiApp.copy_to_clipboard``), mirroring how
    :func:`session_detail_spans` copies the bare id.

    A damaged session (``state != "ok"``) still gets its command line --
    the CLI's own resume path already reports a clear, distinct exit code
    for a corrupt/unindexed target (S3's ``RESUME_EXIT_CORRUPT``) rather
    than needing this surface to gate it twice -- but the state chip and
    explanation are repeated here too, so a keyboard user does not have to
    separately open detail to learn WHY resume might refuse the id.
    """
    command = resume_command_for(summary)
    spans = [
        Segment(text="\u00b7 ", style_token="blue"),
        Segment(text="Resume ready", style_token="bright", bold=True),
        Segment(text=f"  {summary.short_id}\n", style_token="dim"),
        Segment(text="  command  ", style_token="dim"),
        Segment(text=f"{command}\n", style_token="bright", bold=True),
    ]
    if summary.state != "ok":
        spans.append(
            Segment(
                text=f"  \u26a0 {STATE_LABELS[summary.state]}  ",
                style_token=STATE_STYLE_TOKENS[summary.state],
                bold=True,
            )
        )
        spans.append(Segment(text=f"{STATE_EXPLANATIONS[summary.state]}\n", style_token="dim"))
    spans.append(
        Segment(
            text="  /sessions stays read-only \u2014 run the command above in a new terminal"
            " (or after quitting) to switch into it; nothing here is torn down\n",
            style_token="dimmer",
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
    "STATE_EXPLANATIONS",
    "STATE_LABELS",
    "STATE_STYLE_TOKENS",
    "diff_spans",
    "mcp_spans",
    "model_listing_spans",
    "names_spans",
    "resume_command_for",
    "session_detail_spans",
    "session_resume_spans",
    "sessions_spans",
    "skill_loaded_spans",
    "skills_spans",
    "status_spans",
]
