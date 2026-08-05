"""Test-only deterministic TUI entry point with one native-shaped question."""

from __future__ import annotations

from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter


def main() -> None:
    """Launch the real Textual app with a pre-parked custom decision."""
    adapter = DemoRuntimeAdapter()
    adapter.needs_you.defer(
        "Which test label should I use?",
        "Test label",
        choices=("Alpha", "Beta"),
        custom=True,
    )
    TuiApp(adapter).run()


if __name__ == "__main__":
    main()
