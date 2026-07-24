"""Thin async click entry point (``amplifier-newtui``).

Default invocation launches the full-screen TUI on a real amplifier
session (RealRuntime); ``--demo`` swaps in the scripted DemoRuntime
(fully offline — no bundle, no network, no credentials). Subcommands:

- ``run [PROMPT]`` — one-shot session from an argument or piped stdin;
  emits text, one-document JSON, or live versioned JSONL events.
- ``sessions``     — list stored session ids for this project.
- ``resume ID``    — launch the TUI resuming a stored session.
- ``doctor``       — plain-text setup checkup (exit 0 ok / 1 findings).

Contract: ``main()`` is the console-script entry; every async body runs
under a single ``asyncio.run`` — no sync/async bridging deeper down.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from time import monotonic
from typing import IO, Any, Literal, cast

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


async def _run_once(
    prompt: str,
    bundle: str | None,
    output_format: Literal["text", "json", "json-trace", "jsonl"],
    *,
    mode: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    resume_id: str | None = None,
    jsonl_output: IO[str] | None = None,
) -> int:
    from .kernel.runtime import RealRuntime

    # Per-invocation overrides are threaded through the kernel seam and stay
    # ephemeral: --model/--provider mutate only the resolved in-memory plan,
    # --mode seeds the runtime posture, --resume replays a stored session's
    # context. Only non-default kwargs are passed so the untouched call remains
    # ``RealRuntime(bundle=bundle)``.
    runtime_kwargs: dict[str, Any] = {"bundle": bundle}
    if resume_id is not None:
        runtime_kwargs["resume_id"] = resume_id
    if model is not None:
        runtime_kwargs["model_override"] = model
    if provider is not None:
        runtime_kwargs["provider_override"] = provider
    if mode is not None:
        mode_value = mode
        runtime_kwargs["mode"] = lambda: mode_value
    runtime = RealRuntime(**runtime_kwargs)
    json_mode = output_format in ("json", "json-trace", "jsonl")
    started = monotonic()
    response = ""
    error: Exception | None = None
    session_id = ""
    bundle_name = bundle or ""
    model_name = ""

    async def execute() -> None:
        nonlocal response, error, session_id, bundle_name, model_name
        try:
            await runtime.start()
            session_id = runtime.session_id
            bundle_name = runtime.bundle_name
            model_name = runtime.model_name
            response = await runtime.submit(prompt)
        except Exception as caught:  # noqa: BLE001 — structured error is part of the CLI contract
            error = caught
        finally:
            try:
                await runtime.cleanup()
            except Exception as caught:  # noqa: BLE001 — best-effort finally cleanup: keep the original error if teardown also fails
                if error is None:
                    error = caught

    if output_format == "jsonl":
        from .kernel.jsonl import JsonlRecord, JsonlRecords

        records = JsonlRecords()
        output = jsonl_output or sys.stdout

        def emit(record: JsonlRecord) -> None:
            output.write(record.model_dump_json(fallback=str) + "\n")
            output.flush()

        # Hold the caller's stdout handle while runtime/module print() calls
        # are redirected.  JSONL records still reach the original stream as
        # soon as their normalized UIEvent enters the queue.
        with redirect_stdout(sys.stderr):
            try:
                await runtime.start()
                session_id = runtime.session_id
                bundle_name = runtime.bundle_name
                model_name = runtime.model_name
                emit(
                    records.session_started(
                        session_id=session_id,
                        bundle=bundle_name,
                        model=model_name,
                    )
                )

                submit = asyncio.create_task(runtime.submit(prompt))
                while not submit.done():
                    next_event = asyncio.create_task(runtime.queue.get())
                    done, _pending = await asyncio.wait(
                        (submit, next_event), return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_event in done:
                        emit(records.runtime_event(next_event.result()))
                    else:
                        next_event.cancel()
                        try:
                            await next_event
                        except asyncio.CancelledError:
                            pass
                while not runtime.queue.empty():
                    emit(records.runtime_event(runtime.queue.get_nowait()))
                response = await submit
            except Exception as caught:  # noqa: BLE001 — jsonl error path: any failure is emitted as a structured error record
                error = caught
                while not runtime.queue.empty():
                    emit(records.runtime_event(runtime.queue.get_nowait()))
            finally:
                try:
                    await runtime.cleanup()
                except Exception as caught:  # noqa: BLE001 — best-effort finally cleanup: keep the original error if teardown also fails
                    if error is None:
                        error = caught

        duration_ms = round((monotonic() - started) * 1000, 3)
        if error is None:
            emit(
                records.turn_completed(
                    session_id=session_id,
                    response=response,
                    duration_ms=duration_ms,
                )
            )
            return 0
        emit(
            records.error(
                session_id=session_id,
                error=error,
                duration_ms=duration_ms,
            )
        )
        return 1

    if json_mode:
        # Bundle/module diagnostics and accidental print() calls belong on
        # stderr. stdout is exactly one parseable JSON document.
        with redirect_stdout(sys.stderr):
            await execute()
        if error is None:
            payload: dict[str, object] = {
                "status": "success",
                "response": response,
                "session_id": session_id,
                "bundle": bundle_name,
                "model": model_name,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            payload = {
                "status": "error",
                "error": str(error),
                "error_type": type(error).__name__,
                "session_id": session_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        if output_format == "json-trace":
            trace = []
            while not runtime.queue.empty():
                trace.append(runtime.queue.get_nowait().model_dump(mode="json"))
            payload["execution_trace"] = trace
            payload["metadata"] = {
                "event_count": len(trace),
                "duration_ms": round((monotonic() - started) * 1000, 3),
            }
        click.echo(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if error is None else 1

    await execute()
    if error is not None:
        click.echo(f"Error: {error}", err=True)
        return 1
    click.echo(response)
    return 0


def _resolve_run_prompt(prompt: str | None) -> str:
    if prompt is not None:
        return prompt
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    raise click.UsageError("Prompt required (pass PROMPT or pipe content on stdin)")


async def _first_run_gate() -> int | None:
    """Launch-time provider gate (app-cli's ``check_first_run`` wiring).

    Ported from amplifier-app-cli ``run.py`` / ``session_runner.py``: when no
    provider can be mounted, an interactive terminal is walked through provider
    setup *before* the full-screen TUI takes over; a non-interactive shell
    falls back to env-var auto-init. Returns ``None`` to proceed to launch, or
    an exit code to stop (nothing to onboard). ``--demo`` skips this entirely.
    """
    from .kernel import setup

    if setup.has_configured_provider():
        return None
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        configured = await setup.auto_init_from_env()
        if configured:
            click.echo(f"auto-configured {configured} from environment", err=True)
            return None
        click.echo(
            "No AI provider configured. Run `amplifier-newtui init` or export a "
            "provider key (e.g. ANTHROPIC_API_KEY) to get started.",
            err=True,
        )
        return 1
    click.echo("Welcome to Amplifier — no AI provider is configured yet. Let's set one up.\n")
    code = await _init(
        provider=None, api_key=None, base_url=None, model=None, yes=False, from_env=False
    )
    if code != 0:
        return code
    if setup.has_configured_provider():
        click.echo("")  # spacer before the full-screen TUI takes over
        return None
    click.echo("\nNo provider configured yet. Run `amplifier-newtui` again when ready.")
    return 0


@click.group(invoke_without_command=True)
@click.option(
    "--demo", is_flag=True, help="Run the scripted DemoRuntime instead of a real session."
)
@click.option("--bundle", default=None, help="Bundle name or URI (default: settings/bundled).")
@click.version_option(__version__, prog_name="amplifier-newtui")
@click.pass_context
def main(ctx: click.Context, demo: bool, bundle: str | None) -> None:
    """Amplifier full-screen TUI (v3 Cohesive)."""
    if ctx.invoked_subcommand is not None:
        return
    if not demo:
        gate = asyncio.run(_first_run_gate())
        if gate is not None:
            raise SystemExit(gate)
    raise SystemExit(asyncio.run(_launch_tui(demo=demo, bundle=bundle)))


@main.command()
@click.argument("prompt", required=False)
@click.option("--bundle", default=None, help="Bundle name or URI.")
@click.option(
    "--model",
    "-m",
    default=None,
    help="Model override for THIS invocation only (requires --provider; not persisted).",
)
@click.option(
    "--provider",
    "-p",
    default=None,
    help="Provider override for THIS invocation only (not persisted to settings).",
)
@click.option(
    "--mode",
    "mode",
    default=None,
    help="Interaction mode to start in (chat, plan, brainstorm, build, auto).",
)
@click.option(
    "--resume",
    "resume",
    default=None,
    metavar="SESSION_ID",
    help="Seed this one-shot from an existing session's stored context.",
)
@click.option(
    "--output-format",
    type=click.Choice(("text", "json", "json-trace", "jsonl")),
    default="text",
    show_default=True,
    help="Response format; JSON modes reserve stdout for machine-readable output.",
)
def run(
    prompt: str | None,
    bundle: str | None,
    model: str | None,
    provider: str | None,
    mode: str | None,
    resume: str | None,
    output_format: str,
) -> None:
    """Execute PROMPT (or piped stdin) in one real session.

    ``--model``/``--provider`` override the resolved plan for THIS invocation
    only (never written to a settings scope); ``--mode`` seeds the interaction
    posture; ``--resume`` seeds the run from a stored session's context.
    """
    from .model.modes import MODE_PROFILES

    # --model without --provider is ambiguous (which provider hosts it?) --
    # match the reference CLI and refuse early (amplifier-app-cli run.py).
    if model is not None and provider is None:
        click.echo(
            "Error: --model requires --provider (name the provider that hosts the model)",
            err=True,
        )
        raise SystemExit(1)
    # --mode must name a real interaction mode; unknown ids fail loud rather
    # than silently falling back to the default posture.
    if mode is not None and mode not in MODE_PROFILES:
        valid = ", ".join(MODE_PROFILES)
        click.echo(f"Error: unknown mode '{mode}' · valid modes: {valid}", err=True)
        raise SystemExit(1)
    # --resume resolves a (possibly partial) id to one stored session up front,
    # so an unknown/ambiguous id errors clearly before any boot work begins.
    resume_id: str | None = None
    if resume is not None:
        from .kernel import session_manager

        try:
            resume_id = session_manager.resolve(_session_store(), resume)
        except FileNotFoundError:
            click.echo(f"no session found matching '{resume}'", err=True)
            raise SystemExit(1) from None
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from None
    resolved_prompt = _resolve_run_prompt(prompt)
    raise SystemExit(
        asyncio.run(
            _run_once(
                resolved_prompt,
                bundle,
                cast(Literal["text", "json", "json-trace", "jsonl"], output_format),
                mode=mode,
                model=model,
                provider=provider,
                resume_id=resume_id,
            )
        )
    )


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


def _session_store():  # noqa: ANN202 — SessionStore (lazy import keeps --demo offline)
    from .kernel.persistence import SessionStore

    return SessionStore()


def _pick_session_id(limit: int) -> str | None:
    """Print a numbered picker of recent sessions; return the chosen id.

    The interactive counterpart to ``resume ID`` (amplifier-app-cli
    ``resume`` with no argument): a single-session store auto-selects, an
    empty store returns ``None`` with a hint, and ``q`` cancels. Numbering
    is 1-based over the newest-first listing.
    """
    from .kernel import session_manager

    summaries = session_manager.list_summaries(_session_store(), limit=limit)
    if not summaries:
        click.echo("no stored sessions · start one with `amplifier-newtui`")
        return None
    if len(summaries) == 1:
        click.echo(f"only one session · resuming {summaries[0].short_id}")
        return summaries[0].session_id
    click.echo("Recent sessions:")
    for index, summary in enumerate(summaries, start=1):
        label = f"{summary.name} · " if summary.name else ""
        click.echo(
            f"  [{index}] {label}{summary.short_id} · {summary.bundle} · "
            f"{summary.messages} msgs · {summary.time_ago}"
        )
    raw = click.prompt("resume which? (number, or q to cancel)", default="q", show_default=False)
    choice = raw.strip().lower()
    if choice in ("q", "quit", "exit", ""):
        click.echo("cancelled")
        return None
    try:
        selected = summaries[int(choice) - 1]
    except (ValueError, IndexError):
        click.echo(f"invalid selection: {raw}", err=True)
        return None
    return selected.session_id


@main.command()
@click.argument("session_id", required=False, default=None)
@click.option("--bundle", default=None, help="Bundle name or URI.")
@click.option("--limit", "-n", default=10, show_default=True, help="Sessions shown in the picker.")
def resume(session_id: str | None, bundle: str | None, limit: int) -> None:
    """Launch the TUI resuming a stored session (interactive picker if no id)."""
    if session_id is None:
        resolved = _pick_session_id(limit)
        if resolved is None:
            raise SystemExit(0)
    else:
        from .kernel import session_manager

        try:
            resolved = session_manager.resolve(_session_store(), session_id)
        except FileNotFoundError:
            click.echo(f"no session found matching '{session_id}'", err=True)
            raise SystemExit(1) from None
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from None
    raise SystemExit(asyncio.run(_launch_tui(demo=False, bundle=bundle, resume_id=resolved)))


# --------------------------------------------------------------------------
# tool group -- list + invoke a mounted bundle tool from the command line
# --------------------------------------------------------------------------


def _parse_tool_args(
    pairs: tuple[str, ...], json_args: str | None
) -> tuple[dict[str, object], str | None]:
    """Resolve CLI tool arguments to a dict (amplifier-app-cli key=value convention).

    Each ``key=value`` VALUE is JSON-decoded when it can be (so ``limit=5`` is an
    int and ``data='{"k": 1}'`` is an object) and kept as a plain string
    otherwise. ``--json`` passes the whole argument object at once and is
    mutually exclusive with positional pairs. Returns ``(args, error)`` -- a
    non-None error is a usage message, never a raised exception.
    """
    if json_args is not None:
        if pairs:
            return {}, "pass arguments as key=value pairs OR --json, not both"
        try:
            data = json.loads(json_args)
        except json.JSONDecodeError as error:
            return {}, f"--json is not valid JSON: {error}"
        if not isinstance(data, dict):
            return {}, '--json must be a JSON object, e.g. \'{"file_path": "README.md"}\''
        return {str(key): value for key, value in data.items()}, None
    args: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            return {}, f"invalid argument '{pair}' -- expected key=value (or use --json)"
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args, None


def _emit_tool_error(error: Exception, output_format: str, *, tool_name: str | None = None) -> int:
    """Render a boot/teardown failure; return the CLI exit code (1)."""
    if output_format == "json":
        payload: dict[str, object] = {
            "status": "error",
            "error": str(error),
            "error_type": type(error).__name__,
        }
        if tool_name is not None:
            payload["tool"] = tool_name
        click.echo(json.dumps(payload, ensure_ascii=False))
    else:
        click.echo(f"Error: {error}", err=True)
    return 1


def _format_tool_output(output: object) -> str:
    """A tool result as scriptable text: strings verbatim, else indented JSON."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, indent=2, ensure_ascii=False, default=str)


async def _tool_list(bundle: str | None, output_format: str) -> int:
    """Boot a real session, enumerate its mounted tools, tear it down."""
    from .kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=bundle)
    error: Exception | None = None
    tools: tuple[Any, ...] = ()
    # Boot/module diagnostics print to stdout; keep stdout for the listing.
    with redirect_stdout(sys.stderr):
        try:
            await runtime.start()
            tools = await runtime.describe_tools()
        except Exception as caught:  # noqa: BLE001 -- structured CLI error, never a traceback
            error = caught
        finally:
            try:
                await runtime.cleanup()
            except Exception as caught:  # noqa: BLE001 -- best-effort teardown keeps the first error
                if error is None:
                    error = caught
    if error is not None:
        return _emit_tool_error(error, output_format)
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "status": "success",
                    "bundle": runtime.bundle_name,
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "invokable": tool.invokable,
                        }
                        for tool in tools
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not tools:
        click.echo("no tools mounted")
        return 0
    for tool in tools:
        summary = f"  \u00b7  {tool.description}" if tool.description else ""
        marker = "" if tool.invokable else "  (not invokable)"
        click.echo(f"{tool.name}{summary}{marker}")
    click.echo("invoke with `amplifier-newtui tool invoke <name> key=value ...`", err=True)
    return 0


