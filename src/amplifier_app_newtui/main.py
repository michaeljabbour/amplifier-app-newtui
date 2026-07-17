"""Thin async click entry point (``amplifier-newtui``).

Default invocation launches the full-screen TUI on a real amplifier
session (RealRuntime); ``--demo`` swaps in the scripted DemoRuntime
(fully offline — no bundle, no network, no credentials). Subcommands:

- ``run PROMPT``   — one-shot session: execute one prompt, print result.
- ``sessions``     — list stored session ids for this project.
- ``resume ID``    — launch the TUI resuming a stored session.
- ``doctor``       — plain-text setup checkup (exit 0 ok / 1 findings).

Contract: ``main()`` is the console-script entry; every async body runs
under a single ``asyncio.run`` — no sync/async bridging deeper down.
"""

from __future__ import annotations

import asyncio

import click

from . import __version__


async def _launch_tui(
    *, demo: bool, bundle: str | None = None, resume_id: str | None = None
) -> int:
    from .ui.app import NewTuiApp
    from .ui.term_probe import patch_legacy_alt_named_keys, probe_kitty_protocol

    patch_legacy_alt_named_keys()

    if demo:
        from .ui.demo_wiring import DemoRuntimeAdapter

        adapter = DemoRuntimeAdapter()
    else:
        from .ui.runtime_adapter import RealRuntimeAdapter

        adapter = RealRuntimeAdapter(bundle=bundle, resume_id=resume_id)
    app = NewTuiApp(adapter, kitty_protocol=probe_kitty_protocol())
    await app.run_async()
    return app.return_code or 0


async def _run_once(prompt: str, bundle: str | None) -> int:
    from .kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=bundle)
    await runtime.start()
    try:
        click.echo(await runtime.submit(prompt))
    finally:
        await runtime.cleanup()
    return 0


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run the scripted DemoRuntime instead of a real session.")
@click.option("--bundle", default=None, help="Bundle name or URI (default: settings/bundled).")
@click.version_option(__version__, prog_name="amplifier-newtui")
@click.pass_context
def main(ctx: click.Context, demo: bool, bundle: str | None) -> None:
    """Amplifier full-screen TUI (v3 Cohesive)."""
    if ctx.invoked_subcommand is not None:
        return
    raise SystemExit(asyncio.run(_launch_tui(demo=demo, bundle=bundle)))


@main.command()
@click.argument("prompt")
@click.option("--bundle", default=None, help="Bundle name or URI.")
def run(prompt: str, bundle: str | None) -> None:
    """Execute one prompt in a real session and print the response."""
    raise SystemExit(asyncio.run(_run_once(prompt, bundle)))


@main.command()
def sessions() -> None:
    """List stored session ids for this project."""
    from .kernel.runtime import list_sessions

    stored = list_sessions()
    if not stored:
        click.echo("no stored sessions")
        return
    for session_id in stored:
        click.echo(session_id)


@main.command()
@click.argument("session_id")
@click.option("--bundle", default=None, help="Bundle name or URI.")
def resume(session_id: str, bundle: str | None) -> None:
    """Launch the TUI resuming a stored session."""
    raise SystemExit(
        asyncio.run(_launch_tui(demo=False, bundle=bundle, resume_id=session_id))
    )


@main.command()
def doctor() -> None:
    """Setup checkup: prints the report, exit 1 when findings exist."""
    from .commands.doctor import run_standalone

    raise SystemExit(run_standalone())


if __name__ == "__main__":
    main()
