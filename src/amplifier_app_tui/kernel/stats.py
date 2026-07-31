"""Cross-session cost/usage dashboard — aggregate spend + tokens over stored sessions.

Re-expresses opencode's ``stats`` command (``packages/opencode/src/cli/cmd/stats.ts``)
over the amplifier host. opencode materializes ``session.cost`` / ``session.tokens`` on a
row of its single global session DB; the amplifier host does **not** persist a per-session
cost number (kernel/runtime.py writes only ``{"bundle": ...}`` into ``metadata.json``).

The honest source of truth is therefore the append-only normalized
``provider_response_usage`` UIEvents in each session's ``ui-events.jsonl`` — the SAME stream
the live cost footer (:class:`~amplifier_app_tui.kernel.cost.CostTracker`) records — priced
through :func:`~amplifier_app_tui.kernel.cost.cost_of` (a provider-reported ``cost_usd`` wins,
else the offline fallback pricing table). Usage that cannot be priced (unknown model, no
``cost_usd``) is counted as ``unpriced`` so the total is marked ``~$`` rather than lying.

Divergences from the donor (see ``.ai/oc_donor.md``): amplifier stores sessions **per project
slug**, so the current project is the default scope and ``--project all`` is an explicit
cross-project scan; ``ProviderResponseUsage`` has no separate ``reasoning`` field (folded into
output upstream); the per-tool bar chart is out of scope (usage events are per-response, not
per-tool-part).

Layering (ADR-0007): kernel-only — imports kernel siblings + amplifier-core is fine, never
Textual. Every function is pure over a :class:`SessionStore`, so it unit-tests against a tmp-dir
store with no runtime, and all rendering is plain text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path

from . import session_manager
from .config import get_project_slug
from .cost import PricingTable, cost_of
from .events import ProviderResponseUsage
from .persistence import SessionStore

_DAY_SECONDS = 86_400.0
_USAGE_KIND = "provider_response_usage"


# --------------------------------------------------------------------------
# Rollup records
# --------------------------------------------------------------------------


@dataclass
class ModelRollup:
    """Per-model usage across the selected sessions (keyed by model id)."""

    model: str
    responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: Decimal = Decimal("0")
    unpriced: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write


@dataclass
class DayRollup:
    """Spend/usage bucketed by the session's (last-updated) UTC day."""

    date: str
    sessions: int = 0
    responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ProjectRollup:
    """Per-project totals (only populated for a ``--project all`` scan)."""

    project: str
    sessions: int = 0
    responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: Decimal = Decimal("0")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write


@dataclass
class StatsReport:
    """The aggregated dashboard model (rendered by :func:`render`)."""

    scope: str = "current project"
    window_label: str = "all time"
    days: int = 0
    total_sessions: int = 0
    total_messages: int = 0
    total_responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_cost: Decimal = Decimal("0")
    unpriced: int = 0
    earliest: float | None = None
    latest: float | None = None
    multi_project: bool = False
    by_model: dict[str, ModelRollup] = field(default_factory=dict)
    by_day: dict[str, DayRollup] = field(default_factory=dict)
    by_project: dict[str, ProjectRollup] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    @property
    def cost_per_day(self) -> Decimal:
        return self.total_cost / self.days if self.days else Decimal("0")

    @property
    def tokens_per_session(self) -> int:
        return round(self.total_tokens / self.total_sessions) if self.total_sessions else 0


# --------------------------------------------------------------------------
# Source resolution (which project stores to aggregate)
# --------------------------------------------------------------------------

Source = tuple[str, SessionStore]


