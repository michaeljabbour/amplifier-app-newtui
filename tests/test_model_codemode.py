"""Model-layer Code Mode contract: catalog, discovery, limits, result shape."""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.codemode import (
    CODE_MODE_TOOL,
    RUNTIME_SEARCH_TOOL,
    Diagnostic,
    DiagnosticKind,
    ExecuteResult,
    ExecutionLimits,
    ToolCall,
    ToolSpec,
    build_catalog,
    diagnostic_result,
    render_instructions,
    render_signature,
)


def _spec(ns: str, name: str, desc: str = "", **schema: object) -> ToolSpec:
    return ToolSpec(namespace=ns, name=name, description=desc, input_schema=schema)


def test_code_mode_tool_name_is_execute() -> None:
    assert CODE_MODE_TOOL == "execute"


def test_build_catalog_sanitizes_and_dedupes() -> None:
    catalog = build_catalog(
        [
            _spec("f s", "read file"),  # sanitized -> f_s.read_file
            _spec("f_s", "read_file"),  # duplicate path -> dropped (first wins)
            _spec("web", "fetch"),
        ]
    )
    paths = [spec.path for spec in catalog.specs]
    assert paths == ["f_s.read_file", "web.fetch"]
    assert catalog.namespaces == ("f_s", "web")
    assert catalog.total_tools == 2


def test_render_signature_shape() -> None:
    spec = ToolSpec(
        namespace="fs",
        name="read_file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
        output_schema={"type": "object"},
    )
    sig = render_signature(spec)
    assert sig == "tools.fs.read_file(input: { path: string, limit?: number }): Promise<object>"


def test_render_signature_defaults_unknown_output_and_empty_input() -> None:
    assert render_signature(_spec("ns", "ping")) == "tools.ns.ping(input: {}): Promise<unknown>"


def test_instructions_complete_lists_every_namespace_and_omits_search() -> None:
    catalog = build_catalog([_spec("read", "read_file"), _spec("web", "fetch")])
    text = render_instructions(catalog, catalog_budget=10_000)
    assert "COMPLETE list" in text
    assert "### read (1 tool)" in text
    assert "### web (1 tool)" in text
    assert "tools.read.read_file" in text
    # search is advertised ONLY when the inline catalog is partial.
    assert f"call `{RUNTIME_SEARCH_TOOL}" not in text


def test_instructions_partial_advertises_search_and_keeps_all_namespaces_visible() -> None:
    catalog = build_catalog(
        [_spec("a", "one", "x" * 200), _spec("a", "two", "y" * 200), _spec("b", "three", "z" * 200)]
    )
    text = render_instructions(catalog, catalog_budget=1)  # nothing fits
    assert "PARTIAL — 0 of 3 shown" in text
    assert RUNTIME_SEARCH_TOOL in text
    # Every namespace is still listed with its count even when nothing inlined.
    assert "### a (2 tools, none shown)" in text
    assert "### b (1 tools, none shown)" in text


def test_instructions_budget_is_round_robin_fair_across_namespaces() -> None:
    # Budget admits exactly two entries; fairness must give each namespace one
    # before either gets a second.
    catalog = build_catalog([_spec("a", "a1"), _spec("a", "a2"), _spec("b", "b1")])
    one_each = render_instructions(catalog, catalog_budget=20)
    assert "### a (2 tools, 1 shown)" in one_each
    assert "### b (1 tool)" in one_each


def test_execution_limits_validation() -> None:
    with pytest.raises(ValueError):
        ExecutionLimits(timeout_ms=0)
    with pytest.raises(ValueError):
        ExecutionLimits(max_tool_calls=-1)
    with pytest.raises(ValueError):
        ExecutionLimits(max_output_bytes=-5)
    limits = ExecutionLimits(timeout_ms=1500)
    assert limits.timeout_seconds == 1.5
    assert ExecutionLimits().timeout_seconds is None


def test_execute_result_render_output_variants() -> None:
    assert ExecuteResult(ok=True, value="hi").render_output() == "hi"
    assert ExecuteResult(ok=True, value=None).render_output() == "null"
    assert ExecuteResult(ok=True, value={"a": 1}).render_output() == '{\n  "a": 1\n}'
    with_logs = ExecuteResult(ok=True, value="done", logs=("step 1", "step 2"))
    assert with_logs.render_output() == "done\n\nLogs:\nstep 1\nstep 2"


def test_execute_result_failure_render_output_uses_diagnostic() -> None:
    result = diagnostic_result(
        DiagnosticKind.TOOL_FAILURE, "order is unavailable", suggestions=("retry later",)
    )
    assert result.ok is False
    assert result.diagnostic == Diagnostic(
        kind=DiagnosticKind.TOOL_FAILURE,
        message="order is unavailable",
        suggestions=("retry later",),
    )
    body = result.render_output()
    assert "order is unavailable" in body
    assert "retry later" in body


def test_tool_call_defaults_completed() -> None:
    assert ToolCall(name="fs.read_file").status == "completed"
