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
from typing import Literal

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


# --------------------------------------------------------------------------
# bundle group — manage the active bundle + the discovery registry
# --------------------------------------------------------------------------


def _scope(
    is_global: bool, is_project: bool, is_local: bool
) -> Literal["global", "project", "local"]:
    """Resolve the scope flags to one scope (default: global, app-cli parity)."""
    del is_global
    if is_project:
        return "project"
    if is_local:
        return "local"
    return "global"


def _scope_options(fn):  # noqa: ANN001 — click decorator stack
    fn = click.option("--local", "is_local", is_flag=True, help="Write to .amplifier/settings.local.yaml.")(fn)
    fn = click.option("--project", "is_project", is_flag=True, help="Write to .amplifier/settings.yaml.")(fn)
    fn = click.option("--global", "is_global", is_flag=True, help="Write to ~/.amplifier/settings.yaml (default).")(fn)
    return fn


@main.group()
def bundle() -> None:
    """Manage bundles: list, show, use, add, remove, update."""


@bundle.command("list")
@click.option("--all", "all_bundles", is_flag=True, help="Include nested dependency bundles.")
def bundle_list(all_bundles: bool) -> None:
    """List available bundles (● marks the active one)."""
    from rich.console import Console
    from rich.table import Table

    from .kernel import bundle_admin
    from .kernel.config import DEFAULT_BUNDLE

    entries = bundle_admin.list_bundles(all_bundles=all_bundles)
    console = Console()
    if not entries:
        console.print("no bundles found")
        return

    table = Table(title="Available Bundles", title_justify="center", header_style="bold cyan")
    table.add_column("", width=1, no_wrap=True)  # active marker
    table.add_column("Name", style="green", no_wrap=True)
    table.add_column("Location", style="dim", overflow="fold")
    table.add_column("Status", no_wrap=True)
    for entry in entries:
        marker = "●" if entry.active else ""
        status = "app" if entry.source == "app" else ""
        location = entry.uri or ("(on disk)" if entry.source == "local" else "")
        name = f"[bold]{entry.name}[/bold]" if entry.active else entry.name
        table.add_row(marker, name, location, status)
    console.print(table)

    active = bundle_admin.current_bundle()
    console.print(
        f"Active: [green]{active}[/green]" if active else f"No bundle active ({DEFAULT_BUNDLE} default)",
        style="dim",
    )
    if not all_bundles:
        console.print("Use --all to include nested dependency bundles.", style="dim")


@bundle.command("current")
def bundle_current() -> None:
    """Show the active bundle name (or the built-in default)."""
    from .kernel import bundle_admin
    from .kernel.config import DEFAULT_BUNDLE

    active = bundle_admin.current_bundle()
    click.echo(active if active else f"{DEFAULT_BUNDLE} (default)")