def resolve_sources(
    project: str | None,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
) -> tuple[list[Source], str]:
    """Resolve the ``--project`` selector to ``(sources, scope_label)``.

    - ``None`` → the current project only (amplifier's natural per-project store).
    - ``"all"`` → every ``~/.amplifier/projects/*/sessions`` store (cross-project scan).
    - ``"<slug>"`` → one named project slug.
    """
    home = amplifier_home or (Path.home() / ".amplifier")
    if project is None:
        slug = get_project_slug(project_dir)
        return [(slug, SessionStore(project_dir=project_dir))], f"current project ({slug})"
    if project == "all":
        sources: list[Source] = []
        root = home / "projects"
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                sessions_dir = entry / "sessions"
                if sessions_dir.is_dir():
                    sources.append((entry.name, SessionStore(base_dir=sessions_dir)))
        return sources, "all projects"
    sessions_dir = home / "projects" / project / "sessions"
    return [(project, SessionStore(base_dir=sessions_dir))], f"project {project}"


# --------------------------------------------------------------------------
# Aggregation (pure; injectable clock + pricing table)
# --------------------------------------------------------------------------


def _window_days(days: int | None) -> int | None:
    """opencode window: ``None`` all-time, ``0`` today (→ 1 day), else N."""
    if days is None:
        return None
    return 1 if days == 0 else days


def _cutoff_ts(days: int | None, now: datetime) -> float | None:
    """Lower bound (posix seconds) for a session's updated-time, or ``None``."""
    if days is None:
        return None
    if days == 0:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
    return now.timestamp() - days * _DAY_SECONDS


def _window_label(days: int | None) -> str:
    if days is None:
        return "all time"
    if days == 0:
        return "today"
    return f"last {days} days"


def aggregate(
    sources: list[Source],
    *,
    days: int | None = None,
    now: datetime | None = None,
    pricing: PricingTable | None = None,
    scope: str = "current project",
    multi_project: bool = False,
) -> StatsReport:
    """Roll up cost + token usage across every session in *sources*.

    ``days`` selects the window (donor semantics: ``None`` all-time, ``0`` today,
    ``N`` last N days). ``pricing`` defaults to the offline fallback table for
    determinism. Best-effort per session: a malformed session directory or an
    unreadable event log is skipped, never fatal.
    """
    now = now or datetime.now(UTC)
    cutoff = _cutoff_ts(days, now)
    window = _window_days(days)

    report = StatsReport(
        scope=scope,
        window_label=_window_label(days),
        multi_project=multi_project,
    )

    for label, store in sources:
        project_rollup = report.by_project.setdefault(label, ProjectRollup(project=label))
        for session_id in store.list_sessions():
            summary = session_manager.summary_for(store, session_id)
            if cutoff is not None and summary.mtime < cutoff:
                continue

            report.total_sessions += 1
            report.total_messages += summary.messages
            project_rollup.sessions += 1

            if summary.mtime:
                report.earliest = (
                    summary.mtime
                    if report.earliest is None
                    else min(report.earliest, summary.mtime)
                )
                report.latest = (
                    summary.mtime if report.latest is None else max(report.latest, summary.mtime)
                )
                day_key = datetime.fromtimestamp(summary.mtime, tz=UTC).date().isoformat()
            else:
                day_key = "unknown"
            day_rollup = report.by_day.setdefault(day_key, DayRollup(date=day_key))
            day_rollup.sessions += 1

            for record in store.read_events(session_id):
                if record.get("kind") != _USAGE_KIND:
                    continue
                try:
                    usage = ProviderResponseUsage.model_validate(record)
                except Exception:  # noqa: BLE001 — skip malformed usage records
                    continue

                cost = cost_of(usage, pricing)
                priced = cost is not None
                cost = cost or Decimal("0")

                report.total_responses += 1
                report.input_tokens += usage.input_tokens
                report.output_tokens += usage.output_tokens
                report.cache_read += usage.cache_read
                report.cache_write += usage.cache_write
                report.total_cost += cost
                if not priced:
                    report.unpriced += 1

                model_key = usage.model or "unknown"
                model_rollup = report.by_model.setdefault(model_key, ModelRollup(model=model_key))
                model_rollup.responses += 1
                model_rollup.input_tokens += usage.input_tokens
                model_rollup.output_tokens += usage.output_tokens
                model_rollup.cache_read += usage.cache_read
                model_rollup.cache_write += usage.cache_write
                model_rollup.cost += cost
                if not priced:
                    model_rollup.unpriced += 1

                day_rollup.responses += 1
                day_rollup.input_tokens += usage.input_tokens
                day_rollup.output_tokens += usage.output_tokens
                day_rollup.cost += cost

                project_rollup.responses += 1
                project_rollup.input_tokens += usage.input_tokens
                project_rollup.output_tokens += usage.output_tokens
                project_rollup.cache_read += usage.cache_read
                project_rollup.cache_write += usage.cache_write
                project_rollup.cost += cost

    # effectiveDays: the window when set, else the observed span (min 1); 0 when empty.
    if report.total_sessions == 0:
        report.days = window or 0
    elif window is not None:
        report.days = window
    elif report.earliest is not None and report.latest is not None:
        span = ceil((report.latest - report.earliest) / _DAY_SECONDS)
        report.days = max(1, span)
    else:
        report.days = 1
    return report