async def _tool_invoke(
    bundle: str | None,
    name: str,
    args: dict[str, object],
    allow_writes: bool,
    output_format: str,
) -> int:
    """Boot a real session, invoke *name* through the trust gate, tear it down."""
    from .kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=bundle)
    error: Exception | None = None
    result: Any = None
    with redirect_stdout(sys.stderr):
        try:
            await runtime.start()
            result = await runtime.invoke_tool(name, args, allow_writes=allow_writes)
        except Exception as caught:  # noqa: BLE001 -- structured CLI error, never a traceback
            error = caught
        finally:
            try:
                await runtime.cleanup()
            except Exception as caught:  # noqa: BLE001 -- best-effort teardown keeps the first error
                if error is None:
                    error = caught
    if error is not None:
        return _emit_tool_error(error, output_format, tool_name=name)
    if result.ok:
        if output_format == "json":
            click.echo(
                json.dumps(
                    {"status": "success", "tool": name, "result": result.output},
                    ensure_ascii=False,
                    default=str,
                )
            )
        else:
            click.echo(_format_tool_output(result.output))
        return 0
    if output_format == "json":
        failure: dict[str, object] = {"status": "error", "tool": name, "error": result.error}
        if result.blocked:
            failure["blocked"] = True
            failure["capability"] = result.capability
        click.echo(json.dumps(failure, ensure_ascii=False))
    else:
        label = "Blocked" if result.blocked else "Error"
        detail = (
            f" (capability: {result.capability})" if result.blocked and result.capability else ""
        )
        click.echo(f"{label}: {result.error}{detail}", err=True)
    return 1


