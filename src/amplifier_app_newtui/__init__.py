"""amplifier-app-newtui — full-screen Textual TUI for Amplifier.

Layering contract (ADR-0007, enforced):

    ui/  ->  model/  ->  kernel/  ->  amplifier-core / amplifier-foundation

- ``kernel/`` owns every amplifier-core/foundation touchpoint and never
  imports Textual.
- ``model/`` is framework-agnostic typed state: it imports neither Textual
  nor amplifier-core.
- ``ui/`` is Textual-only presentation; all colors flow through theme
  tokens defined in :mod:`amplifier_app_newtui.ui.themes`.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