# --------------------------------------------------------------------------
# Rendering (plain text dashboard or JSON)
# --------------------------------------------------------------------------

_WIDTH = 60


def _humanize(num: int) -> str:
    """1234 → ``1.2K``, 3_400_000 → ``3.4M`` (donor ``formatNumber``)."""
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)


def _money(cost: Decimal, unpriced: int = 0) -> str:
    """``$0.02`` — prefixed ``~`` when some usage could not be priced (a floor)."""
    prefix = "~" if unpriced else ""
    return f"{prefix}${float(cost):.2f}"


def _row(label: str, value: str) -> str:
    return f"  {label:<26}{value}"


def _section(title: str) -> list[str]:
    return [title, "─" * _WIDTH]


def _parse_model_limit(models: str | None) -> int | None:
    """``None`` → hidden; ``"all"``/non-numeric → all (``0``); ``"N"`` → top N."""
    if models is None:
        return None
    try:
        return max(0, int(models))
    except (TypeError, ValueError):
        return 0  # bare --models (flag_value "all") shows every model


def render(report: StatsReport, *, models: str | None = None, json_output: bool = False) -> str:
    """Render *report* as a readable terminal dashboard (or JSON when asked)."""
    if json_output:
        return _render_json(report, models=models)

    lines: list[str] = []
    lines.append("AMPLIFIER USAGE STATS")
    lines.append(f"scope: {report.scope}  ·  window: {report.window_label}")
    lines.append("")

    lines += _section("OVERVIEW")
    lines.append(_row("Sessions", str(report.total_sessions)))
    lines.append(_row("Messages", str(report.total_messages)))
    lines.append(_row("Responses", str(report.total_responses)))
    lines.append(_row("Days", str(report.days)))
    if report.earliest is not None and report.latest is not None:
        first = datetime.fromtimestamp(report.earliest, tz=UTC).date().isoformat()
        last = datetime.fromtimestamp(report.latest, tz=UTC).date().isoformat()
        lines.append(_row("Date range", f"{first} → {last}"))
    lines.append("")

    if report.total_sessions == 0:
        lines.append("No usage recorded for the selected window.")
        lines.append("")
        return "\n".join(lines)

    lines += _section("COST & TOKENS")
    lines.append(_row("Total Cost", _money(report.total_cost, report.unpriced)))
    lines.append(_row("Avg Cost/Day", _money(report.cost_per_day, report.unpriced)))
    lines.append(_row("Avg Tokens/Session", _humanize(report.tokens_per_session)))
    lines.append(_row("Input", _humanize(report.input_tokens)))
    lines.append(_row("Output", _humanize(report.output_tokens)))
    lines.append(_row("Cache Read", _humanize(report.cache_read)))
    lines.append(_row("Cache Write", _humanize(report.cache_write)))
    if report.unpriced:
        lines.append(_row("Unpriced responses", f"{report.unpriced} (cost is a floor)"))
    lines.append("")

    lines += _by_day_section(report)

    limit = _parse_model_limit(models)
    if limit is not None and report.by_model:
        lines += _by_model_section(report, limit)

    if report.multi_project:
        lines += _by_project_section(report)

    return "\n".join(lines)