@bundle.command("use")
@click.argument("name")
@_scope_options
def bundle_use(name: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Set NAME as the active bundle."""
    from .kernel import bundle_admin

    known = {e.name for e in bundle_admin.list_bundles()}
    if name not in known and not bundle_admin.is_bundle_uri(name):
        click.echo(f"unknown bundle: {name} · run `amplifier-newtui bundle list`", err=True)
        raise SystemExit(1)
    scope = _scope(is_global, is_project, is_local)
    path = bundle_admin.set_active_bundle(bundle_admin.settings_paths(None, None), name, scope)
    click.echo(f"active bundle → {name}  ({scope}: {path})")


@bundle.command("clear")
@_scope_options
def bundle_clear(is_global: bool, is_project: bool, is_local: bool) -> None:
    """Clear the active-bundle setting (revert to the default)."""
    from .kernel import bundle_admin

    scope = _scope(is_global, is_project, is_local)
    cleared = bundle_admin.clear_active_bundle(bundle_admin.settings_paths(None, None), scope)
    click.echo(f"cleared active bundle ({scope})" if cleared else f"nothing to clear ({scope})")


@bundle.command("show")
@click.argument("name")
def bundle_show(name: str) -> None:
    """Show a bundle's version, description, includes and mount counts."""
    from .kernel import bundle_admin

    info = asyncio.run(bundle_admin.load_bundle_info(name))
    if info is None:
        click.echo(f"could not load bundle: {name}", err=True)
        raise SystemExit(1)
    click.echo(f"{info.name} {info.version}".strip())
    if info.description:
        click.echo(f"  {' '.join(info.description.split())}")
    if info.uri:
        click.echo(f"  uri: {info.uri}")
    if info.includes:
        click.echo(f"  includes: {', '.join(info.includes)}")
    click.echo(
        f"  mounts: {info.providers} providers · {info.tools} tools · "
        f"{info.hooks} hooks · {info.agents} agents"
    )


@bundle.command("add")
@click.argument("uri")
@click.option("--name", "-n", default=None, help="Registry name (default: the bundle's own name).")
@click.option("--app", "as_app", is_flag=True, help="Also compose onto every session (overlay).")
@_scope_options
def bundle_add(
    uri: str, name: str | None, as_app: bool, is_global: bool, is_project: bool, is_local: bool
) -> None:
    """Register a bundle URI for discovery (validates it loads first)."""
    from .kernel import bundle_admin

    info = asyncio.run(bundle_admin.load_bundle_info(uri))
    if info is None:
        click.echo(f"could not load bundle from: {uri}", err=True)
        raise SystemExit(1)
    resolved_name = name or info.name
    scope = _scope(is_global, is_project, is_local)
    path = bundle_admin.add_bundle(
        bundle_admin.settings_paths(None, None), resolved_name, uri, scope, as_app=as_app
    )
    overlay = " · composed as app overlay" if as_app else ""
    click.echo(f"registered {resolved_name} → {uri}  ({scope}: {path}){overlay}")


@bundle.command("remove")
@click.argument("name")
@_scope_options
def bundle_remove(name: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Remove a bundle from the discovery registry."""
    from .kernel import bundle_admin

    scope = _scope(is_global, is_project, is_local)
    removed = bundle_admin.remove_bundle(bundle_admin.settings_paths(None, None), name, scope)
    click.echo(f"removed {name} ({scope})" if removed else f"not registered: {name} ({scope})")


@bundle.command("update")
@click.argument("name")
def bundle_update(name: str) -> None:
    """Check a bundle's sources for available updates."""
    from .kernel import bundle_admin

    summary = asyncio.run(bundle_admin.check_updates(name))
    if summary is None:
        click.echo(f"could not check updates for: {name}", err=True)
        raise SystemExit(1)
    click.echo(f"{name}: {summary}")


# --------------------------------------------------------------------------
# init — set up provider credentials (keys.env)
# --------------------------------------------------------------------------


def _match_provider(choices, token: str):  # noqa: ANN001, ANN202
    """Find the provider choice matching a user token (name/id/prefix)."""
    from .kernel.setup import provider_env_prefix

    needle = token.strip().lower()
    for choice in choices:
        if needle in {
            choice.module_id.lower(),
            provider_env_prefix(choice.module_id).lower(),
            choice.module_id.replace("provider-", "").lower(),
        }:
            return choice
    return None


async def _init(
    provider: str | None,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    yes: bool,
    from_env: bool,
) -> int:
    from .kernel import setup

    # Non-interactive env setup (CI/Docker), explicit opt-in: detect a provider
    # from env vars and write its config.providers entry — the key is already
    # exported. (Explicit flag only, so piped stdin never triggers a write.)
    if from_env:
        configured = await setup.auto_init_from_env()
        if configured:
            click.echo(f"auto-configured {configured} from environment")
            return 0
        click.echo("no provider credentials found in the environment", err=True)
        return 1

    status = setup.setup_status()
    click.echo(f"keys file: {status.keys_path}")
    click.echo(f"active bundle: {status.active_bundle or 'newtui (default)'}")
    click.echo(
        "stored keys: " + (", ".join(status.stored_keys) if status.stored_keys else "none")
    )

    choices = await setup.discover_providers()
    if not choices:
        click.echo("no provider modules discovered (is amplifier-core installed?)", err=True)
        return 1

    click.echo("\nproviders:")
    for index, choice in enumerate(choices, start=1):
        mark = "✓" if choice.has_key else " "
        click.echo(f"  {index}. [{mark}] {choice.module_id}  → {choice.key_var}")

    # Resolve the target provider.
    target = _match_provider(choices, provider) if provider else None
    if provider and target is None:
        click.echo(f"unknown provider: {provider}", err=True)
        return 1
    if target is None:
        if yes:
            # Non-interactive with no provider selected → status only.
            return 0
        raw = click.prompt("\nset up which provider? (number, or blank to skip)", default="", show_default=False)
        if not raw.strip():
            return 0
        try:
            target = choices[int(raw) - 1]
        except (ValueError, IndexError):
            click.echo(f"invalid selection: {raw}", err=True)
            return 1

    # Resolve the API key.
    if api_key is None:
        if yes:
            click.echo(f"--api-key required with --yes for {target.module_id}", err=True)
            return 1
        api_key = click.prompt(f"{target.key_var}", hide_input=True, default="", show_default=False)
    key = (api_key or "").strip()
    if not key:
        click.echo("no key entered · nothing written")
        return 0

    from .kernel import bundle_admin

    path = setup.keys_file()
    setup.write_key(path, target.key_var, key)
    written = [target.key_var]
    if base_url:
        setup.write_key(path, target.base_url_var, base_url.strip())
        written.append(target.base_url_var)
    # Persist the provider into config.providers so it actually mounts — not
    # just a key in keys.env. ${VAR} placeholders reference the keys above.
    entry = setup.provider_config_entry(
        target.module_id,
        key_var=target.key_var,
        model=(model or "").strip() or None,
        base_url=base_url.strip() if base_url else None,
        base_url_var=target.base_url_var,
    )
    cfg_path = setup.write_provider_config(
        bundle_admin.settings_paths(None, None), "global", entry
    )
    click.echo(f"\nwrote {', '.join(written)} → {path}")
    click.echo(f"configured provider {target.module_id} → {cfg_path}")
    click.echo("run `amplifier-newtui` to start a session.")
    return 0


@main.command()
@click.option("--provider", "-p", default=None, help="Provider to set up (e.g. anthropic).")
@click.option("--api-key", default=None, help="API key (non-interactive; else prompted).")
@click.option("--base-url", default=None, help="Optional provider base-URL override.")
@click.option("--model", default=None, help="Default model for the provider.")
@click.option("--from-env", is_flag=True, help="Non-interactive: configure a provider detected from env vars.")
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: never prompt (needs --api-key).")
def init(
    provider: str | None,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    from_env: bool,
    yes: bool,
) -> None:
    """Set up a provider: writes the API key to ~/.amplifier/keys.env and the
    provider entry to settings (config.providers)."""
    raise SystemExit(asyncio.run(_init(provider, api_key, base_url, model, yes, from_env)))


# --------------------------------------------------------------------------
# update — refresh the bundles/modules newtui mounts (foundation cache)
# --------------------------------------------------------------------------


async def _update(check_only: bool, yes: bool, force: bool) -> int:
    from rich.console import Console
    from rich.table import Table

    from .kernel import updater

    console = Console()
    if force:
        console.print("clearing uv cache…", style="dim")
        updater.uv_cache_clean()

    statuses = await updater.check_bundles()
    if not statuses:
        console.print("no bundles to check")
        console.print(updater.self_update_hint(), style="dim")
        return 0

    table = Table(title="Bundle updates", title_justify="center", header_style="bold cyan")
    table.add_column("Bundle", style="green", no_wrap=True)
    table.add_column("Status")
    for status in statuses:
        mark = "[yellow]●[/yellow]" if status.has_updates else "[green]✓[/green]"
        table.add_row(status.name, f"{mark} {status.summary}")
    console.print(table)

    stale = [s for s in statuses if s.has_updates]
    if not stale and not force:
        console.print("✓ all bundles up to date", style="green")
        console.print(updater.self_update_hint(), style="dim")
        return 0
    if check_only:
        console.print(updater.self_update_hint(), style="dim")
        return 0

    targets = statuses if force else stale
    if not yes and not click.confirm(f"update {len(targets)} bundle(s)?", default=True):
        return 0
    updated, failed = await updater.update_bundles([s.target for s in targets])
    if updated:
        console.print(f"✓ updated: {', '.join(updated)}", style="green")
    if failed:
        console.print(f"✗ failed: {', '.join(failed)}", style="red")
    console.print(updater.self_update_hint(), style="dim")
    return 1 if failed else 0


@main.command()
@click.option("--check-only", is_flag=True, help="Report available updates; change nothing.")
@click.option("--yes", "-y", is_flag=True, help="Apply without the confirmation prompt.")
@click.option("--force", is_flag=True, help="uv cache clean first, then re-fetch every source.")
def update(check_only: bool, yes: bool, force: bool) -> None:
    """Update the bundles/modules this app mounts (not the app or platform)."""
    raise SystemExit(asyncio.run(_update(check_only, yes, force)))


if __name__ == "__main__":
    main()
