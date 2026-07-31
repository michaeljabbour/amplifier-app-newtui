"""``/codemode`` — preview the Code Mode ``execute()`` tool catalog.

A pure command (model + stdlib only) that surfaces the host-native Code Mode
contract as a durable transcript block: instead of the model emitting many
separate tool calls, it can write ONE confined program that calls the tools
below programmatically in a single sandboxed pass (`.ai/oc_donor.md`).

The previewed catalog is built from the app's OWN governance map
(``model.trust.tool_capability_map``), grouped into namespaces by the capability
each tool exercises — so the surface is honest about what an ``execute`` program
could orchestrate, and stays a live reflection of the trust table. The handler
is registered as a first-class During-group built-in (``commands/builtin.py``).
"""

from __future__ import annotations

from ..model.blocks import ToolLine
from ..model.codemode import (
    CODE_MODE_TOOL,
    ToolCatalog,
    ToolSpec,
    build_catalog,
    render_instructions,
)
from ..model.trust import CapabilityClass, tool_capability_map
from .registry import CommandContext

CODEMODE_COMMAND = "/codemode"

# Capability -> program-visible namespace. Grouping governed tools by the
# capability they exercise keeps the preview tied to the real trust posture.
_NAMESPACE_BY_CAPABILITY: dict[CapabilityClass, str] = {
    CapabilityClass.READ: "read",
    CapabilityClass.WRITE: "write",
    CapabilityClass.NET: "net",
    CapabilityClass.TEST: "test",
    CapabilityClass.SPEND: "spend",
    CapabilityClass.EXEC: "shell",
    CapabilityClass.OUTSIDE_PROJECT: "outside",
}


def governed_catalog() -> ToolCatalog:
    """A Code Mode catalog built from the app's governance map.

    Excludes ``execute`` itself — a code-mode program does not orchestrate code
    mode — and groups every other governed tool into its capability namespace.
    """
    specs: list[ToolSpec] = []
    for tool_name, capability in sorted(tool_capability_map().items()):
        if tool_name == CODE_MODE_TOOL:
            continue
        namespace = _NAMESPACE_BY_CAPABILITY.get(capability, "tool")
        specs.append(
            ToolSpec(
                namespace=namespace,
                name=tool_name,
                description=f"{capability.value} capability",
            )
        )
    return build_catalog(specs)


def build_codemode_block(catalog: ToolCatalog, *, block_id: str) -> ToolLine:
    """The durable Code Mode preview block (visible summary + instructions body)."""
    tools = catalog.total_tools
    namespaces = len(catalog.namespaces)
    summary = (
        f"Code Mode · {CODE_MODE_TOOL}() · orchestrate {tools} tools across "
        f"{namespaces} namespaces in one sandboxed pass"
    )
    body = tuple(render_instructions(catalog).splitlines())
    return ToolLine(id=block_id, summary=summary, body=body, status="completed", expanded=True)


def cmd_codemode(ctx: CommandContext, args: str) -> None:
    """``/codemode`` — post the Code Mode ``execute()`` catalog preview block."""
    del args
    ctx.post_block(build_codemode_block(governed_catalog(), block_id=ctx.next_block_id()))


__all__ = [
    "CODEMODE_COMMAND",
    "build_codemode_block",
    "cmd_codemode",
    "governed_catalog",
]