def _by_day_section(report: StatsReport) -> list[str]:
    lines = _section("BY DAY")
    lines.append(f"  {'Date':<12}{'Responses':>10}{'Tokens':>12}{'Cost':>12}")
    for key in sorted(report.by_day):
        day = report.by_day[key]
        lines.append(
            f"  {day.date:<12}{day.responses:>10}{_humanize(day.tokens):>12}{_money(day.cost):>12}"
        )
    lines.append("")
    return lines


def _by_model_section(report: StatsReport, limit: int) -> list[str]:
    ordered = sorted(report.by_model.values(), key=lambda m: (m.responses, m.model), reverse=True)
    if limit > 0:
        ordered = ordered[:limit]
    lines = _section("BY MODEL")
    for model in ordered:
        lines.append(f"  {model.model}")
        lines.append(_row("  Responses", str(model.responses)))
        lines.append(_row("  Input Tokens", _humanize(model.input_tokens)))
        lines.append(_row("  Output Tokens", _humanize(model.output_tokens)))
        lines.append(_row("  Cache Read", _humanize(model.cache_read)))
        lines.append(_row("  Cache Write", _humanize(model.cache_write)))
        lines.append(_row("  Cost", _money(model.cost, model.unpriced)))
    lines.append("")
    return lines


def _by_project_section(report: StatsReport) -> list[str]:
    ordered = sorted(report.by_project.values(), key=lambda p: p.cost, reverse=True)
    lines = _section("BY PROJECT")
    lines.append(f"  {'Project':<32}{'Sessions':>9}{'Resp':>7}{'Cost':>12}")
    for proj in ordered:
        name = proj.project if len(proj.project) <= 32 else proj.project[:29] + "..."
        lines.append(f"  {name:<32}{proj.sessions:>9}{proj.responses:>7}{_money(proj.cost):>12}")
    lines.append("")
    return lines


def _render_json(report: StatsReport, *, models: str | None) -> str:
    limit = _parse_model_limit(models)
    model_items = sorted(
        report.by_model.values(), key=lambda m: (m.responses, m.model), reverse=True
    )
    if limit is not None and limit > 0:
        model_items = model_items[:limit]
    payload = {
        "scope": report.scope,
        "window": report.window_label,
        "days": report.days,
        "total_sessions": report.total_sessions,
        "total_messages": report.total_messages,
        "total_responses": report.total_responses,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "cache_read": report.cache_read,
        "cache_write": report.cache_write,
        "total_tokens": report.total_tokens,
        "total_cost": str(report.total_cost),
        "cost_per_day": str(report.cost_per_day),
        "unpriced": report.unpriced,
        "by_model": {
            m.model: {
                "responses": m.responses,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "cache_read": m.cache_read,
                "cache_write": m.cache_write,
                "cost": str(m.cost),
                "unpriced": m.unpriced,
            }
            for m in model_items
        },
        "by_day": {
            key: {
                "responses": report.by_day[key].responses,
                "tokens": report.by_day[key].tokens,
                "cost": str(report.by_day[key].cost),
            }
            for key in sorted(report.by_day)
        },
    }
    if report.multi_project:
        payload["by_project"] = {
            p.project: {
                "sessions": p.sessions,
                "responses": p.responses,
                "cost": str(p.cost),
                "total_tokens": p.total_tokens,
            }
            for p in sorted(report.by_project.values(), key=lambda p: p.cost, reverse=True)
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


__all__ = [
    "DayRollup",
    "ModelRollup",
    "ProjectRollup",
    "StatsReport",
    "aggregate",
    "render",
    "resolve_sources",
]
