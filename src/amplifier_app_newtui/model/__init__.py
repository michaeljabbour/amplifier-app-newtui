"""Model layer: framework-agnostic typed state.

Modules in this package import neither Textual nor amplifier-core.
Everything here is plain typed Python (pydantic v2 frozen models for data,
small stateful classes for queues/registries) so it is unit-testable
without any UI framework or kernel installed.
"""