@main.group("tool")
def tool() -> None:
    """Invoke a mounted bundle tool from the command line (list, invoke)."""


@tool.command("list")
@click.option("--bundle", default=None, help="Bundle name or URI (default: settings/bundled).")
@click.option(
    "--output-format",
    type=click.Choice(("text", "json")),
    default="text",
    show_default=True,
    help="Listing format; json reserves stdout for one machine-readable document.",
)
def tool_list(bundle: str | None, output_format: str) -> None:
    """List the tools the active bundle mounts (name, one-line summary)."""
    raise SystemExit(asyncio.run(_tool_list(bundle, output_format)))


@tool.command("invoke")
@click.argument("name")
@click.argument("args", nargs=-1)
@click.option("--bundle", default=None, help="Bundle name or URI (default: settings/bundled).")
@click.option(
    "--json",
    "json_args",
    default=None,
    help='Pass ALL arguments as one JSON object (e.g. --json \'{"file_path": "x"}\').',
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Permit in-project write tools; exec/network/spend and out-of-project writes stay blocked.",
)
@click.option(
    "--output-format",
    type=click.Choice(("text", "json")),
    default="text",
    show_default=True,
    help="Result format; json reserves stdout for one machine-readable document.",
)
def tool_invoke(
    name: str,
    args: tuple[str, ...],
    bundle: str | None,
    json_args: str | None,
    yes: bool,
    output_format: str,
) -> None:
    """Invoke tool NAME with ARGS and print its result.

    ARGS are key=value pairs; each VALUE is parsed as JSON when it can be
    (numbers, booleans, arrays, objects) and kept as a plain string otherwise:

    \b
        amplifier-newtui tool invoke read_file file_path=README.md
        amplifier-newtui tool invoke some_tool data='{"k": "v"}' limit=5

    Or pass the whole argument object at once with --json:

    \b
        amplifier-newtui tool invoke read_file --json '{"file_path": "README.md"}'

    Governance: a one-shot CLI cannot answer an interactive approval, so it runs
    a SAFE posture -- read/test tools run; write/exec/network/spend are refused.
    --yes opts into in-project writes (still boundary-checked). For anything the
    CLI refuses, run it in the interactive TUI where the approval gate applies.
    """
    tool_args, parse_error = _parse_tool_args(args, json_args)
    if parse_error is not None:
        raise click.UsageError(parse_error)
    raise SystemExit(asyncio.run(_tool_invoke(bundle, name, tool_args, yes, output_format)))


