"""Prompt-stash command handlers (/stashes, /unstash) via the fake context.

The handlers are thin: they parse args and delegate through the
``CommandContext`` seam (ADR-0007), so the app owns the composer/store.
"""

from __future__ import annotations

from amplifier_app_tui.commands.builtin import build_registry


def test_stashes_delegates_to_list(fake_command_context) -> None:
    build_registry().run("/stashes", fake_command_context)
    assert fake_command_context.calls == ["list_stashes"]


def test_unstash_no_arg_pops_most_recent(fake_command_context) -> None:
    build_registry().run("/unstash", fake_command_context)
    assert fake_command_context.calls == ["recall_stash:None"]


def test_unstash_with_index_recalls_that_entry(fake_command_context) -> None:
    build_registry().run("/unstash", fake_command_context, "2")
    assert fake_command_context.calls == ["recall_stash:2"]


def test_unstash_rejects_non_numeric_arg(fake_command_context) -> None:
    build_registry().run("/unstash", fake_command_context, "abc")
    assert fake_command_context.calls == []  # never reached the store
    assert fake_command_context.notices == ["usage: /unstash [n]"]
