"""Thin async click entry point (``amplifier-tui``).

Default invocation launches the full-screen TUI on a real amplifier
session (RealRuntime); ``--demo`` swaps in the scripted DemoRuntime
(fully offline — no bundle, no network, no credentials). Subcommands:

- ``run [PROMPT]`` — one-shot session from an argument or piped stdin;
  emits text, one-document JSON, or live versioned JSONL events.
- ``sessions``     — named table of stored sessions (``--plain`` for ids).
- ``resume ID``    — launch the TUI resuming a stored session.
- ``continue``     — resume the most recent stored session (no picker).
- ``init``         — interactive provider + routing setup (flags bypass it).
- ``version``      — app + amplifier-core/-foundation versions.
- ``doctor``       — plain-text setup checkup (exit 0 ok / 1 findings).

Contract: ``main()`` is the console-script entry; every async body runs
under a single ``asyncio.run`` — no sync/async bridging deeper down.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from time import monotonic
from typing import IO, Any, Literal, cast

import click

from . import __version__


async def _launch_tui(
    *,
    demo: bool,
    bundle: str | None = None,
    resume_id: str | None = None,
    mode: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    from .ui.app import TuiApp
    from .ui.term_probe import patch_legacy_alt_named_keys, probe_kitty_protocol

    patch_legacy_alt_named_keys()

    if demo:
        from .ui.demo_wiring import DemoRuntimeAdapter

        adapter = DemoRuntimeAdapter()
    else:
        from .ui.runtime_adapter import RealRuntimeAdapter

        # Per-invocation overrides ride the same ephemeral seam as ``run``:
        # --provider/--model mutate only the resolved in-memory plan; --mode
        # seeds the interaction posture. None of them touch a settings scope.
        adapter = RealRuntimeAdapter(
            bundle=bundle,
            resume_id=resume_id,
            provider_override=provider,
            model_override=model,
        )
    app = TuiApp(adapter, kitty_protocol=probe_kitty_protocol(), initial_mode=mode)
    await app.run_async()
    _print_resume_hint(getattr(adapter, "session_id", ""))
    return app.return_code or 0


def _print_resume_hint(session_id: str) -> None:
    """On TUI exit, tell the user how to get back into this session.

    Mirrors amplifier-app-cli's farewell banner with the CORRECT tui
    commands (S4 / #148): real sessions carry a stored id; demo sessions
    do not, so the hint is skipped when there is nothing to resume.
    """
    if not session_id:
        return
    click.echo(f"resume this session: amplifier-tui resume {session_id}")
    click.echo("list sessions:       amplifier-tui sessions")


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


def _is_interactive_terminal() -> bool:
    """True when both stdin and stdout are TTYs (a real interactive shell).

    The single predicate for "can we take over the screen?" — used to decide
    whether a bare ``run`` (no prompt, nothing piped) should launch the
    full-screen TUI instead of erroring.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _validate_overrides(model: str | None, provider: str | None, mode: str | None) -> None:
    """Guard the per-invocation ``--model``/``--provider``/``--mode`` overrides.

    Shared by the headless ``run`` command and the interactive launcher so both
    entry points enforce the same rules: ``--model`` without ``--provider`` is
    ambiguous (which provider hosts it?) and refused early, and ``--mode`` must
    name a real interaction mode rather than silently falling back to default.
    Exits nonzero with a message on any violation (never returns an error).
    """
    from .model.modes import MODE_PROFILES

    if model is not None and provider is None:
        click.echo(
            "Error: --model requires --provider (name the provider that hosts the model)",
            err=True,
        )
        raise SystemExit(1)
    if mode is not None and mode not in MODE_PROFILES:
        valid = ", ".join(MODE_PROFILES)
        click.echo(f"Error: unknown mode '{mode}' · valid modes: {valid}", err=True)
        raise SystemExit(1)


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
            "No AI provider configured. Run `amplifier-tui init` or export a "
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
    click.echo("\nNo provider configured yet. Run `amplifier-tui` again when ready.")
    return 0


def _interactive_launch(
    *,
    demo: bool,
    bundle: str | None,
    resume_id: str | None = None,
    mode: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Run the first-run provider gate (real sessions), then boot the TUI.

    The single path every interactive entry point funnels through so the gate
    and the per-invocation overrides stay consistent. Returns the process exit
    code; ``--demo`` skips the gate (fully offline).
    """
    if not demo:
        gate = asyncio.run(_first_run_gate())
        if gate is not None:
            return gate
    return asyncio.run(
        _launch_tui(
            demo=demo,
            bundle=bundle,
            resume_id=resume_id,
            mode=mode,
            provider=provider,
            model=model,
        )
    )


@click.group(invoke_without_command=True)
@click.option(
    "--demo", is_flag=True, help="Run the scripted DemoRuntime instead of a real session."
)
@click.option("--bundle", default=None, help="Bundle name or URI (default: settings/bundled).")
@click.option(
    "--provider",
    "-p",
    default=None,
    help="Provider override for THIS launch only (not persisted to settings).",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="Model override for THIS launch only (requires --provider; not persisted).",
)
@click.option(
    "--mode",
    "mode",
    default=None,
    help="Interaction mode to start in (chat, plan, brainstorm, build, auto).",
)
@click.version_option(__version__, prog_name="amplifier-tui")
@click.pass_context
def main(
    ctx: click.Context,
    demo: bool,
    bundle: str | None,
    provider: str | None,
    model: str | None,
    mode: str | None,
) -> None:
    """Amplifier full-screen TUI (v3 Cohesive).

    ``--provider``/``--model`` override the resolved plan for THIS launch only
    (never written to a settings scope); ``--mode`` seeds the interaction
    posture the TUI opens in. Same ephemeral semantics as the ``run`` command.
    """
    if ctx.invoked_subcommand is not None:
        return
    _validate_overrides(model, provider, mode)
    raise SystemExit(
        _interactive_launch(demo=demo, bundle=bundle, mode=mode, provider=provider, model=model)
    )


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

    With NO prompt on an interactive terminal (nothing piped, default text
    output), ``run`` launches the full-screen TUI with these same overrides
    instead of erroring — so ``run -p ... -m ... --mode chat`` opens a chat
    session. Piped/non-interactive/JSON invocations stay prompt-required.
    """
    # Shared with the interactive launcher: --model requires --provider, and
    # --mode must name a real interaction mode (both fail loud, nonzero exit).
    _validate_overrides(model, provider, mode)
    # --resume resolves a (possibly partial) id to one stored session up front,
    # so an unknown/ambiguous id errors clearly before any boot work begins.
    resume_id: str | None = None
    if resume is not None:
        from .kernel import session_manager

        try:
            resume_id = session_manager.resolve(_session_store(), resume)
        except FileNotFoundError:
            click.echo(f"no session found matching '{resume}'", err=True)
            _echo_cross_project_hint(resume)
            raise SystemExit(1) from None
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from None
    # A bare `run` on a TTY (no prompt, nothing piped, plain text output) means
    # "start a session" — boot the interactive TUI with the same overrides
    # rather than refusing. Headless use (piped stdin, non-TTY, or a JSON
    # output format) stays prompt-required so scripts fail loud as before.
    if prompt is None and output_format == "text" and _is_interactive_terminal():
        raise SystemExit(
            _interactive_launch(
                demo=False,
                bundle=bundle,
                resume_id=resume_id,
                mode=mode,
                provider=provider,
                model=model,
            )
        )
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


def _print_session_table(summaries: list[Any]) -> None:
    """Render session *summaries* as the shared rich table (newest-first).

    The single renderer behind both ``sessions`` and ``session list`` so the
    two can't drift (S3): Name · Session · Bundle · Msgs · Turns · Age. The
    Turns column reflects the ``turn_count`` the incremental saver records in
    ``metadata.json``; sessions whose stored metadata predates that field show
    ``—`` rather than a fabricated ``0``.
    """
    from rich.console import Console
    from rich.table import Table

    table = Table(title="Sessions", title_justify="center", header_style="bold cyan")
    table.add_column("Name", style="cyan", overflow="fold")
    table.add_column("Session", style="green", no_wrap=True)
    table.add_column("Bundle", style="magenta", no_wrap=True)
    table.add_column("Msgs", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Age", style="dim", no_wrap=True)
    for summary in summaries:
        table.add_row(
            summary.name or "—",
            summary.short_id,
            summary.bundle,
            str(summary.messages),
            "—" if summary.turns is None else str(summary.turns),
            summary.time_ago,
        )
    Console().print(table)


@main.command()
@click.option("--bundle", default=None, help="Bundle name or URI.")
@click.option("--model", "-m", default=None, help="Model override (requires --provider).")
@click.option("--provider", "-p", default=None, help="Provider override for THIS invocation.")
@click.option("--mode", "mode", default=None, help="Interaction mode to start in.")
@click.option(
    "--resume", "resume", default=None, metavar="SESSION_ID", help="Resume a stored session."
)
@click.option(
    "--attach",
    "attach",
    default=None,
    metavar="REF",
    help="Attach ref (amplifier-session:<id>[#<handoff>]); claims the handoff on boot.",
)
@click.option(
    "--actor", "actor", default=None, metavar="ID", help="Default actor id for control ops."
)
@click.option(
    "--actor-kind",
    "actor_kind",
    type=click.Choice(["human", "automation"]),
    default="automation",
    show_default=True,
    help="Default actor kind (drives lease takeover precedence).",
)
def serve(
    bundle: str | None,
    model: str | None,
    provider: str | None,
    mode: str | None,
    resume: str | None,
    attach: str | None,
    actor: str | None,
    actor_kind: str,
) -> None:
    """Run an interactive session as a bidirectional line protocol on stdio.

    The out-of-process front-end contract: normalized events (plus
    ``approval.required``) stream to stdout as JSON lines; ``submit`` /
    ``approve`` / ``interrupt`` submissions arrive on stdin. This is the seam a
    Rust (or any external) UI drives; it wraps the same ``RealRuntime`` the TUI
    uses, so amplifier-core is untouched. See ``kernel/serve.py`` for the wire.

    ``--attach`` is the human-takeover path: hand a person the ref a paused
    controller minted (``handoff.created``) and this boots on the SAME session,
    claims the handoff, and hands them the write lease. ``--actor`` /
    ``--actor-kind`` stamp the identity that ops without their own ``actor``
    are attributed to (a ``human`` actor outranks an ``automation`` one when
    taking the lease over).
    """
    _validate_overrides(model, provider, mode)
    resume_id: str | None = None
    if attach is not None:
        from .kernel.session_control import parse_attach_ref

        attached_session, _ = parse_attach_ref(attach)
        if attached_session:
            resume = attached_session
    if resume is not None:
        from .kernel import session_manager

        resume_id = session_manager.resolve(_session_store(), resume)
    from .kernel.serve import serve as _serve

    raise SystemExit(
        asyncio.run(
            _serve(
                bundle,
                mode=mode,
                model=model,
                provider=provider,
                resume_id=resume_id,
                attach=attach,
                actor=actor,
                actor_kind=actor_kind,
            )
        )
    )


@main.command()
@click.option("--limit", "-n", default=20, show_default=True, help="Number of sessions to show.")
@click.option(
    "--plain",
    is_flag=True,
    help="Print bare session ids, one per line (machine-readable; no table).",
)
def sessions(limit: int, plain: bool) -> None:
    """List stored sessions for this project (named table, newest first).

    Renders the same rich table as ``session list`` (Name · Session · Bundle ·
    Msgs · Turns · Age). ``--plain`` restores the ids-only stream for scripts.
    """
    from .kernel import session_manager

    summaries = session_manager.list_summaries(_session_store(), limit=limit)
    if not summaries:
        click.echo("no stored sessions")
        return
    if plain:
        for summary in summaries:
            click.echo(summary.session_id)
        return
    _print_session_table(summaries)


def _session_store():  # noqa: ANN202 — SessionStore (lazy import keeps --demo offline)
    from .kernel.persistence import SessionStore

    return SessionStore()


def _current_usernames() -> tuple[str, ...]:
    """Best-effort local account name(s) to redact from a sanitized export.

    The username embedded in the developer's home path is the classic identity
    leak; supplying it lets the pure ``model.sanitize`` redactor scrub it
    whole-word wherever it appears (not only inside a path). Never raises.
    """
    import getpass

    names: list[str] = []
    try:
        login = getpass.getuser()
    except Exception:  # noqa: BLE001 — username lookup is best-effort
        login = ""
    if login:
        names.append(login)
    home = Path.home().name
    if home and home not in names:
        names.append(home)
    return tuple(names)


def _echo_cross_project_hint(partial: str) -> None:
    """After a per-project 'no session found', point to the session if it lives
    in another project. Sessions are stored per working directory, so a bare
    ``resume <id>`` only sees the current dir's project — this makes the error
    actionable instead of a dead end."""
    from .kernel import session_manager

    matches = session_manager.find_across_projects(partial)
    if not matches:
        return
    click.echo("  it exists in another project — resume it from there:", err=True)
    for full_id, working_dir in matches[:3]:
        location = working_dir or "(directory unknown)"
        click.echo(f"    cd {location} && amplifier-tui resume {full_id[:8]}", err=True)
    if len(matches) > 3:
        click.echo(f"    …and {len(matches) - 3} more", err=True)


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
        click.echo("no stored sessions · start one with `amplifier-tui`")
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
            _echo_cross_project_hint(session_id)
            raise SystemExit(1) from None
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from None
    raise SystemExit(asyncio.run(_launch_tui(demo=False, bundle=bundle, resume_id=resolved)))


@main.command("continue")
@click.option("--bundle", default=None, help="Bundle name or URI.")
def continue_(bundle: str | None) -> None:
    """Resume the most recent stored session for this project.

    The no-argument shortcut for ``resume``: auto-selects the newest stored
    session (``list_summaries`` is newest-first) and launches straight into
    it, skipping the picker.
    """
    from .kernel import session_manager

    summaries = session_manager.list_summaries(_session_store(), limit=1)
    if not summaries:
        click.echo("no stored sessions · start one with `amplifier-tui`")
        raise SystemExit(0)
    latest = summaries[0]
    click.echo(f"continuing {latest.short_id}")
    raise SystemExit(
        asyncio.run(_launch_tui(demo=False, bundle=bundle, resume_id=latest.session_id))
    )


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
    click.echo("invoke with `amplifier-tui tool invoke <name> key=value ...`", err=True)
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
        amplifier-tui tool invoke read_file file_path=README.md
        amplifier-tui tool invoke some_tool data='{"k": "v"}' limit=5

    Or pass the whole argument object at once with --json:

    \b
        amplifier-tui tool invoke read_file --json '{"file_path": "README.md"}'

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
    """List stored sessions (name · id · msgs · turns · age), newest first."""
    from .kernel import session_manager

    summaries = session_manager.list_summaries(_session_store(), limit=limit)
    if not summaries:
        click.echo("no stored sessions")
        return
    _print_session_table(summaries)


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
    lineage) and seeds it with DIRECTIVE, so ``amplifier-tui resume <child>``
    runs that instruction first. Re-expresses amplifier-app-cli's ``/fork
    <directive>`` self-delegation over tui's persisted store: the child is
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
    click.echo(f"resume to run the directive: amplifier-tui resume {detail[:8]}")


@session.command("export")
@click.argument("session_id")
@click.option(
    "--sanitize",
    is_flag=True,
    help="Redact user filesystem paths (home dirs / usernames) for safe sharing.",
)
@click.option(
    "--tool-io",
    "tool_io",
    is_flag=True,
    help="Also redact tool inputs/outputs (implies --sanitize).",
)
@click.option(
    "--output",
    "-o",
    "output",
    default=None,
    metavar="FILE",
    help="Write JSON to FILE (default: stdout).",
)
def session_export(session_id: str, sanitize: bool, tool_io: bool, output: str | None) -> None:
    """Export a stored session as portable JSON (round-trips via `session import`).

    Distinct from the in-app markdown ``/export`` (human-readable but lossy):
    this is the STRUCTURED artifact that can be imported back into a session.
    ``--sanitize`` redacts user filesystem paths on top of the always-on secret
    scrub; ``--tool-io`` also blanks tool inputs/outputs. With no flags the
    export is unredacted — the existing default is unchanged.
    """
    from .kernel import session_manager, session_transfer

    store = _session_store()
    try:
        resolved = session_manager.resolve(store, session_id)
    except FileNotFoundError:
        click.echo(f"no session found matching '{session_id}'", err=True)
        _echo_cross_project_hint(session_id)
        raise SystemExit(1) from None
    except ValueError as error:
        click.echo(str(error), err=True)
        raise SystemExit(1) from None
    payload = session_transfer.export_session(
        store,
        resolved,
        sanitize=sanitize,
        redact_tool_io=tool_io,
        users=_current_usernames(),
    )
    text = session_transfer.dumps(payload)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
        label = "sanitized " if payload["sanitized"] else ""
        click.echo(f"exported {label}session {resolved[:8]} → {output}", err=True)
        return
    click.echo(text)


@session.command("import")
@click.argument("file")
@click.option("--name", "-n", "new_name", default="", help="Name for the imported session.")
def session_import(file: str, new_name: str) -> None:
    """Import a session from a portable JSON export FILE (local path).

    Mints a NEW stored session (fresh id + origin provenance) so it never
    clobbers an existing one, then lists/resumes like any native session. A
    sanitized export imports fine but keeps its redaction placeholders — the
    real content is gone by design. (The donor's share-URL import needs a share
    service the host does not run, so it is out of scope: local file only.)
    """
    from .kernel import session_transfer

    store = _session_store()
    try:
        payload = session_transfer.read_export_file(file)
        new_id = session_transfer.import_session(store, payload, name=new_name or None)
    except session_transfer.SessionTransferError as error:
        click.echo(str(error), err=True)
        raise SystemExit(1) from None
    click.echo(f"imported → {new_id}")
    click.echo(f"resume it: amplifier-tui resume {new_id[:8]}")


# ``session resume <id>`` — alias to the top-level ``resume`` command, so both
# amplifier-app-cli spellings work (``resume`` interactive + ``session resume
# <id>``). Registering the same Command object reuses the one handler rather
# than forking the logic (S4 / #148).
session.add_command(resume, "resume")


@main.command()
def doctor() -> None:
    """Setup checkup: prints the report, exit 1 when findings exist."""
    from .commands.doctor import run_standalone
    from .kernel import updater

    anchors = asyncio.run(updater.anchors_status())
    raise SystemExit(run_standalone(anchors_status=anchors))


def _package_version(dist_name: str) -> str:
    """Installed distribution version, or ``unknown`` when absent.

    Reads packaging metadata only — no ``import amplifier_core`` — so the
    ADR-0007 kernel boundary stays intact and the command runs offline.
    """
    from importlib import metadata

    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return "unknown"


@main.command()
def version() -> None:
    """Show the app version alongside amplifier-core / -foundation versions.

    The subcommand form of the ``--version`` flag; the flag stays available on
    the top-level command.
    """
    click.echo(f"amplifier-tui {__version__}")
    click.echo(f"  core        {_package_version('amplifier-core')}")
    click.echo(f"  foundation  {_package_version('amplifier-foundation')}")


# --------------------------------------------------------------------------
# stats -- cross-session cost/usage dashboard (see kernel/stats.py)
# --------------------------------------------------------------------------


@main.command()
@click.option(
    "--days",
    type=int,
    default=None,
    help="Window: last N days (0 = today, omit = all time).",
)
@click.option(
    "--models",
    "models",
    is_flag=False,
    flag_value="all",
    default=None,
    metavar="[N]",
    help="Show the per-model rollup: bare --models = all; --models N = top N.",
)
@click.option(
    "--project",
    "project",
    default=None,
    metavar="SLUG",
    help="Project to aggregate: default current project; 'all' = every project; else a slug.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON (machine-readable).")
def stats(days: int | None, models: str | None, project: str | None, as_json: bool) -> None:
    """Aggregate cost + token usage ACROSS stored sessions (cross-session dashboard).

    Re-expresses opencode's ``stats`` over tui's per-project session store: spend and
    token usage are reconstructed from each session's normalized ``provider_response_usage``
    events (the same source the live cost footer uses), rolled up by day / model / project.

    \b
      amplifier-tui stats                     current project, all time
      amplifier-tui stats --days 7 --models   last 7 days + per-model breakdown
      amplifier-tui stats --project all       every project (adds a by-project rollup)
    """
    from .kernel import stats as stats_kernel

    if days is not None and days < 0:
        raise click.UsageError("--days must be non-negative (0 = today, omit for all time)")
    sources, scope = stats_kernel.resolve_sources(project)
    report = stats_kernel.aggregate(
        sources, days=days, scope=scope, multi_project=(project == "all")
    )
    click.echo(stats_kernel.render(report, models=models, json_output=as_json))


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
@click.option(
    "--reinstall",
    is_flag=True,
    help="After clearing, reinstall the tui tool (repair a wedged install).",
)
@click.option(
    "--install-source",
    default=None,
    metavar="URI",
    help="Source for --reinstall (default: the tui git repo; use '.' from a clone).",
)
def reset(
    categories: tuple[str, ...],
    dry_run: bool,
    yes: bool,
    home_override: str | None,
    list_only: bool,
    reinstall: bool,
    install_source: str | None,
) -> None:
    """Data-safe reset: clear selected categories, preserve the rest.

    Re-expresses amplifier-app-cli's ``reset`` recovery command as a guarded,
    category-scoped cleaner scoped to tui's app home. ``--category`` names
    what to CLEAR; everything else is preserved. The default clears only the
    auto-regenerating categories (cache, registry).

    \b
    Guards:
      - --dry-run previews and removes NOTHING
      - a confirmation prompt (bypass with --yes) before any removal
      - secrets (keys) are cleared ONLY when named explicitly
      - never deletes outside the confirmed app home

    ``--reinstall`` additionally repairs a wedged install by reinstalling the
    tui tool (``uv tool install --reinstall``) after clearing — the ``uv
    tool`` analogue of app-cli's reset-and-reinstall.

    \b
    Examples:
      amplifier-tui reset --list                 Show the taxonomy
      amplifier-tui reset --dry-run              Preview the safe default
      amplifier-tui reset --category cache -y    Clear only the cache
      amplifier-tui reset -c sessions,config     Clear sessions + config
      amplifier-tui reset --reinstall -y         Clear + reinstall the tool
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

    source = install_source or reset_kernel.DEFAULT_INSTALL_SOURCE

    if plan.removed:
        click.echo("would remove:" if dry_run else "to remove:")
        for path in plan.removed:
            click.echo(f"  - {path}")
    else:
        click.echo("nothing to remove -- selected categories have no files on disk")

    if dry_run:
        if reinstall:
            click.echo(f"would reinstall: {' '.join(reset_kernel.reinstall_command(source))}")
        click.echo("DRY RUN -- nothing was changed")
        return

    if not plan.removed and not reinstall:
        return

    if not yes:
        actions: list[str] = []
        if plan.removed:
            item = f"remove {len(plan.removed)} item(s)"
            if plan.destructive_cleared:
                item += f" (incl {', '.join(plan.destructive_cleared)})"
            actions.append(item)
        if reinstall:
            actions.append("reinstall the tui tool")
        if not click.confirm("permanently " + " and ".join(actions) + "?", default=False):
            click.echo("cancelled")
            return

    if plan.removed:
        final = reset_kernel.run_reset(home, selected, dry_run=False)
        click.echo(f"removed {len(final.removed)} item(s); preserved {len(final.preserved)}")
        for path in final.preserved:
            click.echo(f"  preserved: {path}")

    if reinstall:
        click.echo(f"reinstalling tui from {source} ...")
        ok, message = reset_kernel.reinstall_tool(source)
        click.echo(message if ok else f"reinstall failed: {message}", err=not ok)
        if not ok:
            raise SystemExit(1)


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
    """Manage bundles: list, show, use, add, remove, update, warm."""


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
        click.echo(f"unknown bundle: {name} · run `amplifier-tui bundle list`", err=True)
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
@click.option(
    "--warm",
    "warm",
    is_flag=True,
    help="Pre-install the bundle's modules now (out of the boot burst).",
)
@_scope_options
def bundle_add(
    uri: str,
    name: str | None,
    as_app: bool,
    warm: bool,
    is_global: bool,
    is_project: bool,
    is_local: bool,
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
    if warm:
        # Install modules NOW so a later boot only ever skips the install —
        # the tui-side mitigation for foundation's fragile mass install.
        result = asyncio.run(bundle_admin.warm_bundle(uri))
        click.echo(
            f"warmed {resolved_name} · {result.message}"
            if result.ok
            else f"warm failed · {result.message}",
            err=not result.ok,
        )


@bundle.command("warm")
@click.argument("name")
def bundle_warm(name: str) -> None:
    """Pre-install a bundle's modules (out of the boot install burst).

    NAME is a registered bundle name or a URI. Warming installs its modules
    once so a later session that composes it only ever skips the install —
    the mitigation for the cold-boot ``activate_all`` burst getting a module
    killed. Also the recommended companion to ``bundle.deferred``: warm a
    deferred overlay so ``/bundle load`` composes it instantly."""
    from .kernel import bundle_admin

    # Resolve a registered name to its URI so `bundle warm <added-name>` works.
    settings = bundle_admin.load_merged_settings(bundle_admin.settings_paths(None, None))
    uri = bundle_admin.added_bundles(settings).get(name, name)
    result = asyncio.run(bundle_admin.warm_bundle(uri))
    if not result.ok:
        click.echo(f"warm failed · {result.message}", err=True)
        raise SystemExit(1)
    click.echo(f"warmed {name} · {result.message}")


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
    """Find the provider choice matching a user token (name/id/prefix/display)."""
    from .kernel.setup import provider_env_prefix

    needle = token.strip().lower()
    for choice in choices:
        candidates = {
            choice.module_id.lower(),
            provider_env_prefix(choice.module_id).lower(),
            choice.module_id.replace("provider-", "").lower(),
        }
        if choice.display:
            candidates.add(choice.display.lower())
        if needle in candidates:
            return choice
    return None


async def _resolve_provider_schema(choice):  # noqa: ANN001, ANN202
    """The provider's own config schema, fetching/installing the module if need be.

    A module-level seam on purpose: it is the one place setup reaches the
    network, so tests patch this rather than stubbing git.

    The ``sys.path`` graft cannot satisfy a module's third-party imports
    (issue #182: vLLM needs ``openai``), so when the schema is still
    unreadable this offers a real install into the running environment —
    app-cli's ``provider install`` behavior — on explicit confirm. Declining
    (or a failed install) returns ``None`` and the caller degrades to the
    catalog's fallback fields, never silently to the key-only basic flow.
    """
    from .kernel import setup

    if not choice.installed:
        verb = "loading" if choice.cached else "fetching"
        click.echo(f"\n  {verb} {choice.module_id} …", nl=False)
        availability = await setup.ensure_provider_available(choice.module_id, choice.source_uri)
        click.echo(" ok" if availability.available else f" {availability.reason}")
    schema = setup.load_provider_info(choice.module_id)
    if schema is None and choice.source_uri:
        try:
            wanted = click.confirm(
                f"  install {choice.module_id} into this environment to read its setup fields?",
                default=True,
            )
        except (click.Abort, EOFError):
            wanted = False
        if wanted:
            click.echo(f"  installing {choice.module_id} …", nl=False)
            ok, detail = await setup.install_provider_module(choice.module_id, choice.source_uri)
            click.echo(" ok" if ok else f" {detail}")
            if ok:
                schema = setup.load_provider_info(choice.module_id)
    return schema


def _prompt_config_field(field, *, collected, existing, env_var, keys_path, written):  # noqa: ANN001, ANN202
    """Prompt for one ``config_fields`` entry; return ``(field_id, value)``.

    Any field carrying an ``env_var`` — text as much as secret — is stored in
    keys.env and referenced from settings as ``${VAR}``, which is how the
    endpoint and tuning values end up as ``${VLLM_BASE_URL}`` /
    ``${VLLM_CONTEXT_WINDOW}`` rather than literals. Writing the key also
    exports it, so the model probe two steps later can actually connect.

    Returns ``None`` when the user aborts, and omits a field they left blank.
    On edit (*existing* given) stored values are the defaults: ``${VAR}``
    placeholders resolve through the environment and then keys.env — a fresh
    CLI process has not exported stored keys — and a blank secret keeps the
    stored reference rather than dropping the field.
    """
    from .kernel import setup

    label = field.display_name or field.id
    prompt_text = f"{label}" if not field.prompt else f"{label}\n  {field.prompt}"
    stored = existing.get(field.id) if existing else None
    current = setup.resolve_placeholder(stored) if existing else None
    if current is None and isinstance(stored, str) and stored.startswith("${"):
        current = setup.read_keys(keys_path).get(stored[2:-1])
    elif current is None and isinstance(stored, str):
        current = stored
    env_value = os.environ.get(env_var) if env_var else None
    fallback = current or env_value or field.default or ""

    # "Found in env" hint, app-cli style: the user learns a value already
    # exists before deciding whether to type over it.
    if env_value and not current:
        if field.field_type == "secret":
            click.echo(f"\n  ({env_var} found in environment — will use if you don't configure)")
        elif field.field_type == "text":
            click.echo(f"\n  (Found: {env_value})")

    try:
        if field.field_type == "boolean":
            value: str | None = (
                "true" if click.confirm(label, default=_truthy(fallback)) else "false"
            )
        elif field.field_type == "choice" and field.choices:
            click.echo(f"\n{label}")
            for index, option in enumerate(field.choices, start=1):
                click.echo(f"  [{index}] {option}")
            raw = click.prompt("  choice", default="", show_default=False).strip()
            if not raw:
                value = fallback or None
            else:
                try:
                    value = field.choices[int(raw) - 1]
                except (ValueError, IndexError):
                    click.echo(f"  invalid selection: {raw}", err=True)
                    return None
        elif field.field_type == "secret":
            suffix = " (press Enter to keep the stored value)" if fallback else ""
            entered = click.prompt(
                f"\n{prompt_text}{suffix}", hide_input=True, default="", show_default=False
            ).strip()
            value = entered or (fallback or None)
        else:
            value = click.prompt(f"\n{prompt_text}", default=fallback, show_default=bool(fallback))
            value = (value or "").strip() or None
    except (click.Abort, EOFError):
        return None

    if value is None or value == "":
        if isinstance(stored, str) and stored.startswith("${"):
            return (field.id, stored)  # keep the stored reference untouched
        if field.required:
            click.echo(f"  {label} is required", err=True)
            return None
        return (field.id, None)
    if env_var:
        setup.write_key(keys_path, env_var, str(value))
        if env_var not in written:
            written.append(env_var)
        click.echo("  ✓ Saved")
        return (field.id, f"${{{env_var}}}")
    return (field.id, value)


def _truthy(value) -> bool:  # noqa: ANN001
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _prompt_model_selection(catalog, default_model: str | None) -> str | None:
    """The ``Default Model`` picker — numbered live models, then custom.

    An empty or failed catalog is not fatal: print the reason and fall back to
    free text, because a provider can serve a model it does not advertise.
    """
    click.echo("\nDefault Model")
    if catalog.error:
        click.echo(f"  could not list models · {catalog.error}")
    if not catalog.models:
        if not catalog.error:
            click.echo("  no models advertised by the server")
        entered = click.prompt("  model name", default=default_model or "", show_default=True)
        return (entered or "").strip() or None

    options: list[str] = []
    for model in catalog.models:
        options.append(model.id)
        caps = [c for c in model.capabilities if c in ("fast", "thinking", "vision", "reasoning")]
        suffix = f"  ({', '.join(caps)})" if caps else ""
        click.echo(f"  [{len(options)}] {model.display_name or model.id}{suffix}")
    if default_model and default_model not in {m.id for m in catalog.models}:
        options.append(default_model)
        click.echo(f"  [{len(options)}] {default_model}  (current)")
    options.append("")  # custom sentinel
    click.echo(f"  [{len(options)}] custom")

    default_choice = ""
    if default_model and default_model in options:
        default_choice = str(options.index(default_model) + 1)
    raw = click.prompt(
        "  choice", default=default_choice, show_default=bool(default_choice)
    ).strip()
    if not raw:
        return default_model
    try:
        picked = options[int(raw) - 1]
    except (ValueError, IndexError):
        click.echo(f"  invalid selection: {raw}", err=True)
        return None
    if picked:
        return picked
    entered = click.prompt("  model name", default=default_model or "", show_default=True)
    return (entered or "").strip() or None


def _existing_key_override(schema, existing_config) -> str | None:  # noqa: ANN001
    """Recover an instance's own credential var from its stored ``${VAR}``.

    Re-configuring must keep using the variable the instance already owns
    rather than suggesting a fresh one (which would collide with itself) or
    resetting to the type default (app-cli's ``_recover_env_var_override``).
    """
    raw = existing_config.get(schema.key_field_id)
    if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
        current = raw[2:-1]
        if current and current != schema.key_var:
            return current
    return None


async def _configure_provider_interactive(
    choice,  # noqa: ANN001
    schema,  # noqa: ANN001
    *,
    cli_api_key: str | None,
    cli_base_url: str | None,
    cli_model: str | None,
    instance_id: str | None,
    keys_path,  # noqa: ANN001
    existing_config: dict[str, Any] | None = None,
):
    """Field-driven provider setup. Returns ``(config, written_vars)`` or None.

    Three phases, mirroring app-cli's ``configure_provider``: pre-model fields,
    then the live model picker, then any ``requires_model`` fields (which can
    ``show_when`` on the chosen model). *existing_config* (the edit path)
    supplies stored values as defaults so Enter keeps every previous choice.
    """
    from .kernel import setup

    collected: dict[str, object] = {}
    written: list[str] = []
    overrides: dict[str, str] = {}
    if cli_api_key:
        overrides[schema.key_field_id] = cli_api_key
    if cli_base_url:
        overrides["base_url"] = cli_base_url

    # A second instance of the same provider type needs its own credential
    # variable, or saving it would overwrite the first instance's key. On
    # edit, recover the variable the instance already owns instead — a fresh
    # suggestion would collide with the instance itself.
    key_env_override: str | None = None
    if instance_id and schema.key_var:
        if existing_config is not None:
            key_env_override = _existing_key_override(schema, existing_config)
        else:
            try:
                key_env_override = setup.suggest_instance_env_var(
                    choice.module_id, instance_id, setup.claimed_env_vars()
                )
            except ValueError as exc:
                click.echo(f"{exc}", err=True)
                return None
            click.echo(f"  credential variable for this instance: {key_env_override}")

    display = schema.display_name or choice.module_id
    click.echo(f"\nConfiguring {display}")

    def _env_for(field) -> str | None:  # noqa: ANN001
        if key_env_override and field.env_var and field.env_var == schema.key_var:
            return key_env_override
        return field.env_var

    def _run_fields(fields) -> bool:  # noqa: ANN001
        for field in fields:
            if not setup.should_show_field(field, collected):
                continue
            if field.id in overrides:
                collected[field.id] = overrides[field.id]
                env_var = _env_for(field)
                if env_var:
                    setup.write_key(keys_path, env_var, str(overrides[field.id]))
                    written.append(env_var)
                    collected[field.id] = f"${{{env_var}}}"
                continue
            outcome = _prompt_config_field(
                field,
                collected=collected,
                existing=existing_config,
                env_var=_env_for(field),
                keys_path=keys_path,
                written=written,
            )
            if outcome is None:
                return False
            field_id, value = outcome
            if value is not None:
                collected[field_id] = value
        return True

    pre_model = [f for f in schema.config_fields if not f.requires_model]
    post_model = [f for f in schema.config_fields if f.requires_model]
    if not _run_fields(pre_model):
        return None

    if cli_model:
        collected["default_model"] = cli_model
        click.echo(f"\nDefault Model: {cli_model}")
    else:
        current_model = (existing_config or {}).get("default_model")
        click.echo("\n  fetching available models …")
        catalog = await setup.list_provider_models(choice.module_id, collected)
        model = _prompt_model_selection(catalog, str(current_model) if current_model else None)
        if model:
            collected["default_model"] = model

    if not _run_fields(post_model):
        return None
    return collected, written


async def _init(
    provider: str | None,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    yes: bool,
    from_env: bool,
    instance_id: str | None = None,
    scope: str = "global",
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
    click.echo(f"active bundle: {status.active_bundle or 'tui (default)'}")
    click.echo("stored keys: " + (", ".join(status.stored_keys) if status.stored_keys else "none"))

    choices = await setup.onboarding_choices()
    if not choices:
        click.echo("no provider modules discovered (is amplifier-core installed?)", err=True)
        return 1

    click.echo("\nproviders:")
    for index, choice in enumerate(choices, start=1):
        mark = "✓" if choice.has_key else " "
        label = f"{choice.display} · {choice.module_id}" if choice.display else choice.module_id
        suffix = f"  · {choice.availability}" if choice.availability else ""
        click.echo(f"  {index}. [{mark}] {label}  → {choice.key_var}{suffix}")

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

    from .kernel import bundle_admin

    path = setup.keys_file()
    paths = bundle_admin.settings_paths(None, None)
    write_scope: Literal["global", "project", "local"] = (
        scope if scope in ("global", "project", "local") else "global"  # type: ignore[assignment]
    )

    if yes:
        return _init_non_interactive(
            target,
            api_key=api_key,
            base_url=base_url,
            model=model,
            instance_id=instance_id,
            keys_path=path,
            paths=paths,
            scope=write_scope,
        )

    return await _interactive_provider_setup(
        target,
        api_key=api_key,
        base_url=base_url,
        model=model,
        instance_id=instance_id,
        scope=write_scope,
    )


async def _interactive_provider_setup(
    target,  # noqa: ANN001
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    instance_id: str | None = None,
    scope: Literal["global", "project", "local"] = "global",
) -> int:
    """Interactive schema-driven setup for one provider choice.

    Shared by ``init``/``provider add`` and the ``[a] Add`` console action.
    Drives the provider's OWN declared schema when readable (so vLLM is asked
    for a server URL and ollama is not asked for a key); when the schema stays
    unreadable it falls back to the catalog's minimum fields (issue #182) and
    only degrades to the key-only basic flow for providers the catalog has no
    required fields for.
    """
    from .kernel import bundle_admin, setup

    path = setup.keys_file()
    paths = bundle_admin.settings_paths(None, None)

    schema = await _resolve_provider_schema(target)
    if schema is None or not schema.config_fields:
        fallback = setup.fallback_provider_fields(target.module_id)
        if fallback is not None:
            click.echo(
                f"  (schema unavailable for {target.module_id} — "
                "prompting for the catalog's required fields)"
            )
            schema = fallback
        else:
            if schema is None:
                click.echo(f"  (schema unavailable for {target.module_id} — basic setup)")
            return _init_basic_interactive(
                target,
                base_url=base_url,
                model=model,
                instance_id=instance_id,
                keys_path=path,
                paths=paths,
                scope=scope,
            )

    if instance_id is None and setup.instance_id_in_use(_default_instance_id(target)):
        suggestion = f"{_default_instance_id(target)}-2"
        click.echo(f"\na {_default_instance_id(target)} provider is already configured.")
        instance_id = (
            click.prompt("  instance id", default=suggestion, show_default=True) or suggestion
        ).strip()

    outcome = await _configure_provider_interactive(
        target,
        schema,
        cli_api_key=api_key,
        cli_base_url=base_url,
        cli_model=model,
        instance_id=instance_id,
        keys_path=path,
    )
    if outcome is None:
        click.echo("cancelled · nothing written to settings")
        return 0
    collected, written = outcome
    entry = setup.provider_config_entry(
        target.module_id,
        config=collected,
        instance_id=instance_id,
        source=None if target.installed else target.source_uri,
    )
    cfg_path = setup.write_provider_config(paths, scope, entry)
    if written:
        click.echo(f"\nwrote {', '.join(written)} → {path}")
    click.echo(f"configured provider {instance_id or target.module_id} → {cfg_path}")
    click.echo("run `amplifier-tui` to start a session.")
    return 0


def _default_instance_id(choice) -> str:  # noqa: ANN001
    return choice.module_id.replace("provider-", "")


def _requires_secret(choice, schema) -> bool:  # noqa: ANN001
    """Whether this provider genuinely cannot be configured without a key.

    Unknown schema ⇒ assume yes (the historical behavior, and the safe guess).
    A declared-but-optional secret (vLLM against an unauthenticated endpoint,
    ollama) ⇒ no, so ``--yes`` works for them without ``--api-key``.
    """
    del choice
    if schema is None:
        return True
    for field in schema.config_fields:
        if field.field_type == "secret":
            return field.required
    return False


def _init_non_interactive(
    target,  # noqa: ANN001
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    instance_id: str | None,
    keys_path,  # noqa: ANN001
    paths,  # noqa: ANN001
    scope: Literal["global", "project", "local"],
) -> int:
    """``--yes``: no prompts, and deliberately no network.

    Never fetches a source and never calls ``list_models()`` — a scripted or
    CI invocation must not depend on reachability. The schema is consulted only
    when the module is already importable, which is what lets a keyless or
    optional-key provider succeed here without ``--api-key``.
    """
    from .kernel import setup

    schema = setup.load_provider_info(target.module_id)
    key = (api_key or "").strip()
    if not key and _requires_secret(target, schema):
        click.echo(f"--api-key required with --yes for {target.module_id}", err=True)
        return 1

    written: list[str] = []
    key_var = target.key_var
    if key and instance_id:
        try:
            key_var = setup.suggest_instance_env_var(
                target.module_id, instance_id, setup.claimed_env_vars()
            )
        except ValueError as exc:
            click.echo(f"{exc}", err=True)
            return 1
    if key:
        setup.write_key(keys_path, key_var, key)
        written.append(key_var)
    if base_url:
        setup.write_key(keys_path, target.base_url_var, base_url.strip())
        written.append(target.base_url_var)

    entry = setup.provider_config_entry(
        target.module_id,
        key_var=key_var if key else None,
        model=(model or "").strip() or None,
        base_url=base_url.strip() if base_url else None,
        base_url_var=target.base_url_var,
        instance_id=instance_id,
        source=None if target.installed else target.source_uri,
    )
    cfg_path = setup.write_provider_config(paths, scope, entry)
    if written:
        click.echo(f"\nwrote {', '.join(written)} → {keys_path}")
    click.echo(f"configured provider {instance_id or target.module_id} → {cfg_path}")
    return 0


def _init_basic_interactive(
    target,  # noqa: ANN001
    *,
    base_url: str | None,
    model: str | None,
    instance_id: str | None,
    keys_path,  # noqa: ANN001
    paths,  # noqa: ANN001
    scope: Literal["global", "project", "local"],
) -> int:
    """The pre-schema flow, kept as the degraded path.

    Reached when the provider module cannot be fetched or introspected
    (offline, missing dependency). Still writes ``source:`` so the next boot
    installs the module and a later ``provider add`` gets the full wizard.
    """
    from .kernel import setup

    api_key = click.prompt(f"{target.key_var}", hide_input=True, default="", show_default=False)
    key = (api_key or "").strip()
    if not key:
        click.echo("no key entered · nothing written")
        return 0
    setup.write_key(keys_path, target.key_var, key)
    written = [target.key_var]
    if base_url:
        setup.write_key(keys_path, target.base_url_var, base_url.strip())
        written.append(target.base_url_var)
    entry = setup.provider_config_entry(
        target.module_id,
        key_var=target.key_var,
        model=(model or "").strip() or None,
        base_url=base_url.strip() if base_url else None,
        base_url_var=target.base_url_var,
        instance_id=instance_id,
        source=None if target.installed else target.source_uri,
    )
    cfg_path = setup.write_provider_config(paths, scope, entry)
    click.echo(f"\nwrote {', '.join(written)} → {keys_path}")
    click.echo(f"configured provider {instance_id or target.module_id} → {cfg_path}")
    click.echo("run `amplifier-tui` to start a session.")
    return 0


# --------------------------------------------------------------------------
# init console — the combined setup dashboard (app-cli `amplifier init` parity)
# --------------------------------------------------------------------------

_WriteScope = Literal["global", "project", "local"]

_SCOPE_HINTS: dict[str, tuple[str, str]] = {
    "global": ("~/.amplifier/settings.yaml", ""),
    "project": (".amplifier/settings.yaml", "(team-shared, committed)"),
    "local": (".amplifier/settings.local.yaml", "(this machine only, gitignored)"),
}


def _print_scope_indicator(console: Any, scope: str) -> None:
    """One-line "Saving to:" banner (app-cli's ``print_scope_indicator``)."""
    file_hint, parenthetical = _SCOPE_HINTS[scope]
    if scope == "global":
        console.print(f"  [dim]Saving to:[/dim] [bold]{scope}[/bold]  [dim]{file_hint}[/dim]")
    else:
        console.print(
            f"  [yellow]Saving to:[/yellow] [bold yellow]{scope}[/bold yellow]"
            f"  [dim]{file_hint}[/dim]  [yellow]{parenthetical}[/yellow]"
        )


def _prompt_scope_change(console: Any, current: _WriteScope) -> _WriteScope:
    """Numbered write-scope picker (app-cli's ``prompt_scope_change``)."""
    order: tuple[_WriteScope, ...] = ("global", "project", "local")
    console.print("\n  Write scope:")
    for index, name in enumerate(order, start=1):
        file_hint, _paren = _SCOPE_HINTS[name]
        marker = "▸" if name == current else " "
        default_tag = " (default)" if name == "global" else ""
        console.print(f"  {marker} \\[{index}] {name:<8} {file_hint:<40}{default_tag}")
    console.print()
    try:
        raw = click.prompt(
            "  Scope", default=str(order.index(current) + 1), show_default=False
        ).strip()
    except (click.Abort, EOFError):
        return current
    try:
        chosen = order[int(raw) - 1]
    except (ValueError, IndexError):
        console.print(f"  invalid selection: {raw}", style="yellow")
        return current
    if chosen != current:
        file_hint, _paren = _SCOPE_HINTS[chosen]
        console.print(
            f"  [green]✓ Switched to {chosen} scope. Changes save to {file_hint}.[/green]"
        )
    return chosen


def _render_provider_table(console: Any, *, numbered: bool = False):  # noqa: ANN202
    """The configured-providers table (★ = primary). Returns the row entries."""
    from rich.table import Table

    from .kernel import setup

    providers = setup.configured_providers()
    if not providers:
        console.print("\n  [yellow]No providers configured.[/yellow]\n")
        return providers
    table = Table(title="Configured Providers" if numbered else "Providers")
    if numbered:
        table.add_column("#", justify="right", width=3)
    table.add_column("Name/ID", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Default Model")
    table.add_column("Priority", justify="right")
    table.add_column("Source", style="dim")
    for index, entry in enumerate(providers, start=1):
        name_col = f"★ {entry.name}" if entry.primary else f"  {entry.name}"
        ptype = entry.module_id.removeprefix("provider-")
        row = [name_col, ptype, entry.model or "-", str(entry.priority), entry.scope]
        if numbered:
            row.insert(0, str(index))
        table.add_row(*row)
    console.print(table)
    return providers


def _render_routing_summary(console: Any, paths: Any) -> None:
    """Active matrix name + its Role/Model/Provider resolution table."""
    from rich.table import Table

    from .kernel import routing_admin
    from .kernel.config import load_merged_settings

    settings = load_merged_settings(paths)
    active = routing_admin.active_matrix(settings)
    console.print(f"  Routing: [bold]{active}[/bold]")
    matrices = routing_admin.load_all_matrices(
        routing_admin.discover_matrix_files(paths.global_settings.parent)
    )
    matrix_data = matrices.get(active)
    if not matrix_data:
        return
    rows = routing_admin.resolve_effective(matrix_data, settings)
    if not rows:
        return
    table = Table(title=f"Routing: {active}")
    table.add_column("Role", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Provider")
    for row in rows:
        if row.model and row.provider:
            table.add_row(row.role, row.model, row.provider)
        else:
            table.add_row(row.role, "[yellow]⚠ (no provider)[/yellow]", "[dim]-[/dim]")
    console.print(table)


def _init_console() -> int:
    """Combined setup dashboard: providers + routing + actions loop.

    The same menu console as app-cli's ``amplifier init``: render the
    configured-providers table and the active routing resolution, then loop on
    \\[p] Manage providers / \\[r] Manage routing / \\[w] Change write scope /
    \\[d] Done. First run (no providers) drops straight into the provider
    console, exactly like app-cli.
    """
    from rich.console import Console

    from .kernel import bundle_admin, setup

    console = Console()
    scope: _WriteScope = "global"

    if not setup.configured_providers():
        console.print("\n  [yellow]No providers configured. Let's set one up:[/yellow]\n")
        scope = _provider_console(scope)

    while True:
        paths = bundle_admin.settings_paths(None, None)
        console.print("\n  [bold]══════════════════════════════════════════════════════[/bold]")
        console.print("  [bold]Amplifier Setup[/bold]")
        console.print("  [bold]══════════════════════════════════════════════════════[/bold]\n")
        _print_scope_indicator(console, scope)
        console.print()
        _render_provider_table(console)
        _render_routing_summary(console, paths)

        console.print("\n  Actions:")
        console.print("    \\[p] Manage providers")
        console.print("    \\[r] Manage routing")
        console.print("    \\[w] Change write scope")
        console.print("    \\[d] Done")
        console.print()
        try:
            choice = click.prompt("  Choice", default="d", show_default=False).strip().lower()
        except (click.Abort, EOFError):
            return 0
        if choice == "d":
            return 0
        if choice == "p":
            scope = _provider_console(scope)
        elif choice == "r":
            scope = _routing_console(scope)
        elif choice == "w":
            scope = _prompt_scope_change(console, scope)


def _provider_console(scope: _WriteScope) -> _WriteScope:
    """Interactive provider management loop (app-cli's ``provider manage``).

    Tracks the write scope internally and returns it when done, so the init
    dashboard and this console stay on the same scope.
    """
    from rich.console import Console

    console = Console()
    while True:
        providers = _render_provider_table(console, numbered=True)
        _print_scope_indicator(console, scope)
        console.print("  Actions:")
        console.print("    \\[a] Add a provider")
        console.print("    \\[e] Edit a provider (enter number)")
        console.print("    \\[r] Remove a provider (enter number)")
        console.print("    \\[p] Reorder priorities")
        console.print("    \\[t] Test connections")
        console.print("    \\[w] Change write scope")
        console.print("    \\[d] Done")
        console.print()
        try:
            choice = click.prompt("  Choice", default="d", show_default=False).strip().lower()
        except (click.Abort, EOFError):
            return scope
        if choice == "d":
            return scope
        if choice == "a":
            _console_add_provider(console, scope)
        elif choice.startswith("e"):
            _console_edit_provider(console, choice, providers)
        elif choice.startswith("r"):
            _console_remove_provider(console, choice, providers)
        elif choice == "p":
            _console_reorder_providers(console, providers)
        elif choice == "t":
            _console_test_providers(console, providers)
        elif choice == "w":
            scope = _prompt_scope_change(console, scope)


def _parse_choice_number(choice: str, prefix: str, count: int, console: Any) -> int | None:
    """``e2`` / ``r 3`` → 0-based index; prompts when the number is omitted."""
    num_str = choice[len(prefix) :].strip()
    if not num_str:
        try:
            num_str = click.prompt("  Enter number", default="", show_default=False).strip()
        except (click.Abort, EOFError):
            return None
    try:
        num = int(num_str)
    except ValueError:
        console.print("  [red]Invalid input. Enter a number.[/red]")
        return None
    if not 1 <= num <= count:
        console.print(f"  [red]Invalid number. Enter 1-{count}.[/red]")
        return None
    return num - 1


def _choice_label(choice) -> str:  # noqa: ANN001
    from .kernel import setup

    return choice.display or setup.friendly_provider_name(choice.module_id)


def _console_add_provider(console: Any, scope: _WriteScope) -> None:
    """``[a]``: catalog picker (friendly names) → the shared field wizard."""
    from .kernel import setup

    choices = asyncio.run(setup.onboarding_choices())
    if not choices:
        console.print("  [red]No providers available (is amplifier-core installed?)[/red]")
        return
    ordered = sorted(choices, key=lambda c: _choice_label(c).lower())
    console.print("\n  [bold]Available providers:[/bold]")
    for index, entry in enumerate(ordered, start=1):
        console.print(f"    \\[{index}] {_choice_label(entry)}")
    try:
        raw = click.prompt("  Which provider?", default="", show_default=False).strip()
    except (click.Abort, EOFError):
        return
    if not raw:
        return
    try:
        target = ordered[int(raw) - 1]
    except (ValueError, IndexError):
        console.print(f"  [red]invalid selection: {raw}[/red]")
        return
    asyncio.run(_interactive_provider_setup(target, scope=scope))


def _console_choice_for(entry) -> Any:  # noqa: ANN001
    """A ProviderChoice for an already-configured entry (edit path)."""
    from .kernel import setup

    match = _match_provider(asyncio.run(setup.onboarding_choices()), entry.module_id)
    if match is not None:
        return match
    prefix = setup.provider_env_prefix(entry.module_id)
    return setup.ProviderChoice(
        module_id=entry.module_id,
        name=entry.name,
        key_var=f"{prefix}_API_KEY",
        base_url_var=f"{prefix}_BASE_URL",
        installed=setup.load_provider_info(entry.module_id) is not None,
        source_uri=setup.effective_provider_sources().get(entry.module_id),
    )


def _console_edit_provider(console: Any, choice: str, providers) -> None:  # noqa: ANN001
    """``[e N]``: re-run the field wizard with stored values as defaults."""
    from .kernel import bundle_admin, setup

    if not providers:
        console.print("  [yellow]No providers to edit.[/yellow]")
        return
    idx = _parse_choice_number(choice, "e", len(providers), console)
    if idx is None:
        return
    target_entry = providers[idx]
    target = _console_choice_for(target_entry)
    schema = asyncio.run(_resolve_provider_schema(target)) or setup.fallback_provider_fields(
        target_entry.module_id
    )
    if schema is None or not schema.config_fields:
        console.print(
            f"  [red]schema unavailable for {target_entry.module_id} — cannot edit; "
            f"re-add it with \\[a] instead[/red]"
        )
        return
    outcome = asyncio.run(
        _configure_provider_interactive(
            target,
            schema,
            cli_api_key=None,
            cli_base_url=None,
            cli_model=None,
            instance_id=target_entry.instance_id,
            keys_path=setup.keys_file(),
            existing_config=target_entry.config,
        )
    )
    if outcome is None:
        console.print("  [dim]Cancelled.[/dim]")
        return
    collected, _written = outcome
    entry = setup.provider_config_entry(
        target_entry.module_id,
        config=collected,
        priority=target_entry.priority,  # editing must not reshuffle priorities
        instance_id=target_entry.instance_id,
        source=target_entry.source,
    )
    paths = bundle_admin.settings_paths(None, None)
    setup.replace_provider_config(paths, target_entry.scope, entry)  # type: ignore[arg-type]
    model = collected.get("default_model", "")
    model_display = f" ({model})" if model else ""
    console.print(f"\n  [green]✓ Provider updated: {target_entry.name}{model_display}[/green]")


def _console_remove_provider(console: Any, choice: str, providers) -> None:  # noqa: ANN001
    """``[r N]``: confirm, then drop the entry from every scope."""
    from .kernel import bundle_admin, setup

    if not providers:
        console.print("  [yellow]No providers to remove.[/yellow]")
        return
    idx = _parse_choice_number(choice, "r", len(providers), console)
    if idx is None:
        return
    target_entry = providers[idx]
    try:
        if not click.confirm(f"  Remove {target_entry.name}?", default=False):
            console.print("  [dim]Cancelled.[/dim]")
            return
    except (click.Abort, EOFError):
        return
    removed = setup.remove_provider(bundle_admin.settings_paths(None, None), target_entry.name)
    if removed is None:
        console.print(f"  [red]could not remove {target_entry.name}[/red]")
        return
    console.print(f"\n  [green]✓ Removed provider: {removed.name}[/green]")


def _console_reorder_providers(console: Any, providers) -> None:  # noqa: ANN001
    """``[p]``: re-number priorities from a ``2 1 3`` style answer."""
    from .kernel import bundle_admin, setup

    if len(providers) < 2:
        console.print("  [dim]Need at least 2 providers to reorder.[/dim]")
        return
    console.print("\n  Current order:")
    for index, entry in enumerate(providers, start=1):
        console.print(f"    \\[{index}] {entry.name}")
    try:
        order_str = click.prompt(
            "  Enter new order (e.g., 2 1 3)", default="", show_default=False
        ).strip()
    except (click.Abort, EOFError):
        return
    try:
        new_order = [int(x) for x in order_str.split()]
    except ValueError:
        console.print("  [red]Invalid input. Enter numbers separated by spaces.[/red]")
        return
    if sorted(new_order) != list(range(1, len(providers) + 1)):
        console.print(f"  [red]Please enter all numbers from 1 to {len(providers)}.[/red]")
        return
    priorities = {providers[num - 1].key: pri for pri, num in enumerate(new_order, start=1)}
    setup.set_provider_priorities(bundle_admin.settings_paths(None, None), priorities)
    console.print("\n  [green]✓ Priorities updated.[/green]")


def _console_test_providers(console: Any, providers) -> None:  # noqa: ANN001
    """``[t]``: ping every provider via ``list_models()`` and tabulate ✓/✗."""
    from rich.table import Table

    from .kernel import setup

    if not providers:
        console.print("  [yellow]No providers to test.[/yellow]")
        return
    table = Table(title="Provider Test Results")
    table.add_column("Name", style="cyan")
    table.add_column("Status")
    table.add_column("Latency", justify="right")
    table.add_column("Details")
    for entry in providers:
        start = monotonic()
        catalog = asyncio.run(setup.list_provider_models(entry.module_id, entry.config))
        latency = f"{monotonic() - start:.1f}s"
        if catalog.error:
            detail = catalog.error if len(catalog.error) <= 60 else catalog.error[:57] + "..."
            table.add_row(entry.name, "[red]✗[/red]", latency, detail)
        else:
            table.add_row(
                entry.name, "[green]✓[/green]", latency, f"{len(catalog.models)} model(s) available"
            )
    console.print(table)


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
    """Set up Amplifier: provider credentials plus a routing matrix.

    With no flags this opens the setup console — the configured-providers and
    routing tables plus \\[p]/\\[r]/\\[w]/\\[d] actions, the same dashboard as
    app-cli's ``amplifier init``. Passing any flag
    (``--provider``/``--api-key``/``--from-env``/``-y``/…) bypasses the console
    and takes the non-interactive path: the key is written to
    ~/.amplifier/keys.env and the provider entry to settings (config.providers).
    """
    flags_given = any([provider, api_key, base_url, model, from_env, yes])
    if flags_given:
        raise SystemExit(asyncio.run(_init(provider, api_key, base_url, model, yes, from_env)))
    raise SystemExit(_init_console())


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
        click.echo("no providers configured · run `amplifier-tui provider add`")
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
@click.option(
    "--instance-id",
    default=None,
    help="Name a second instance of the same provider type (e.g. runpod). "
    "Routing matrices target this id.",
)
@click.option(
    "--scope",
    type=click.Choice(["global", "project", "local"]),
    default="global",
    help="Settings scope to write the provider entry into.",
)
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: never prompt (needs --api-key).")
def provider_add(
    provider_type: str | None,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    instance_id: str | None,
    scope: str,
    yes: bool,
) -> None:
    """Add and configure a provider (interactive picker when TYPE is omitted).

    Interactively this reads the provider's own config schema — server URL,
    credential, tuning fields — and then lists the models the endpoint
    actually serves, so you pick a default rather than typing one blind.

    Adding a second provider keeps the first: the newest becomes primary and
    the others stay switchable via `amplifier-tui provider use`. Use
    `--instance-id` for a second instance of the SAME provider type; it gets
    its own credential variable instead of overwriting the first's.
    """
    raise SystemExit(
        asyncio.run(
            _init(
                provider_type,
                api_key,
                base_url,
                model,
                yes,
                False,
                instance_id=instance_id,
                scope=scope,
            )
        )
    )


@provider.command("use")
@click.argument("name")
def provider_use(name: str) -> None:
    """Make NAME the primary provider (sets it to priority 1)."""
    from .kernel import bundle_admin, setup

    target = setup.use_provider(bundle_admin.settings_paths(None, None), name)
    if target is None:
        click.echo(f"unknown provider: {name} · run `amplifier-tui provider list`", err=True)
        raise SystemExit(1)
    click.echo(f"primary provider → {target.name}")


@provider.command("remove")
@click.argument("name")
def provider_remove(name: str) -> None:
    """Remove NAME from the provider configuration (every scope)."""
    from .kernel import bundle_admin, setup

    removed = setup.remove_provider(bundle_admin.settings_paths(None, None), name)
    if removed is None:
        click.echo(f"unknown provider: {name} · run `amplifier-tui provider list`", err=True)
        raise SystemExit(1)
    click.echo(f"removed provider: {removed.name}")


@provider.command("dashboard")
def provider_dashboard() -> None:
    """Show configured providers, the primary, and how to switch."""
    from .kernel import setup

    status = setup.setup_status()
    providers = setup.configured_providers()
    click.echo(f"active bundle: {status.active_bundle or 'tui (default)'}")
    click.echo("stored keys: " + (", ".join(status.stored_keys) if status.stored_keys else "none"))
    click.echo("")
    if not providers:
        click.echo("no providers configured · run `amplifier-tui provider add`")
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
    click.echo("switch with `amplifier-tui provider use <name>`")


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
                "`amplifier-tui notify enable desktop` or AMPLIFIER_TERMINAL_NOTIFICATIONS=force",
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
            "  note: no ntfy topic set — run `amplifier-tui notify set topic <topic>`",
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
# update — refresh the bundles/modules tui mounts (foundation cache)
# --------------------------------------------------------------------------


def _sha_text(sha: str | None):  # noqa: ANN202 — rich Text
    """A dim, non-hex-interpreted SHA cell (blank SHAs read as ``unknown``)."""
    from rich.text import Text

    return Text(sha[:7] if sha else "unknown", style="dim")


def _status_glyph(has_update: bool | None):  # noqa: ANN202 — rich Text
    """Map foundation's tri-state to the shared legend glyph.

    ``●`` update available · ``✓`` up to date · ``◦`` no comparison (unknown)."""
    from rich.text import Text

    if has_update is True:
        return Text("●", style="yellow")
    if has_update is False:
        return Text("✓", style="green")
    return Text("◦", style="cyan")


def _print_packages_table(console, packages) -> None:  # noqa: ANN001 — rich Console
    """The app-cli-style "Amplifier" section: app + core + foundation rows.

    ``local``/``remote`` are versions or short SHAs as the checker shaped them;
    an unavailable remote degrades to the row's dim ``note`` ("could not
    check") — never an error."""
    from rich.table import Table
    from rich.text import Text

    if not packages:
        return
    table = Table(title="Amplifier", title_justify="center", header_style="bold cyan")
    table.add_column("Package", style="green", no_wrap=True)
    table.add_column("Local", style="dim", justify="right")
    table.add_column("Remote", style="dim", justify="right")
    table.add_column("", width=1, justify="center")
    for pkg in packages:
        table.add_row(
            pkg.name,
            Text(pkg.local or "unknown", style="dim"),
            Text(pkg.remote or pkg.note or "could not check", style="dim"),
            _status_glyph(pkg.has_update),
        )
    console.print(table)


def _print_update_table(console, statuses, packages=()) -> None:  # noqa: ANN001 — rich Console
    """Render the three app-cli-parity sections: Amplifier → Modules → Bundles.

    app-cli's model: shared transitive sources (``amplifier-foundation``,
    ``skills``, ``modes``…) are referenced by nearly every composed bundle, so a
    per-bundle listing repeats each one ~15×. Instead we flatten to the *unique*
    set (:func:`updater.unique_sources`) and split it into Modules and Bundles —
    each source appears exactly once. Local/non-git sources are summarized
    separately by the uncheckable section the caller prints."""
    from rich.table import Table

    from .kernel import updater

    rows = updater.unique_sources(statuses)
    modules = [r for r in rows if r.name.startswith("amplifier-module-")]
    bundles = [r for r in rows if not r.name.startswith("amplifier-module-")]

    def _render(title: str, items: list) -> None:  # noqa: ANN001 — SourceRow list
        if not items:
            return
        table = Table(title=title, title_justify="center", header_style="bold cyan")
        table.add_column("Name", style="green", no_wrap=True)
        table.add_column("Cached", style="dim", justify="right")
        table.add_column("Remote", style="dim", justify="right")
        table.add_column("", width=1, justify="center")
        for row in items:
            table.add_row(
                row.name,
                _sha_text(row.cached),
                _sha_text(row.remote),
                _status_glyph(row.has_update),
            )
        console.print(table)

    _print_packages_table(console, packages)
    if not modules and not bundles:
        console.print("[dim]No git-tracked sources to compare.[/dim]")
    else:
        _render("Modules", modules)
        _render("Bundles", bundles)
    console.print(
        "[dim]Legend: [green]✓[/green] up to date  "
        "[yellow]●[/yellow] update available  [cyan]◦[/cyan] local changes[/dim]"
    )


async def _update(check_only: bool, yes: bool, force: bool, verbose: bool) -> int:
    from rich.console import Console

    from .kernel import updater

    console = Console()
    if force:
        console.print("Force update mode...")
        console.print("  Clearing uv cache...", style="dim")
        updater.uv_cache_clean()
    else:
        console.print("Checking for updates...")
        console.print("  Checking modules...", style="dim")
        console.print("  Checking bundles...", style="dim")

    statuses = await updater.check_bundles()
    if not statuses:
        console.print("no bundles to check")
        console.print(updater.self_update_hint(), style="dim")
        return 0

    # Amplifier packages (app + core + foundation) — advisory rows; offline
    # degrades each to a dim "could not check", never a crash.
    packages = await updater.check_packages()

    console.print()
    _print_update_table(console, statuses, packages)

    # A bundle whose check errored (unresolvable name, offline clone, …) must
    # be visible — silently omitting it made a totally broken check print
    # "all bundles up to date" on fresh machines.
    errored = [s for s in statuses if s.error]
    for status in errored:
        console.print(f"[red]✗[/red] {status.name} — {status.error}")

    # "Couldn't be checked" collapses to ONE dim summary line (app-cli shows
    # nothing at all here); the per-source listing — with cache paths shortened
    # to <repo>/modules/<module> — is opt-in via --verbose.
    uncheckable = updater.uncheckable_sources(statuses)
    if uncheckable:
        console.print()
        suffix = ":" if verbose else " — see --verbose"
        console.print(f"{len(uncheckable)} {updater.UNCHECKABLE_LABEL}{suffix}", style="dim")
        if verbose:
            for name, reason in uncheckable:
                short = updater.shorten_cache_path(name)
                line = f"  · {short} — {reason}" if reason else f"  · {short}"
                console.print(line, style="dim")

    # Anchors is composed via an include, which foundation's check skips — so
    # surface its freshness explicitly (offline degrades to a neutral note).
    anchors = await updater.anchors_status()
    if anchors.is_stale:
        console.print(f"[yellow]●[/yellow] {anchors.describe()}")
    elif anchors.error is not None or anchors.ref is None:
        console.print(anchors.describe(), style="dim")
    else:
        console.print(f"[green]✓[/green] {anchors.describe()}")

    stale = [s for s in statuses if s.has_updates]
    # What the action summary/prompt advertises must match the ● rows the table
    # shows: unique stale sources, NOT per-bundle stale flags (a shared stale
    # source referenced by 11 bundles is one update, not "11 item(s)").
    stale_sources = updater.count_stale_sources(statuses)
    package_updates = [p for p in packages if p.has_update]
    # A stale anchors cache is applicable work: `update` re-fetches the
    # tracked include (refresh_anchors) since foundation's per-bundle update
    # skips it — otherwise the "run `amplifier-tui update`" hint is circular.
    anchors_work = anchors.is_stale or (force and anchors.ref is not None and not anchors.is_pinned)
    if not stale and not anchors_work and not force:
        if errored:
            console.print(f"{len(errored)} bundle(s) could not be checked (see above)", style="red")
        else:
            console.print("✓ all bundles up to date", style="green")
        if package_updates:
            # Advisory only — this command never self-updates the app/platform.
            names = ", ".join(p.name for p in package_updates)
            verb = "has" if len(package_updates) == 1 else "have"
            console.print(f"  • Update Amplifier packages manually ({names} {verb} updates):")
        console.print(updater.self_update_hint(), style="dim")
        return 1 if errored else 0

    # Action summary (app-cli style bullets), shown before the prompt and in
    # --check-only mode.
    console.print()
    if check_only:
        console.print("Run [cyan]amplifier-tui update[/cyan] to install")
    if force:
        console.print(f"  • Re-fetch {len(statuses)} bundle(s) (--force)")
    elif stale_sources:
        console.print(f"  • Update {stale_sources} module/bundle source(s)")
    elif stale:
        console.print(f"  • Refresh {len(stale)} bundle(s)")
    if anchors_work:
        console.print("  • Refresh anchors include (behind upstream)")
    if package_updates:
        # Advisory only — updating the app/platform stays out of scope here.
        names = ", ".join(p.name for p in package_updates)
        verb = "has" if len(package_updates) == 1 else "have"
        console.print(f"  • Update Amplifier packages manually ({names} {verb} updates):")
        for line in updater.self_update_hint().splitlines():
            console.print(f"    {line}", style="dim")

    if check_only:
        return 0

    console.print()
    if not yes and not click.confirm("Proceed with update?", default=True):
        console.print("Update cancelled", style="dim")
        return 0

    targets = statuses if force else stale
    updated, failed = await updater.update_bundles([s.target for s in targets])
    if anchors_work:
        if await updater.refresh_anchors():
            updated.append("anchors")
        else:
            failed.append(("anchors", "refresh failed"))

    # Per-item apply results, then the app-cli completion lines.
    console.print()
    for name in updated:
        console.print(f"  [green]✓[/green] {name}")
    for name, error in failed:
        console.print(f"  [red]✗[/red] {name} — {error}")
    if failed:
        console.print("[yellow]⚠ Update completed with errors[/yellow]")
    else:
        console.print("[green]✓ Update complete[/green]")
    bundles_updated = [name for name in updated if name != "anchors"]
    console.print(f"Updated {len(bundles_updated)} bundle(s)")
    console.print(updater.self_update_hint(), style="dim")
    return 1 if failed or errored else 0


@main.command()
@click.option("--check-only", is_flag=True, help="Report available updates; change nothing.")
@click.option("--yes", "-y", is_flag=True, help="Apply without the confirmation prompt.")
@click.option("--force", is_flag=True, help="uv cache clean first, then re-fetch every source.")
@click.option("--verbose", "-v", is_flag=True, help="List every skipped local/non-git source.")
def update(check_only: bool, yes: bool, force: bool, verbose: bool) -> None:
    """Update the bundles/modules this app mounts (not the app or platform)."""
    raise SystemExit(asyncio.run(_update(check_only, yes, force, verbose)))


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
        console.print("Add one with: amplifier-tui source add <identifier> <uri>", style="dim")
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
    """Show the source-resolution path tui would use for MODULE_ID."""
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
# routing group — inspect/choose the model routing matrix
# (list/use/show/create/manage)
# --------------------------------------------------------------------------


@main.group("routing")
def routing() -> None:
    """Manage model routing matrices: list, use, show, create, manage."""


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
        console.print("Run `amplifier-tui update` to fetch the routing-matrix bundle.", style="dim")
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


def _render_matrix_resolution(
    console: Any, matrix_name: str, matrix_data: dict[str, Any], settings: dict[str, Any]
) -> None:
    """Print the role -> effective (model, provider) table plus a provider summary."""
    from rich.table import Table

    from .kernel import routing_admin

    rows = routing_admin.resolve_effective(matrix_data, settings)
    if not rows:
        console.print(f"matrix '{matrix_name}' has no roles defined", style="yellow")
        return
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

    provider_types = routing_admin.configured_provider_types(settings)
    if provider_types:
        primary = routing_admin.primary_provider_type(settings)
        display = [f"{pt} (\u2605)" if pt == primary else pt for pt in sorted(provider_types)]
        console.print(f"Providers: {', '.join(display)}", style="dim")
    else:
        console.print("No providers configured. Run `amplifier-tui init`.", style="yellow")


def _render_matrix_waterfall(
    console: Any, matrix_name: str, matrix_data: dict[str, Any], settings: dict[str, Any]
) -> None:
    """Print the full candidate waterfall per role (\u2605 active, \u2713 available, \u2717 missing)."""
    from .kernel import routing_admin

    provider_types = routing_admin.configured_provider_types(settings)
    description = str(matrix_data.get("description", ""))
    updated = str(matrix_data.get("updated", ""))
    console.print(f"\nMatrix: [bold]{matrix_name}[/bold]")
    if description:
        console.print(f"  {description}", style="dim")
    if updated:
        console.print(f"  Updated: {updated}", style="dim")

    for role in routing_admin.matrix_waterfall(matrix_data, provider_types):
        header = f"\n[bold cyan]{role.role}[/bold cyan]"
        if role.description:
            header += f" \u2014 {role.description}"
        console.print(header)
        for cand in role.candidates:
            cfg_str = ""
            if cand.config:
                pairs = ", ".join(f"{k}: {v}" for k, v in cand.config.items())
                cfg_str = f"  [dim]\\[{pairs}][/dim]"
            if cand.active:
                console.print(
                    f"  [green]\u2605 {cand.provider} / {cand.model}[/green]"
                    f"{cfg_str}  [green]\u2190 active[/green]"
                )
            elif cand.configured:
                console.print(f"  [dim]\u2713 {cand.provider} / {cand.model}[/dim]{cfg_str}")
            else:
                console.print(
                    f"  [dim]\u2717 {cand.provider} / {cand.model}[/dim]"
                    f"{cfg_str}  [dim]not configured[/dim]"
                )
        if not role.servable:
            console.print("  [yellow]\u26a0 no configured provider can serve this role[/yellow]")


@routing.command("show")
@click.argument("matrix_name", required=False)
@click.option(
    "--detailed", "detailed", is_flag=True, help="Show the full candidate waterfall per role."
)
def routing_show(matrix_name: str | None, detailed: bool) -> None:
    """Show the effective model routing per role for MATRIX_NAME (default: active)."""
    from rich.console import Console

    from .kernel import bundle_admin, routing_admin
    from .kernel.config import load_merged_settings

    paths = bundle_admin.settings_paths(None, None)
    home = paths.global_settings.parent
    matrices = routing_admin.load_all_matrices(
        routing_admin.discover_matrix_files(home, fetch=True)
    )
    console = Console()
    if not matrices:
        console.print("no routing matrices found")
        console.print("Run `amplifier-tui update` to fetch the routing-matrix bundle.", style="dim")
        return
    settings = load_merged_settings(paths)
    if matrix_name is None:
        matrix_name = routing_admin.active_matrix(settings)
    if matrix_name not in matrices:
        available = ", ".join(sorted(matrices)) or "none"
        click.echo(f"unknown matrix: {matrix_name} \u00b7 available: {available}", err=True)
        raise SystemExit(1)
    matrix_data = matrices[matrix_name]
    if detailed:
        _render_matrix_waterfall(console, matrix_name, matrix_data, settings)
    else:
        _render_matrix_resolution(console, matrix_name, matrix_data, settings)


def _prompt_role_assignment(
    role_name: str, role_desc: str, selectors: list[str], settings: dict[str, Any]
) -> tuple[str, str] | None:
    """Prompt for a provider (by number) + model for one role; None to skip."""
    from .kernel import routing_admin

    click.echo(f"\n{role_name}: {role_desc}" if role_desc else f"\n{role_name}")
    for index, selector in enumerate(selectors, start=1):
        click.echo(f"  [{index}] {selector}")
    click.echo("  [s] skip")
    raw = click.prompt("provider", default="s", show_default=False).strip().lower()
    if raw in ("s", ""):
        return None
    try:
        idx = int(raw)
    except ValueError:
        click.echo(f"invalid choice: {raw}", err=True)
        return None
    if idx < 1 or idx > len(selectors):
        click.echo(f"invalid choice: {raw}", err=True)
        return None
    provider = selectors[idx - 1]
    default_model = routing_admin.provider_default_model(settings, provider) or ""
    model = click.prompt("model", default=default_model, show_default=bool(default_model)).strip()
    if not model:
        return None
    return provider, model


def _print_assignments_summary(assignments: dict[str, dict[str, str]]) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title="Matrix Summary", title_justify="center", header_style="bold cyan")
    table.add_column("Role", style="cyan", no_wrap=True)
    table.add_column("Provider")
    table.add_column("Model", style="green")
    for role, info in assignments.items():
        table.add_row(role, info["provider"], info["model"])
    Console().print(table)


@routing.command("create")
def routing_create() -> None:
    """Interactively create a custom routing matrix (persisted under ~/.amplifier/routing)."""
    from .kernel import bundle_admin, routing_admin
    from .kernel.config import load_merged_settings

    paths = bundle_admin.settings_paths(None, None)
    home = paths.global_settings.parent
    settings = load_merged_settings(paths)
    selectors = routing_admin.provider_selectors(settings)
    if not selectors:
        click.echo("no providers configured \u2014 run `amplifier-tui init` first", err=True)
        raise SystemExit(1)

    roles = routing_admin.discover_roles(routing_admin.discover_matrix_files(home, fetch=True))
    if not roles:
        roles = {
            "general": "Balanced catch-all for unspecialized tasks",
            "fast": "Quick parsing, classification, utility work",
        }

    click.echo("Create Custom Routing Matrix")
    click.echo(f"providers: {', '.join(selectors)}")

    assignments: dict[str, dict[str, str]] = {}
    for role_name, role_desc in roles.items():
        result = _prompt_role_assignment(role_name, role_desc, selectors, settings)
        if result:
            provider, model = result
            assignments[role_name] = {
                "description": role_desc,
                "provider": provider,
                "model": model,
            }
            click.echo(f"  \u2713 {role_name} \u2192 {provider} / {model}")

    # general + fast are the required roles the runtime always needs.
    for required in ("general", "fast"):
        if required not in assignments:
            click.echo(f"\nrequired role '{required}' must be assigned")
            result = _prompt_role_assignment(required, roles.get(required, ""), selectors, settings)
            if not result:
                click.echo("cannot create matrix without required roles", err=True)
                raise SystemExit(1)
            provider, model = result
            assignments[required] = {
                "description": roles.get(required, ""),
                "provider": provider,
                "model": model,
            }
            click.echo(f"  \u2713 {required} \u2192 {provider} / {model}")

    _print_assignments_summary(assignments)
    while True:
        click.echo("\n  [a] add role   [e] edit role   [s] save   [q] quit")
        action = click.prompt("action", default="s", show_default=False).strip().lower()
        if action in ("q",):
            click.echo("cancelled")
            return
        if action in ("s", ""):
            break
        if action == "a":
            name = click.prompt("role name", default="", show_default=False).strip()
            if not name:
                continue
            desc = click.prompt("description", default="", show_default=False).strip()
            result = _prompt_role_assignment(name, desc, selectors, settings)
            if result:
                provider, model = result
                assignments[name] = {"description": desc, "provider": provider, "model": model}
                click.echo(f"  \u2713 {name} \u2192 {provider} / {model}")
                _print_assignments_summary(assignments)
        elif action == "e":
            name = click.prompt("role to edit", default="", show_default=False).strip()
            if name not in assignments:
                click.echo(f"unknown role: {name}", err=True)
                continue
            result = _prompt_role_assignment(
                name, assignments[name]["description"], selectors, settings
            )
            if result:
                provider, model = result
                assignments[name]["provider"] = provider
                assignments[name]["model"] = model
                click.echo(f"  \u2713 {name} \u2192 {provider} / {model}")
                _print_assignments_summary(assignments)

    name = click.prompt("matrix name", default="", show_default=False).strip()
    if not name:
        click.echo("name cannot be empty", err=True)
        raise SystemExit(1)
    if not routing_admin.matrix_name_valid(name):
        click.echo(
            "invalid name: use letters, digits, '-' and '_' (max 64, leading alphanumeric)",
            err=True,
        )
        raise SystemExit(1)
    output_dir = routing_admin.custom_routing_dir(home)
    if (output_dir / f"{name}.yaml").exists() and not click.confirm(
        f"'{name}' already exists \u2014 overwrite?", default=False
    ):
        click.echo("cancelled")
        return
    saved = routing_admin.save_matrix(
        routing_admin.build_custom_matrix(name, assignments), output_dir
    )
    click.echo(f"saved custom matrix '{name}' \u2192 {saved}")


def _manage_select(
    console: Any,
    num_str: str,
    names: list[str],
    paths: Any,
    scope: Literal["global", "project", "local"],
) -> None:
    from .kernel import routing_admin

    if not num_str:
        num_str = click.prompt("matrix number", default="", show_default=False).strip()
    try:
        num = int(num_str)
    except ValueError:
        console.print(f"invalid number: {num_str}", style="yellow")
        return
    if num < 1 or num > len(names):
        console.print(f"out of range: 1-{len(names)}", style="yellow")
        return
    name = names[num - 1]
    path = routing_admin.set_active_matrix(paths, name, scope)
    console.print(f"active routing matrix \u2192 {name}  ({scope}: {path})", style="green")


def _manage_view(
    console: Any,
    num_str: str,
    names: list[str],
    matrices: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> None:
    if not num_str:
        num_str = click.prompt("matrix number", default="", show_default=False).strip()
    try:
        num = int(num_str)
    except ValueError:
        console.print(f"invalid number: {num_str}", style="yellow")
        return
    if num < 1 or num > len(names):
        console.print(f"out of range: 1-{len(names)}", style="yellow")
        return
    name = names[num - 1]
    _render_matrix_waterfall(console, name, matrices[name], settings)


def _routing_console(scope: Literal["global", "project", "local"]) -> Any:
    """Interactive routing-matrix management loop: select, view, or create.

    Shared by ``routing manage`` and the init console's ``[r]`` action.
    Tracks the write scope internally and returns it when done.
    """
    from rich.console import Console
    from rich.table import Table

    from .kernel import bundle_admin, routing_admin
    from .kernel.config import load_merged_settings

    paths = bundle_admin.settings_paths(None, None)
    home = paths.global_settings.parent
    console = Console()

    while True:
        settings = load_merged_settings(paths)
        matrices = routing_admin.load_all_matrices(
            routing_admin.discover_matrix_files(home, fetch=True)
        )
        active = routing_admin.active_matrix(settings)
        console.print(
            f"\nActive routing matrix: [bold]{active}[/bold]  [dim](write scope: {scope})[/dim]"
        )
        if not matrices:
            console.print("no routing matrices found", style="yellow")
            console.print(
                "Run `amplifier-tui update` to fetch the routing-matrix bundle.", style="dim"
            )
            return scope

        provider_types = routing_admin.configured_provider_types(settings)
        names = sorted(matrices)
        table = Table(title="Available Matrices", title_justify="center", header_style="bold cyan")
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column("", width=1, no_wrap=True)
        table.add_column("Name", style="green", no_wrap=True)
        table.add_column("Description", style="dim", overflow="fold")
        table.add_column("Compatibility", no_wrap=True)
        for index, name in enumerate(names, start=1):
            data = matrices[name]
            marker = "\u25cf" if name == active else ""
            if provider_types:
                covered, total = routing_admin.check_compatibility(data, provider_types)
                compat = f"{covered}/{total} roles"
            else:
                compat = "no providers"
            table.add_row(str(index), marker, name, str(data.get("description", "")), compat)
        console.print(table)
        if active in matrices:
            _render_matrix_resolution(console, active, matrices[active], settings)

        console.print(
            "\n  [s<N>] select matrix   [v<N>] view details   [c] create   [w] scope   [d] done"
        )
        try:
            raw = click.prompt("choice", default="d", show_default=False).strip().lower()
        except (click.Abort, EOFError):
            return scope
        if raw in ("d", "", "q"):
            return scope
        if raw == "c":
            try:
                click.get_current_context().invoke(routing_create)
            except SystemExit:
                pass
        elif raw == "w":
            scope = _prompt_scope_change(console, scope)
        elif raw.startswith("s"):
            _manage_select(console, raw[1:].strip(), names, paths, scope)
        elif raw.startswith("v"):
            _manage_view(console, raw[1:].strip(), names, matrices, settings)
        else:
            console.print(f"unknown choice: {raw}", style="yellow")


@routing.command("manage")
@_scope_options
def routing_manage(is_global: bool, is_project: bool, is_local: bool) -> None:
    """Interactive routing-matrix management: select, view details, or create."""
    _routing_console(_scope(is_global, is_project, is_local))


if __name__ == "__main__":
    main()
