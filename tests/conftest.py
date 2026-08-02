"""Shared fixtures for the test suite.

``fake_command_context`` provides a recording implementation of the
``commands.registry.CommandContext`` protocol for the pure-logic command
tests (no Textual involved).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_tui.commands.context import ContextUsage
from amplifier_app_tui.model.blocks import BlockIdAllocator
from amplifier_app_tui.model.queues import NeedsYouQueue, SteeringQueue
from amplifier_app_tui.model.trust import DenialLog
from amplifier_app_tui.model.turn import OutcomeLedger

# Share the offline fake-module workspace fixtures so both test_runtime_offline
# and test_serve_offline resolve them without a by-name import.
from tests.test_runtime_offline import offline_env, offline_workspace  # noqa: F401,E402


class FakeCommandContext:
    """Records every action a command handler takes (CommandContext fake)."""

    def __init__(self) -> None:
        self._ledger = OutcomeLedger()
        self._denial_log = DenialLog()
        self._steering = SteeringQueue()
        self._needs_you = NeedsYouQueue()
        self._ids = BlockIdAllocator()
        self._usage = ContextUsage(conversation=52_000, tools=18_000, memory=8_000)
        self.session_cost = Decimal("0")
        self.mcp_stats: tuple = ()
        self.tallies: tuple = ()
        self.overrides: tuple = ()
        self.answer_chars: int = 42
        self.user_lines: list[str] = []
        self.blocks: list = []
        self.notices: list[str] = []
        self.calls: list[str] = []

    # data surfaces
    @property
    def ledger(self) -> OutcomeLedger:
        return self._ledger

    @property
    def denial_log(self) -> DenialLog:
        return self._denial_log

    @property
    def steering(self) -> SteeringQueue:
        return self._steering

    @property
    def needs_you(self) -> NeedsYouQueue:
        return self._needs_you

    @property
    def session_short(self) -> str:
        return "a1b2c3"

    @property
    def bundle_name(self) -> str:
        return "dev-bundle"

    def next_block_id(self) -> str:
        return self._ids.next_id()

    def context_usage(self) -> ContextUsage:
        return self._usage

    def approval_tallies(self) -> tuple:
        return self.tallies

    def overridden_denials(self) -> tuple:
        return self.overrides

    def mcp_server_stats(self) -> tuple:
        return self.mcp_stats

    def mount_report(self):
        return getattr(self, "mounts", None)

    # actions
    def echo_user_line(self, text: str) -> None:
        self.user_lines.append(text)

    def post_block(self, block) -> None:
        self.blocks.append(block)

    def show_notice(self, text: str) -> None:
        self.notices.append(text)

    def cycle_mode(self) -> None:
        self.calls.append("cycle_mode")

    def set_mode(self, mode_id: str) -> None:
        self.calls.append(f"set_mode:{mode_id}")

    def set_theme(self, name: str) -> None:
        self.calls.append(f"set_theme:{name}")

    def toggle_lanes(self) -> None:
        self.calls.append("toggle_lanes")

    def open_rewind(self) -> None:
        self.calls.append("open_rewind")

    def open_permissions(self) -> None:
        self.calls.append("open_permissions")

    def manage_directories(self, kind: str, args: str) -> None:
        self.calls.append(f"manage_directories:{kind}:{args}")

    def export_transcript(self) -> str:
        self.calls.append("export_transcript")
        return "exports/a1b2c3-20260101-000000.md"

    def copy_answer(self) -> int:
        self.calls.append("copy_answer")
        return self.answer_chars

    def about_info(self) -> tuple[str, str, str, str]:
        self.calls.append("about_info")
        return ("0.1.0", "1.2.3", self.bundle_name, self.session_short)

    def quit_app(self) -> None:
        self.calls.append("quit_app")

    def show_modes(self) -> None:
        self.calls.append("show_modes")

    def set_native_mode(self, name: str | None) -> None:
        self.calls.append(f"set_native_mode:{name}")

    def remove_native_mode(self, name: str) -> None:
        self.calls.append(f"remove_native_mode:{name}")

    def show_status(self) -> None:
        self.calls.append("show_status")

    def show_model(self, arg: str) -> None:
        self.calls.append(f"show_model:{arg}")

    def apply_effort(self, arg: str) -> None:
        self.calls.append(f"apply_effort:{arg}")

    def compact_context(self, focus: str) -> None:
        self.calls.append(f"compact_context:{focus}")

    def clear_context(self) -> None:
        self.calls.append("clear_context")

    def show_tools(self) -> None:
        self.calls.append("show_tools")

    def show_agents(self) -> None:
        self.calls.append("show_agents")

    def show_diff(self, arg: str) -> None:
        self.calls.append(f"show_diff:{arg}")

    def show_skills(self) -> None:
        self.calls.append("show_skills")

    def load_skill(self, name: str) -> None:
        self.calls.append(f"load_skill:{name}")

    def manage_mcp(self, args: str) -> None:
        self.calls.append(f"manage_mcp:{args}")

    def load_bundle(self, args: str) -> None:
        self.calls.append(f"load_bundle:{args}")

    def manage_config(self, args: str) -> None:
        self.calls.append(f"manage_config:{args}")

    def rename_session(self, name: str) -> None:
        self.calls.append(f"rename_session:{name}")

    def show_sessions(self) -> None:
        self.calls.append("show_sessions")

    def branch_session(self, name: str) -> None:
        self.calls.append(f"branch_session:{name}")

    def fork_session(self, directive: str) -> None:
        self.calls.append(f"fork_session:{directive}")

    def manage_tags(self, args: str) -> None:
        self.calls.append(f"manage_tags:{args}")

    def recall_stash(self, index: int | None) -> None:
        self.calls.append(f"recall_stash:{index}")

    def list_stashes(self) -> None:
        self.calls.append("list_stashes")


@pytest.fixture
def fake_command_context() -> FakeCommandContext:
    return FakeCommandContext()


@pytest.fixture(autouse=True)
def _offline_pricing(tmp_path, monkeypatch):
    """Keep the suite fully offline and pricing-deterministic.

    - ``fetch_live_pricing`` is stubbed to ``None`` (no Helicone traffic
      even if a code path starts the background fetch).
    - The on-disk pricing cache is redirected to a per-test tmp file so
      tests never read/write the user's real ``~/.amplifier`` cache.
    - The process-wide active pricing table is reset to the fallback
      around every test (it is module-level mutable state by design).
    """
    from amplifier_app_tui.kernel import cost

    monkeypatch.setattr(cost, "fetch_live_pricing", lambda timeout=5.0: None)
    monkeypatch.setattr(cost, "PRICING_CACHE_PATH", tmp_path / "pricing_cache.json")
    cost.set_active_pricing_table(None)
    yield
    cost.set_active_pricing_table(None)


@pytest.fixture(autouse=True)
def _offline_provider_setup(monkeypatch):
    """Keep provider setup offline and independent of the developer's env.

    ``init``/``provider add`` gained two seams that reach outside the process —
    fetching an uninstalled provider module from git, and calling a provider's
    ``list_models()`` against a real endpoint. Both are stubbed inert here so
    no test can accidentally clone a repo or hit a network, and the
    ``load_provider_info`` memo is cleared around every test because fakes are
    swapped in per-test.

    The provider credential vars are also cleared: with schema-aware ``--yes``,
    a developer's real ``ANTHROPIC_API_KEY`` would otherwise change outcomes.
    """
    from amplifier_app_tui.kernel import setup

    async def _unavailable(module_id, source_uri, **kwargs):
        return setup.ProviderAvailability(module_id, False, reason="offline in tests")

    async def _no_models(*args, **kwargs):
        return setup.ModelCatalog()

    monkeypatch.setattr(setup, "ensure_provider_available", _unavailable)
    monkeypatch.setattr(setup, "list_provider_models", _no_models)
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "GITHUB_TOKEN",
        "VLLM_API_KEY",
        "VLLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    setup.reset_provider_info_cache()
    yield
    setup.reset_provider_info_cache()
