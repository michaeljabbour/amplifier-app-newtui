"""Kernel layer: every amplifier-core/foundation touchpoint lives here.

Modules in this package may import amplifier-core and amplifier-foundation
but must NEVER import Textual. The UI consumes the kernel exclusively
through the normalized :class:`~amplifier_app_newtui.kernel.events.UIEvent`
union and typed queues — no raw hook payload ever crosses this boundary.
"""