# --------------------------------------------------------------------------
# session group — stored-session lifecycle (list / rename / delete / cleanup)
# --------------------------------------------------------------------------


@main.group(invoke_without_command=True)
@click.pass_context
def session(ctx: click.Context) -> None:
    """Manage stored sessions: list, rename, delete, cleanup."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@session.command("list")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of sessions to show.")
def session_list(limit: int) -> None:
    """List stored sessions (name · id · messages · age), newest first."""
    from rich.console import Console
    from rich.table import Table

    from .kernel import session_manager

    summaries = session_manager.list_summaries(_session_store(), limit=limit)
    if not summaries:
        click.echo("no stored sessions")
        return
    table = Table(title="Sessions", title_justify="center", header_style="bold cyan")
    table.add_column("Name", style="cyan", overflow="fold")
    table.add_column("Session", style="green", no_wrap=True)
    table.add_column("Bundle", style="magenta", no_wrap=True)
    table.add_column("Msgs", justify="right")
    table.add_column("Age", style="dim", no_wrap=True)
    for summary in summaries:
        table.add_row(
            summary.name or "—",
            summary.short_id,
            summary.bundle,
            str(summary.messages),
            summary.time_ago,
        )
    Console().print(table)


@session.command("rename")
@click.argument("session_id")
@click.argument("name", nargs=-1, required=True)
def session_rename(session_id: str, name: tuple[str, ...]) -> None:
    """Rename a stored session (metadata name, no file surgery)."""
    from .kernel import session_manager

    ok, detail = session_manager.rename(_session_store(), session_id, " ".join(name))
    if ok:
        click.echo(f"renamed → {detail}")
        return
    click.echo(detail, err=True)
    raise SystemExit(1)


@session.command("delete")
@click.argument("session_id")
@click.option("--force", "-f", is_flag=True, help="Skip the confirmation prompt.")
def session_delete(session_id: str, force: bool) -> None:
    """Delete a stored session and everything under it."""
    from .kernel import session_manager

    store = _session_store()
    try:
        resolved = session_manager.resolve(store, session_id)
    except FileNotFoundError:
        click.echo(f"no session found matching '{session_id}'", err=True)
        raise SystemExit(1) from None
    except ValueError as error:
        click.echo(str(error), err=True)
        raise SystemExit(1) from None
    if not force and not click.confirm(f"delete session {resolved}?", default=False):
        click.echo("cancelled")
        return
    ok, detail = session_manager.delete(store, resolved)
    if ok:
        click.echo(f"deleted {detail}")
        return
    click.echo(detail, err=True)
    raise SystemExit(1)


@session.command("cleanup")
@click.option(
    "--days", "-d", default=30, show_default=True, help="Delete sessions older than N days."
)
@click.option("--force", "-f", is_flag=True, help="Skip the confirmation prompt.")
def session_cleanup(days: int, force: bool) -> None:
    """Delete stored sessions older than N days."""
    from .kernel import session_manager

    if days < 0:
        click.echo("--days must be non-negative", err=True)
        raise SystemExit(1)
    if not force and not click.confirm(f"delete sessions older than {days} days?", default=False):
        click.echo("cancelled")
        return
    removed = session_manager.cleanup(_session_store(), days)
    click.echo(f"removed {removed} session(s) older than {days} days")


@session.command("fork")
@click.argument("session_id")
@click.option(
    "--directive",
    "-d",
    "directive",
    required=True,
    help="Starting instruction the forked child runs first on resume.",
)
@click.option("--name", "-n", "new_name", default="", help="Custom name for the forked session.")
def session_fork(session_id: str, directive: str, new_name: str) -> None:
    """Fork a stored session into a directive-primed child.

    Snapshots the parent's conversation into a NEW session (parent context +
    lineage) and seeds it with DIRECTIVE, so ``amplifier-newtui resume <child>``
    runs that instruction first. Re-expresses amplifier-app-cli's ``/fork
    <directive>`` self-delegation over newtui's persisted store: the child is
    primed and resumable rather than run in a detached background daemon (the
    full-screen TUI host lacks that seam — see kernel/session_manager.fork).
    """
    from .kernel import session_manager

    store = _session_store()
    try:
        resolved = session_manager.resolve(store, session_id)
    except FileNotFoundError:
        click.echo(f"no session found matching '{session_id}'", err=True)
        raise SystemExit(1) from None
    except ValueError as error:
        click.echo(str(error), err=True)
        raise SystemExit(1) from None
    transcript, metadata = store.load(resolved)
    ok, detail = session_manager.fork(
        store,
        resolved,
        transcript,
        directive,
        name=new_name,
        bundle=str(metadata.get("bundle") or ""),
    )
    if not ok:
        click.echo(detail, err=True)
        raise SystemExit(1)
    click.echo(f"forked {resolved[:8]} → {detail}")
    click.echo(f"resume to run the directive: amplifier-newtui resume {detail[:8]}")


@main.command()
def doctor() -> None:
    """Setup checkup: prints the report, exit 1 when findings exist."""
    from .commands.doctor import run_standalone

    raise SystemExit(run_standalone())


# --------------------------------------------------------------------------
# reset -- data-safe, category-scoped cleaner (see kernel/reset.py, issue #110)
# --------------------------------------------------------------------------


@main.command()
@click.option(
    "--category",
    "-c",
    "categories",
    multiple=True,
    metavar="NAME",
    help="Category to clear (repeatable or comma-separated). Default: cache,registry.",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be removed; change nothing.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt (scripted use).")
@click.option(
    "--home",
    "home_override",
    default=None,
    metavar="PATH",
    help="App home to reset (default: $AMPLIFIER_HOME or ~/.amplifier).",
)
@click.option("--list", "list_only", is_flag=True, help="List the category taxonomy and exit.")
def reset(
    categories: tuple[str, ...],
    dry_run: bool,
    yes: bool,
    home_override: str | None,
    list_only: bool,
) -> None:
    """Data-safe reset: clear selected categories, preserve the rest.

    Re-expresses amplifier-app-cli's ``reset`` recovery command as a guarded,
    category-scoped cleaner scoped to newtui's app home. ``--category`` names
    what to CLEAR; everything else is preserved. The default clears only the
    auto-regenerating categories (cache, registry).

    \b
    Guards:
      - --dry-run previews and removes NOTHING
      - a confirmation prompt (bypass with --yes) before any removal
      - secrets (keys) are cleared ONLY when named explicitly
      - never deletes outside the confirmed app home

    \b
    Examples:
      amplifier-newtui reset --list                 Show the taxonomy
      amplifier-newtui reset --dry-run              Preview the safe default
      amplifier-newtui reset --category cache -y    Clear only the cache
      amplifier-newtui reset -c sessions,config     Clear sessions + config
    """
    from .kernel import reset as reset_kernel

    if list_only:
        for name in reset_kernel.CATEGORY_ORDER:
            category = reset_kernel.CATEGORIES[name]
            tags = []
            if name in reset_kernel.DEFAULT_CATEGORIES:
                tags.append("default")
            if category.auto_regenerates:
                tags.append("auto-regenerates")
            if category.secret:
                tags.append("secret")
            suffix = f"  [{', '.join(tags)}]" if tags else ""
            click.echo(f"{name:9} {category.description}{suffix}")
        return

    home = reset_kernel.resolve_app_home(Path(home_override) if home_override else None)

    try:
        selected = reset_kernel.parse_categories(categories)
    except reset_kernel.ResetError as error:
        click.echo(str(error), err=True)
        raise SystemExit(2) from None

    # Plan first (dry run under the hood) -- also runs the home safety guards.
    try:
        plan = reset_kernel.run_reset(home, selected, dry_run=True)
    except reset_kernel.ResetError as error:
        click.echo(f"refusing to reset: {error}", err=True)
        raise SystemExit(2) from None

    click.echo(f"app home: {plan.home}")
    click.echo(f"clear:    {', '.join(plan.clear)}")
    click.echo(f"preserve: {', '.join(plan.keep) or '(nothing else on disk)'}")
    if plan.secret_cleared:
        click.echo(f"WARNING: this clears secrets: {', '.join(plan.secret_cleared)}")

    if not plan.removed:
        click.echo("nothing to remove -- selected categories have no files on disk")
        return

    click.echo("would remove:" if dry_run else "to remove:")
    for path in plan.removed:
        click.echo(f"  - {path}")

    if dry_run:
        click.echo("DRY RUN -- nothing was removed")
        return

    if not yes:
        prompt = f"permanently remove {len(plan.removed)} item(s)?"
        if plan.destructive_cleared:
            prompt = (
                f"permanently remove {len(plan.removed)} item(s) "
                f"including {', '.join(plan.destructive_cleared)}?"
            )
        if not click.confirm(prompt, default=False):
            click.echo("cancelled")
            return

    final = reset_kernel.run_reset(home, selected, dry_run=False)
    click.echo(f"removed {len(final.removed)} item(s); preserved {len(final.preserved)}")
    for path in final.preserved:
        click.echo(f"  preserved: {path}")


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
    fn = click.option(
        "--local", "is_local", is_flag=True, help="Write to .amplifier/settings.local.yaml."
    )(fn)
    fn = click.option(
        "--project", "is_project", is_flag=True, help="Write to .amplifier/settings.yaml."
    )(fn)
    fn = click.option(
        "--global", "is_global", is_flag=True, help="Write to ~/.amplifier/settings.yaml (default)."
    )(fn)
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
        f"Active: [green]{active}[/green]"
        if active
        else f"No bundle active ({DEFAULT_BUNDLE} default)",
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
# allowed-dirs / denied-dirs — tool-filesystem capability administration
# --------------------------------------------------------------------------


def _list_directories(kind: Literal["allowed", "denied"], scope_filter: str | None) -> None:
    from .kernel import bundle_admin, directory_permissions

    scope = cast(bundle_admin.Scope | None, scope_filter)
    entries = directory_permissions.configured_entries(
        bundle_admin.settings_paths(None, None), kind, scope_filter=scope
    )
    title = "Allowed write directories" if kind == "allowed" else "Denied write directories"
    click.echo(f"{title}:")
    if not entries:
        click.echo("  none configured")
    for entry in entries:
        click.echo(f"  {entry.path}  ({entry.scope})")
    if kind == "allowed":
        click.echo(f"  {Path.cwd().resolve()}  (project-default)")


def _update_directory(
    kind: Literal["allowed", "denied"],
    operation: Literal["add", "remove"],
    path: str,
    *,
    is_global: bool,
    is_project: bool,
    is_local: bool,
) -> None:
    from .kernel import bundle_admin, directory_permissions

    scope = _scope(is_global, is_project, is_local)
    changed, resolved, settings_path = directory_permissions.update_configured_path(
        bundle_admin.settings_paths(None, None), kind, operation, path, scope
    )
    if operation == "remove" and not changed:
        click.echo(f"path not found at {scope} scope: {resolved}", err=True)
        raise SystemExit(1)
    if operation == "add" and not Path(resolved).exists():
        click.echo(f"warning: path does not exist yet: {resolved}", err=True)
    verb = "allowed" if kind == "allowed" else "denied"
    state = "unchanged" if not changed else verb
    click.echo(f"{state} · {resolved}  ({scope}: {settings_path})")


def _directory_scope_filter(fn):  # noqa: ANN001 — click decorator stack
    fn = click.option("--global", "scope_filter", flag_value="global")(fn)
    fn = click.option("--project", "scope_filter", flag_value="project")(fn)
    fn = click.option("--local", "scope_filter", flag_value="local")(fn)
    return fn


@main.group("allowed-dirs")
def allowed_dirs() -> None:
    """Manage directories the AI can write to."""


@allowed_dirs.command("list")
@_directory_scope_filter
def allowed_dirs_list(scope_filter: str | None) -> None:
    """List configured allowed write directories and their scopes."""
    _list_directories("allowed", scope_filter)


@allowed_dirs.command("add")
@click.argument("path")
@_scope_options
def allowed_dirs_add(path: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Allow PATH at the selected settings scope."""
    _update_directory(
        "allowed",
        "add",
        path,
        is_global=is_global,
        is_project=is_project,
        is_local=is_local,
    )


