"""Code Mode: the model-facing contract for the confined ``execute`` tool.

A host-native re-expression of opencode's "code mode" (`.ai/oc_donor.md`):
instead of the model emitting N separate tool calls, it writes ONE small
program that calls many tools programmatically in a single sandboxed pass
(context economy). This module owns the *model-facing* surface only — it is
pure (no Textual, no amplifier-core, no kernel): the tool catalog and its
budgeted discovery instructions, the execution-limit knobs, the result shape,
and the diagnostic taxonomy. The genuinely-restricted Python execution backend
that runs a program against these limits lives in ``kernel/codemode.py``.

Faithful to the donor laws (`.ai/oc_donor.md` §4): expected program and tool
failures are DATA (a :class:`Diagnostic`), never exceptions; a tool result is
plain JSON-like data; the host owns authority — code mode only confines a
program to the supplied tool tree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# The single model-facing tool name (donor: ``CODE_MODE_TOOL = "execute"``).
CODE_MODE_TOOL = "execute"
# The program-visible root object holding the namespaced tool tree.
CODE_MODE_NAMESPACE = "tools"
# The always-callable runtime search tool (advertised only when the inline
# catalog is partial). A host cannot define its own ``$codemode`` namespace.
RUNTIME_SEARCH_TOOL = "$codemode.search"

# Default catalog budget: ~2000 estimated tokens (chars / 4), the donor
# heuristic. Applies only to full inlined tool signatures, never the fixed
# instructions or the per-namespace summaries.
DEFAULT_CATALOG_BUDGET = 2000
_CHARS_PER_TOKEN = 4


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- tools & catalog ---------------------------------------------------------


class ToolSpec(_Frozen):
    """One schema-described tool exposed to a code-mode program.

    ``input_schema`` / ``output_schema`` are render-only JSON-Schema documents
    (donor: they shape the model-visible signature; values are not validated
    here — the host tool owns validation and authorization).
    """

    namespace: str
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = {}
    output_schema: Mapping[str, Any] | None = None

    @property
    def path(self) -> str:
        """The program call site, e.g. ``fs.read_file``."""
        return f"{self.namespace}.{self.name}"


def _sanitize(segment: str) -> str:
    """Reduce a raw name to a safe namespace/tool segment (donor: sanitize)."""
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in segment.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "tool"


class ToolCatalog(_Frozen):
    """The namespaced tool tree exposed to a program as ``tools.<ns>.<tool>``."""

    specs: tuple[ToolSpec, ...] = ()

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(spec.namespace for spec in self.specs))

    @property
    def total_tools(self) -> int:
        return len(self.specs)

    def grouped(self) -> tuple[tuple[str, tuple[ToolSpec, ...]], ...]:
        """Specs grouped by namespace, both levels alphabetical (fairness)."""
        groups: dict[str, list[ToolSpec]] = {}
        for spec in self.specs:
            groups.setdefault(spec.namespace, []).append(spec)
        return tuple(
            (namespace, tuple(sorted(groups[namespace], key=lambda s: s.name)))
            for namespace in sorted(groups)
        )


def build_catalog(specs: Iterable[ToolSpec]) -> ToolCatalog:
    """Build a catalog from raw specs: sanitize segments, dedupe by path.

    A later spec never silently overwrites an earlier one at the same path —
    first registration wins (donor: immutable, host-owned tool scope).
    """
    seen: set[str] = set()
    normalized: list[ToolSpec] = []
    for spec in specs:
        namespace = _sanitize(spec.namespace)
        name = _sanitize(spec.name)
        path = f"{namespace}.{name}"
        if path in seen:
            continue
        seen.add(path)
        normalized.append(spec.model_copy(update={"namespace": namespace, "name": name}))
    return ToolCatalog(specs=tuple(normalized))


# -- discovery / instructions ------------------------------------------------

_JSON_TYPES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "null": "null",
}


def _json_type(schema: Mapping[str, Any] | None) -> str:
    if not schema:
        return "unknown"
    raw = schema.get("type")
    if isinstance(raw, list):
        raw = next((item for item in raw if item != "null"), None)
    if raw == "array":
        return f"{_json_type(schema.get('items') if isinstance(schema.get('items'), Mapping) else None)}[]"
    if isinstance(raw, str):
        return _JSON_TYPES.get(raw, "unknown")
    return "unknown"


def render_signature(spec: ToolSpec) -> str:
    """The model-visible TypeScript-ish signature for one tool.

    e.g. ``tools.fs.read_file(input: { path: string, limit?: number }): Promise<object>``.
    """
    props = spec.input_schema.get("properties") if isinstance(spec.input_schema, Mapping) else None
    required = (
        spec.input_schema.get("required", []) if isinstance(spec.input_schema, Mapping) else []
    )
    required_set = set(required) if isinstance(required, (list, tuple)) else set()
    parts: list[str] = []
    if isinstance(props, Mapping):
        for field_name, sub in props.items():
            opt = "" if field_name in required_set else "?"
            sub_schema = sub if isinstance(sub, Mapping) else None
            parts.append(f"{field_name}{opt}: {_json_type(sub_schema)}")
    inp = "{ " + ", ".join(parts) + " }" if parts else "{}"
    out = _json_type(spec.output_schema) if spec.output_schema else "unknown"
    return f"tools.{spec.namespace}.{spec.name}(input: {inp}): Promise<{out}>"


def _entry_cost(spec: ToolSpec) -> int:
    body = render_signature(spec) + " " + spec.description
    return max(1, len(body) // _CHARS_PER_TOKEN)


_WORKFLOW = (
    "## Workflow",
    "1. Pick an exact tool from the catalog below.",
    "2. Call the exact path as written — `tools.<namespace>.<tool>(input)` — do not"
    " guess or normalize segments.",
    "3. Sequence dependent calls and filter/aggregate large results INSIDE the"
    " program; `return` only the distilled data the agent needs.",
)

_RULES = (
    "## Rules",
    "- Only the tools listed (or returned by search) exist inside `tools`; nothing else.",
    "- One `execute` program replaces many round-trips: keep intermediate data in the"
    " program instead of the model's context.",
    "- Authority is host-owned: a program can only exercise the tools it was given —"
    " it cannot import modules, open files, spawn processes, or reach the network.",
)

_LANGUAGE = (
    "## Language",
    "A restricted Python orchestration subset — plain data, control flow, comprehensions,"
    " functions, and the supplied `tools`/`log`. NOT available: `import`, `open`, `eval`,"
    " `exec`, filesystem/process/network access, or arbitrary host globals.",
)


def render_instructions(
    catalog: ToolCatalog,
    *,
    catalog_budget: int = DEFAULT_CATALOG_BUDGET,
) -> str:
    """Render the model-facing Code Mode instructions + budgeted catalog.

    Faithful to the donor discovery contract (`.ai/oc_donor.md` §3): every
    namespace is ALWAYS listed with its tool count; complete signatures are
    inlined round-robin across namespaces (so one big namespace can't starve
    the others) until ``catalog_budget`` estimated tokens are spent; the header
    states coverage (``COMPLETE`` vs ``PARTIAL — k of N shown``) and
    ``$codemode.search`` is advertised only when the inline list is partial.
    """
    if catalog_budget < 0:
        raise ValueError("catalog_budget must be non-negative")

    grouped = catalog.grouped()
    remaining = catalog_budget
    shown: dict[str, list[ToolSpec]] = {ns: [] for ns, _ in grouped}
    queues: dict[str, list[ToolSpec]] = {
        ns: sorted(specs, key=_entry_cost) for ns, specs in grouped
    }
    active = sorted(ns for ns, specs in grouped if specs)
    while active:
        progressed = False
        next_active: list[str] = []
        for ns in active:
            head = queues[ns][0]
            cost = _entry_cost(head)
            if cost <= remaining:
                remaining -= cost
                shown[ns].append(queues[ns].pop(0))
                progressed = True
                if queues[ns]:
                    next_active.append(ns)
            # else: this namespace's cheapest entry did not fit — it drops out.
        active = sorted(next_active)
        if not progressed:
            break

    total = catalog.total_tools
    total_shown = sum(len(v) for v in shown.values())
    complete = total_shown == total

    lines: list[str] = ["# Code Mode"]
    lines.append(
        "Write one confined program that calls the tools below and returns only the"
        " data you need — one pass, many calls, no per-call round-trip."
    )
    lines.append("")
    lines.extend(_WORKFLOW)
    lines.append("")
    lines.extend(_RULES)
    if not complete:
        lines.append(
            f"- The catalog is PARTIAL — call `{RUNTIME_SEARCH_TOOL}"
            "({ query })` to find any tool not inlined below."
        )
    lines.append("")
    lines.extend(_LANGUAGE)
    lines.append("")
    coverage = "COMPLETE list" if complete else f"PARTIAL — {total_shown} of {total} shown"
    lines.append(f"## Available tools ({coverage})")
    if not grouped:
        lines.append("(no tools are currently available to code mode)")
    for namespace, specs in grouped:
        count = len(specs)
        picked = shown[namespace]
        if len(picked) == count:
            suffix = f"({count} tools)" if count != 1 else "(1 tool)"
        elif picked:
            suffix = f"({count} tools, {len(picked)} shown)"
        else:
            suffix = f"({count} tools, none shown)"
        lines.append(f"### {namespace} {suffix}")
        for spec in picked:
            lines.append(f"- {render_signature(spec)}")
            if spec.description:
                lines.append(f"  {spec.description}")
    return "\n".join(lines)


# -- execution limits --------------------------------------------------------


class ExecutionLimits(_Frozen):
    """The three host-policy knobs (donor: no defaults — budgets are host policy).

    ``timeout_ms`` >= 1 when set; ``max_tool_calls`` / ``max_output_bytes`` >= 0
    when set. Invalid configuration raises ``ValueError`` (the host analogue of
    the donor's ``RangeError`` at construction).
    """

    timeout_ms: int | None = None
    max_tool_calls: int | None = None
    max_output_bytes: int | None = None

    @model_validator(mode="after")
    def _check(self) -> ExecutionLimits:
        if self.timeout_ms is not None and self.timeout_ms < 1:
            raise ValueError("timeout_ms must be at least 1")
        if self.max_tool_calls is not None and self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative")
        if self.max_output_bytes is not None and self.max_output_bytes < 0:
            raise ValueError("max_output_bytes must be non-negative")
        return self

    @property
    def timeout_seconds(self) -> float | None:
        return None if self.timeout_ms is None else self.timeout_ms / 1000


# -- diagnostics & result ----------------------------------------------------


class DiagnosticKind(str, Enum):
    """Normalized failure taxonomy (donor: failures are data, not exceptions)."""

    PARSE_ERROR = "parse_error"
    UNSUPPORTED_SYNTAX = "unsupported_syntax"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_TOOL_INPUT = "invalid_tool_input"
    INVALID_TOOL_OUTPUT = "invalid_tool_output"
    INVALID_DATA_VALUE = "invalid_data_value"
    TOOL_CALL_LIMIT_EXCEEDED = "tool_call_limit_exceeded"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    TOOL_FAILURE = "tool_failure"
    EXECUTION_FAILURE = "execution_failure"


class Diagnostic(_Frozen):
    """A safe, model-visible failure record — never carries a host cause."""

    kind: DiagnosticKind
    message: str
    suggestions: tuple[str, ...] = ()


ToolCallStatus = Literal["running", "completed", "error"]


class ToolCall(_Frozen):
    """One nested call admitted by the runtime (name + status; no input/cause)."""

    name: str
    status: ToolCallStatus = "completed"


class ExecuteResult(_Frozen):
    """The single structured outcome of running a program (donor: `CodeMode.Result`)."""

    ok: bool
    value: Any = None
    logs: tuple[str, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    truncated: bool = False
    diagnostic: Diagnostic | None = None

    def render_output(self) -> str:
        """Assemble the adapter-visible ``output`` string (donor `code-mode.ts`).

        The program's return value as-is when it is a string, else JSON pretty-
        printed; a trailing ``Logs:`` section is appended when the program
        logged. On failure the diagnostic message (plus any suggestions) is the
        body.
        """
        import json

        if self.ok:
            if isinstance(self.value, str):
                body = self.value
            elif self.value is None:
                body = "null"
            else:
                try:
                    body = json.dumps(self.value, indent=2, ensure_ascii=False)
                except (TypeError, ValueError):
                    body = str(self.value)
        else:
            diag = self.diagnostic
            parts = [diag.message] if diag else ["execution failed"]
            if diag:
                parts.extend(hint for hint in diag.suggestions if hint not in diag.message)
            body = "\n".join(parts)
        if self.logs:
            joined = "\n".join(self.logs)
            body = f"{body}\n\nLogs:\n{joined}" if body else f"Logs:\n{joined}"
        return body


def diagnostic_result(
    kind: DiagnosticKind,
    message: str,
    *,
    logs: Sequence[str] = (),
    tool_calls: Sequence[ToolCall] = (),
    suggestions: Sequence[str] = (),
) -> ExecuteResult:
    """A failed :class:`ExecuteResult` carrying one normalized diagnostic."""
    return ExecuteResult(
        ok=False,
        diagnostic=Diagnostic(kind=kind, message=message, suggestions=tuple(suggestions)),
        logs=tuple(logs),
        tool_calls=tuple(tool_calls),
    )


__all__ = [
    "CODE_MODE_NAMESPACE",
    "CODE_MODE_TOOL",
    "DEFAULT_CATALOG_BUDGET",
    "RUNTIME_SEARCH_TOOL",
    "Diagnostic",
    "DiagnosticKind",
    "ExecuteResult",
    "ExecutionLimits",
    "ToolCall",
    "ToolCallStatus",
    "ToolCatalog",
    "ToolSpec",
    "build_catalog",
    "diagnostic_result",
    "render_instructions",
    "render_signature",
]
