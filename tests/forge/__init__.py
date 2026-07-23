"""Forge-driven capability test tier (issue #49).

An opt-in ``@pytest.mark.forge`` tier that drives the shipped
``amplifier-newtui`` binary through a real PTY via the ``amplifier-skill-forge``
terminal daemon.  It is excluded from the default gate (``addopts = -m
"not forge"``) because it needs a PTY + the forge daemon; run it with
``uv run pytest -q -m forge tests/forge/`` (or ``scripts/forge_capability.sh``).

See ``docs/plans/2026-07-22-forge-capability-tier.md`` for the design and
``docs/DEVELOPMENT.md`` (Forge capability tier) for how to run it.
"""
