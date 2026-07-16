"""UI layer: Textual-only presentation.

Widgets own their state and communicate via Textual messages (no mixin
god-objects — ADR-0007). All color flows through the theme variables
defined in :mod:`amplifier_app_newtui.ui.themes`; hex values appear
nowhere else in this package.
"""
