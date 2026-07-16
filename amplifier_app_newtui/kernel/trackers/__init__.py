"""Hook trackers: small pure-state classes consuming normalized UIEvents.

Pattern (ported from amplifier-app-cli ``ui/stream_status.py`` et al.):
each tracker declares an ``EVENTS`` tuple, exposes
``async handle_event(event, data) -> HookResult``,
``register_hooks(hooks, *, priority) -> unregister`` and
``add_listener(cb) -> remove``. Trackers hold state only; the app wires
listeners to Textual message posting.
"""
