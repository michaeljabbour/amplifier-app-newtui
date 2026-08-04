"""Every op serve services is classified -- so no mutation can go unattributed.

AC4 says every automated action must be attributable. Before this, that was
true of the control plane and the five transcript writes, and quietly false
everywhere else: ``tag.add`` / ``tag.remove`` / ``effort.set`` / ``effort.cycle``
changed the session with no lease check and no audit entry at all. A controller
could rename, retag and re-tier a session it did not hold the pen for, and the
trail would show nothing.

Fixing the four that existed is not the interesting part -- the interesting
part is making the sixth one, added next year by someone who has never read
this file, impossible to get wrong. So classification is not a convention: it
is :data:`~amplifier_app_tui.kernel.serve.OP_PERMISSIONS`, a table the dispatch
loop reads. An op in it as ``write`` is routed through
``SessionControl.authorize``, which cannot accept it without appending
``write.accepted``; an op missing from it fails the first test below.

That is the whole mechanism: the audit trail is not something an implementer
must remember to call, it is something the routing does on their behalf, and
this file is the tripwire on the routing.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel import serve as serve_module
from amplifier_app_tui.kernel.serve import (
    OP_PERMISSIONS,
    _CONTROL_OPS,
    _GUARDED_OPS,
    _META_OPS,
    _WRITE_OPS,
)
from amplifier_app_tui.kernel.session_authz import CONTROL, PERMISSIONS, READ, WRITE

SOURCE = Path(inspect.getfile(serve_module))


def _dispatched_ops() -> set[str]:
    """Every op literal the protocol loop can branch on.

    Read out of the AST rather than a hand-kept list, because a hand-kept list
    is exactly the thing that drifts. Collects both ``kind == "..."`` compares
    and ``kind in <SET>`` memberships, resolving the set names against the
    module.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_loop"
    )
    found: set[str] = set()
    for node in ast.walk(loop):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id in {"kind", "kind_str"}):
            continue
        for comparator in node.comparators:
            found |= _literals(comparator)
    return found


def _literals(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        resolved = getattr(serve_module, node.id, None)
        if isinstance(resolved, str):
            return {resolved}
        if isinstance(resolved, frozenset | set | tuple | list):
            return {str(item) for item in resolved}
        return set()
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return {lit for element in node.elts for lit in _literals(element)}
    return set()


def test_every_dispatched_op_is_classified() -> None:
    """The tripwire.

    Add an op to the loop without adding it to ``OP_PERMISSIONS`` and this
    fails -- which is the only reason "no mutation path can bypass
    attribution" is a property of the code rather than a promise in a doc.
    """
    unclassified = _dispatched_ops() - set(OP_PERMISSIONS) - _META_OPS
    assert not unclassified, (
        f"unclassified op(s) {sorted(unclassified)}: add them to serve.OP_PERMISSIONS "
        "(READ observes, WRITE mutates and is audited, CONTROL owns the lease)"
    )


def test_the_registry_only_names_ops_the_loop_actually_handles() -> None:
    """Drift the other way: a stale entry claims a guarantee for nothing."""
    dispatched = _dispatched_ops()
    orphans = set(OP_PERMISSIONS) - dispatched
    assert not orphans, f"registry names op(s) the loop never dispatches: {sorted(orphans)}"


def test_permissions_come_from_the_closed_vocabulary() -> None:
    assert set(OP_PERMISSIONS.values()) <= PERMISSIONS


def test_the_previously_unaudited_mutations_are_now_writes() -> None:
    """The specific gap, named.

    These four mutated the session with no attribution whatsoever. They are
    the reason this file exists, and pinning them by name is what stops a
    future "it's only metadata" argument from quietly un-fixing it.
    """
    for op in ("tag.add", "tag.remove", "effort.set", "effort.cycle"):
        assert OP_PERMISSIONS[op] == WRITE, f"{op} mutates the session; it must be audited"
        assert op in _WRITE_OPS


def test_transcript_writes_stayed_writes() -> None:
    for op in ("submit", "steer", "approve", "decision", "interrupt"):
        assert OP_PERMISSIONS[op] == WRITE


def test_reads_are_never_lease_gated() -> None:
    """Observation must not need the pen.

    In particular ``history.replay`` stays a READ: a participant reattaching
    has to be able to catch up without touching the lease or the transcript.
    (``audit.query`` / ``handoff.list`` are READs that happen to be *serviced*
    by the control-plane handler -- ``_CONTROL_OPS`` is a routing set, not a
    permission class, and neither of them touches the lease.)
    """
    reads = {op for op, need in OP_PERMISSIONS.items() if need == READ}
    assert "history.replay" in reads
    assert "lease.status" in reads
    assert "session.status" in reads
    assert not reads & _WRITE_OPS


def test_the_control_permission_is_a_subset_of_the_control_router() -> None:
    """Everything needing ``control`` is serviced by the control plane itself,
    so no ownership change can be made anywhere else in the loop."""
    owning = {op for op, need in OP_PERMISSIONS.items() if need == CONTROL}
    assert owning <= _CONTROL_OPS


def test_seizing_the_session_is_its_own_permission_class() -> None:
    """Driving a session and owning it are different powers, exactly as the
    downstream grant vocabulary (B8) assumes."""
    for op in ("lease.acquire", "lease.takeover", "session.pause", "handoff.claim"):
        assert OP_PERMISSIONS[op] == CONTROL
        assert op in _CONTROL_OPS


def test_guarded_ops_are_exactly_the_registry() -> None:
    assert _GUARDED_OPS == frozenset(OP_PERMISSIONS)


@pytest.mark.parametrize("op", sorted(_META_OPS))
def test_lifecycle_ops_carry_no_permission(op: str) -> None:
    """``quit`` / EOF close a connection; they change no session state."""
    assert op not in OP_PERMISSIONS


def test_the_registry_is_the_single_source_for_the_write_set() -> None:
    """Derived, not duplicated -- the two cannot drift."""
    assert _WRITE_OPS == frozenset(op for op, need in OP_PERMISSIONS.items() if need == WRITE)


def test_no_op_is_classified_twice() -> None:
    counts: dict[str, Any] = {}
    for op in OP_PERMISSIONS:
        counts[op] = counts.get(op, 0) + 1
    assert all(count == 1 for count in counts.values())