@allowed_dirs.command("remove")
@click.argument("path")
@_scope_options
def allowed_dirs_remove(path: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Remove PATH from the selected settings scope."""
    _update_directory(
        "allowed",
        "remove",
        path,
        is_global=is_global,
        is_project=is_project,
        is_local=is_local,
    )


@main.group("denied-dirs")
def denied_dirs() -> None:
    """Manage directories the AI is blocked from writing to."""


@denied_dirs.command("list")
@_directory_scope_filter
def denied_dirs_list(scope_filter: str | None) -> None:
    """List configured denied write directories and their scopes."""
    _list_directories("denied", scope_filter)


@denied_dirs.command("add")
@click.argument("path")
@_scope_options
def denied_dirs_add(path: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Deny PATH at the selected settings scope."""
    _update_directory(
        "denied",
        "add",
        path,
        is_global=is_global,
        is_project=is_project,
        is_local=is_local,
    )


@denied_dirs.command("remove")
@click.argument("path")
@_scope_options
def denied_dirs_remove(path: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Remove PATH from the selected settings scope."""
    _update_directory(
        "denied",
        "remove",
        path,
        is_global=is_global,
        is_project=is_project,
        is_local=is_local,
    )


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
    click.echo("stored keys: " + (", ".join(status.stored_keys) if status.stored_keys else "none"))

    choices = await setup.onboarding_choices()
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
        raw = click.prompt(
            "\nset up which provider? (number, or blank to skip)", default="", show_default=False
        )
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
    cfg_path = setup.write_provider_config(bundle_admin.settings_paths(None, None), "global", entry)
    click.echo(f"\nwrote {', '.join(written)} → {path}")
    click.echo(f"configured provider {target.module_id} → {cfg_path}")
    click.echo("run `amplifier-newtui` to start a session.")
    return 0


@main.command()
@click.option("--provider", "-p", default=None, help="Provider to set up (e.g. anthropic).")
@click.option("--api-key", default=None, help="API key (non-interactive; else prompted).")
@click.option("--base-url", default=None, help="Optional provider base-URL override.")
@click.option("--model", default=None, help="Default model for the provider.")
@click.option(
    "--from-env", is_flag=True, help="Non-interactive: configure a provider detected from env vars."
)
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
# provider group — configure providers and switch the primary
# --------------------------------------------------------------------------


@main.group()
def provider() -> None:
    """Manage AI providers: list, add, use, remove, dashboard."""


@provider.command("list")
def provider_list() -> None:
    """List configured providers (★ marks the primary)."""
    from .kernel import setup

    providers = setup.configured_providers()
    if not providers:
        click.echo("no providers configured · run `amplifier-newtui provider add`")
        return
    for entry in providers:
        marker = "★" if entry.primary else " "
        model = f"  ({entry.model})" if entry.model else ""
        click.echo(
            f"{marker} {entry.name}  ·  {entry.module_id}  ·  "
            f"pri {entry.priority}  ·  {entry.scope}{model}"
        )


@provider.command("add")
@click.argument("provider_type", required=False)
@click.option("--api-key", default=None, help="API key (non-interactive; else prompted).")
@click.option("--base-url", default=None, help="Optional provider base-URL override.")
@click.option("--model", default=None, help="Default model for the provider.")
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: never prompt (needs --api-key).")
def provider_add(
    provider_type: str | None,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    yes: bool,
) -> None:
    """Add and configure a provider (interactive picker when TYPE is omitted).

    Adding a second provider keeps the first: the newest becomes primary and
    the others stay switchable via `amplifier-newtui provider use`.
    """
    raise SystemExit(asyncio.run(_init(provider_type, api_key, base_url, model, yes, False)))


@provider.command("use")
@click.argument("name")
def provider_use(name: str) -> None:
    """Make NAME the primary provider (sets it to priority 1)."""
    from .kernel import bundle_admin, setup

    target = setup.use_provider(bundle_admin.settings_paths(None, None), name)
    if target is None:
        click.echo(f"unknown provider: {name} · run `amplifier-newtui provider list`", err=True)
        raise SystemExit(1)
    click.echo(f"primary provider → {target.name}")


@provider.command("remove")
@click.argument("name")
def provider_remove(name: str) -> None:
    """Remove NAME from the provider configuration (every scope)."""
    from .kernel import bundle_admin, setup

    removed = setup.remove_provider(bundle_admin.settings_paths(None, None), name)
    if removed is None:
        click.echo(f"unknown provider: {name} · run `amplifier-newtui provider list`", err=True)
        raise SystemExit(1)
    click.echo(f"removed provider: {removed.name}")


@provider.command("dashboard")
def provider_dashboard() -> None:
    """Show configured providers, the primary, and how to switch."""
    from .kernel import setup

    status = setup.setup_status()
    providers = setup.configured_providers()
    click.echo(f"active bundle: {status.active_bundle or 'newtui (default)'}")
    click.echo("stored keys: " + (", ".join(status.stored_keys) if status.stored_keys else "none"))
    click.echo("")
    if not providers:
        click.echo("no providers configured · run `amplifier-newtui provider add`")
        return
    click.echo("providers (★ = primary):")
    for entry in providers:
        marker = "★" if entry.primary else " "
        model = f" ({entry.model})" if entry.model else ""
        click.echo(
            f"  {marker} {entry.name} · {entry.module_id} · "
            f"pri {entry.priority} · {entry.scope}{model}"
        )
    click.echo("")
    click.echo("switch with `amplifier-newtui provider use <name>`")


# --------------------------------------------------------------------------
# notify — configure the attention-notification ladder + ntfy push (issue #106)
# --------------------------------------------------------------------------


def _notify_show() -> None:
    from .kernel import notify_admin

    status = notify_admin.load_status()
    click.echo("Notifications (effective — env wins over settings):")
    click.echo(f"  ladder ceiling : {status.ceiling}  (from {status.ceiling_source})")
    click.echo(f"  desktop rung   : {status.desktop_gate}  (from {status.desktop_gate_source})")
    click.echo(f"  suppress all   : {status.suppress}")
    click.echo("  push (ntfy):")
    enabled = "(module default)" if status.push_enabled is None else str(status.push_enabled)
    click.echo(f"    enabled  : {enabled}")
    click.echo(f"    topic    : {'configured' if status.topic else 'not set'}")
    click.echo(f"    server   : {status.push_server or '(default) https://ntfy.sh'}")
    if status.push_priority:
        click.echo(f"    priority : {status.push_priority}")
    if status.push_tags:
        click.echo(f"    tags     : {', '.join(status.push_tags)}")


def _notify_test() -> int:
    from .kernel import bundle_admin, notify_admin
    from .kernel.config import load_merged_settings
    from .ui import notifications

    paths = bundle_admin.settings_paths(None, None)
    settings = load_merged_settings(paths)
    env = notify_admin.resolved_environ(settings)
    # A deferred decision always qualifies and, when unfocused, opens the
    # desktop rung -- so a test exercises the whole ladder the app would fire.
    rungs = notifications.notification_rungs("decision_deferred", focused=False, environ=env)
    fired: list[str] = []
    if "bell" in rungs:
        click.echo("\a", nl=False)
        fired.append("bell")
    if "desktop" in rungs:
        click.echo(
            notifications.osc777_notification_sequence(
                "Amplifier", "Test notification — the assistant needs you."
            ),
            nl=False,
        )
        fired.append("desktop (OSC 777)")
    if fired:
        click.echo(f"fired: {', '.join(fired)}")
    else:
        click.echo("nothing fired — notifications are silenced (ceiling off / suppress)")
    if "desktop" not in rungs and notifications.notify_ceiling(env) == "desktop":
        if not notifications.desktop_notifications_supported(env):
            click.echo(
                "desktop skipped — terminal not on the OSC render allowlist; enable with "
                "`amplifier-newtui notify enable desktop` or AMPLIFIER_TERMINAL_NOTIFICATIONS=force",
                err=True,
            )
    return 0


@main.group(invoke_without_command=True)
@click.pass_context
def notify(ctx: click.Context) -> None:
    """Configure attention notifications: show, set, enable, disable, test."""
    if ctx.invoked_subcommand is None:
        _notify_show()


@notify.command("show")
def notify_show_cmd() -> None:
    """Show the effective notification config (settings + env resolved)."""
    _notify_show()


@notify.command("set")
@click.argument("key")
@click.argument("value")
@_scope_options
def notify_set(key: str, value: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Set a notification KEY to VALUE.

    Keys: suppress, desktop.enabled, push.enabled, push.server, push.priority,
    push.tags, topic. The ntfy topic is a secret — it is saved to
    ~/.amplifier/keys.env, never a settings file.
    """
    from .kernel import bundle_admin, notify_admin

    scope = _scope(is_global, is_project, is_local)
    try:
        result = notify_admin.set_key(bundle_admin.settings_paths(None, None), key, value, scope)
    except notify_admin.UnknownNotifyKeyError:
        keys = ", ".join(notify_admin.known_key_names())
        click.echo(f"unknown key: {key} · known keys: {keys}", err=True)
        raise SystemExit(1) from None
    except notify_admin.InvalidNotifyValueError as exc:
        click.echo(f"invalid value for {key}: {exc}", err=True)
        raise SystemExit(1) from None
    if result.is_secret:
        click.echo(f"{key} → configured  (secret saved to {result.path})")
    else:
        click.echo(f"{key} → {result.value}  ({scope}: {result.path})")


def _set_channel_enabled(
    target: str, enabled: bool, is_global: bool, is_project: bool, is_local: bool
) -> None:
    from .kernel import bundle_admin, notify_admin

    paths = bundle_admin.settings_paths(None, None)
    scope = _scope(is_global, is_project, is_local)
    result = notify_admin.set_enabled(paths, target, enabled, scope)  # type: ignore[arg-type]
    state = "enabled" if enabled else "disabled"
    click.echo(f"{target} notifications {state}  ({scope}: {result.path})")
    if (
        target == "push"
        and enabled
        and not notify_admin.topic_configured(paths.global_settings.parent)
    ):
        click.echo(
            "  note: no ntfy topic set — run `amplifier-newtui notify set topic <topic>`",
            err=True,
        )


@notify.command("enable")
@click.argument("target", type=click.Choice(["desktop", "push"]), required=False, default="desktop")
@_scope_options
def notify_enable(target: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Enable desktop or push notifications (default: desktop)."""
    _set_channel_enabled(target, True, is_global, is_project, is_local)


@notify.command("disable")
@click.argument("target", type=click.Choice(["desktop", "push"]), required=False, default="desktop")
@_scope_options
def notify_disable(target: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Disable desktop or push notifications (default: desktop)."""
    _set_channel_enabled(target, False, is_global, is_project, is_local)


@notify.command("test")
def notify_test_cmd() -> None:
    """Fire a test notification through the real attention ladder."""
    raise SystemExit(_notify_test())


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


# --------------------------------------------------------------------------
# source group — module/bundle source overrides (add/remove/list/show)
# --------------------------------------------------------------------------


def _source_type_options(fn):  # noqa: ANN001 — click decorator stack
    fn = click.option(
        "--bundle",
        "force_bundle",
        is_flag=True,
        help="Force treating IDENTIFIER as a bundle (skip auto-detect).",
    )(fn)
    fn = click.option(
        "--module",
        "force_module",
        is_flag=True,
        help="Force treating IDENTIFIER as a module (skip auto-detect).",
    )(fn)
    return fn


@main.group("source")
def source() -> None:
    """Manage source overrides for modules and bundles (add/remove/list/show)."""


@source.command("add")
@click.argument("identifier")
@click.argument("source_uri")
@_source_type_options
@_scope_options
def source_add(
    identifier: str,
    source_uri: str,
    force_module: bool,
    force_bundle: bool,
    is_global: bool,
    is_project: bool,
    is_local: bool,
) -> None:
    """Add a source override for a module or bundle.

    IDENTIFIER is the module id or bundle name; SOURCE_URI is a local path or
    git URL. The type is auto-detected (--module/--bundle to force).
    """
    from .kernel import bundle_admin, source_admin

    if force_module and force_bundle:
        click.echo("cannot specify both --module and --bundle", err=True)
        raise SystemExit(1)
    if force_module:
        kind: Literal["module", "bundle"] = "module"
    elif force_bundle:
        kind = "bundle"
    else:
        kind = source_admin.detect_source_type(identifier, source_uri)
    scope = _scope(is_global, is_project, is_local)
    path = source_admin.add_source(
        bundle_admin.settings_paths(None, None), kind, identifier, source_uri, scope
    )
    click.echo(f"{kind} source {identifier} \u2192 {source_uri}  ({scope}: {path})")


@source.command("remove")
@click.argument("identifier")
@_source_type_options
@_scope_options
def source_remove(
    identifier: str,
    force_module: bool,
    force_bundle: bool,
    is_global: bool,
    is_project: bool,
    is_local: bool,
) -> None:
    """Remove a module/bundle source override (auto-detects both by default)."""
    from .kernel import bundle_admin, source_admin

    if force_module and force_bundle:
        click.echo("cannot specify both --module and --bundle", err=True)
        raise SystemExit(1)
    scope = _scope(is_global, is_project, is_local)
    paths = bundle_admin.settings_paths(None, None)
    removed_module, removed_bundle = source_admin.remove_source(
        paths, identifier, scope, module=not force_bundle, bundle=not force_module
    )
    provider_cleaned = False
    if removed_module or not force_bundle:
        provider_cleaned = source_admin.cleanup_provider_config_source(paths, identifier, scope)
    if removed_module:
        click.echo(f"removed module source {identifier} ({scope})")
    if removed_bundle:
        click.echo(f"removed bundle source {identifier} ({scope})")
    if provider_cleaned:
        click.echo(f"reset provider config source for {identifier} \u2192 default ({scope})")
    if not (removed_module or removed_bundle or provider_cleaned):
        click.echo(f"no source override for {identifier} ({scope})")


@source.command("list")
def source_list() -> None:
    """List configured source overrides (modules then bundles)."""
    from rich.console import Console
    from rich.table import Table

    from .kernel import bundle_admin, source_admin

    paths = bundle_admin.settings_paths(None, None)
    entries = source_admin.list_sources(
        project_dir=paths.project_settings.parent.parent,
        amplifier_home=paths.global_settings.parent,
    )
    console = Console()
    if not entries:
        console.print("no source overrides configured")
        console.print("Add one with: amplifier-newtui source add <identifier> <uri>", style="dim")
        return
    # One table (consistent with `bundle list`); a Type column carries the
    # module/bundle distinction so narrow per-kind tables never wrap titles.
    table = Table(title="Source Overrides", title_justify="center", header_style="bold cyan")
    table.add_column("Name", style="green", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Source", style="magenta", overflow="fold")
    for entry in entries:
        table.add_row(entry.name, entry.kind, entry.source_uri)
    console.print(table)


@source.command("show")
@click.argument("module_id")
def source_show(module_id: str) -> None:
    """Show the source-resolution path newtui would use for MODULE_ID."""
    from .kernel import bundle_admin, source_admin

    paths = bundle_admin.settings_paths(None, None)
    report = source_admin.resolve_module(
        module_id,
        project_dir=paths.project_settings.parent.parent,
        amplifier_home=paths.global_settings.parent,
    )
    click.echo(f"module: {report.module_id}")
    click.echo("resolution (highest \u2192 lowest precedence):")
    env = report.env_value if report.env_value else "not set"
    click.echo(f"  1. env {report.env_var}: {env}")
    workspace = "found" if report.workspace_found else "not found"
    click.echo(f"  2. workspace {report.workspace_path}: {workspace}")
    settings_source = report.settings_source if report.settings_source else "not set"
    click.echo(f"  3. settings sources.modules: {settings_source}")
    if report.effective_source:
        click.echo(f"effective override \u2192 {report.effective_source}")
    else:
        click.echo("effective override \u2192 none (foundation resolves the default source)")


# --------------------------------------------------------------------------
# routing group — inspect/choose the model routing matrix (list/use)
# --------------------------------------------------------------------------


@main.group("routing")
def routing() -> None:
    """Manage model routing matrices: list, use."""


@routing.command("list")
def routing_list() -> None:
    """List available routing matrices (\u25cf marks the active one)."""
    from rich.console import Console
    from rich.table import Table

    from .kernel import bundle_admin, routing_admin

    paths = bundle_admin.settings_paths(None, None)
    entries = routing_admin.list_matrices(
        project_dir=paths.project_settings.parent.parent,
        amplifier_home=paths.global_settings.parent,
        fetch=True,
    )
    console = Console()
    if not entries:
        console.print("no routing matrices found")
        console.print(
            "Run `amplifier-newtui update` to fetch the routing-matrix bundle.", style="dim"
        )
        return
    table = Table(title="Routing Matrices", title_justify="center", header_style="bold cyan")
    table.add_column("", width=1, no_wrap=True)  # active marker
    table.add_column("Name", style="green", no_wrap=True)
    table.add_column("Description", style="dim", overflow="fold")
    table.add_column("Compatibility", no_wrap=True)
    table.add_column("Updated", no_wrap=True, style="dim")
    for entry in entries:
        marker = "\u25cf" if entry.active else ""
        name = f"[bold]{entry.name}[/bold]" if entry.active else entry.name
        compat = f"{entry.covered}/{entry.total} roles" if entry.has_providers else "no providers"
        table.add_row(marker, name, entry.description, compat, entry.updated)
    console.print(table)
    active = next((e.name for e in entries if e.active), None)
    console.print(
        f"Active: [green]{active}[/green]"
        if active
        else f"No matrix active ({routing_admin.DEFAULT_MATRIX} default)",
        style="dim",
    )


@routing.command("use")
@click.argument("matrix_name")
@_scope_options
def routing_use(matrix_name: str, is_global: bool, is_project: bool, is_local: bool) -> None:
    """Select MATRIX_NAME as the active routing matrix."""
    from rich.console import Console
    from rich.table import Table

    from .kernel import bundle_admin, routing_admin
    from .kernel.config import load_merged_settings

    paths = bundle_admin.settings_paths(None, None)
    home = paths.global_settings.parent
    matrices = routing_admin.load_all_matrices(
        routing_admin.discover_matrix_files(home, fetch=True)
    )
    if matrix_name not in matrices:
        available = ", ".join(sorted(matrices)) or "none"
        click.echo(f"unknown matrix: {matrix_name} \u00b7 available: {available}", err=True)
        raise SystemExit(1)
    scope = _scope(is_global, is_project, is_local)
    path = routing_admin.set_active_matrix(paths, matrix_name, scope)
    click.echo(f"active routing matrix \u2192 {matrix_name}  ({scope}: {path})")

    settings = load_merged_settings(paths)
    provider_types = routing_admin.configured_provider_types(settings)
    rows = routing_admin.resolve_matrix(matrices[matrix_name], provider_types)
    if not rows:
        return
    console = Console()
    table = Table(title=f"Routing: {matrix_name}", title_justify="center", header_style="bold cyan")
    table.add_column("Role", style="cyan", no_wrap=True)
    table.add_column("Model", style="green")
    table.add_column("Provider")
    for row in rows:
        if row.model and row.provider:
            table.add_row(row.role, row.model, row.provider)
        else:
            table.add_row(row.role, "\u26a0 (no provider)", "-")
    console.print(table)


if __name__ == "__main__":
    main()
