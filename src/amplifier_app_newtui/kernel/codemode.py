"""The Code Mode execution backend (target: Python) — a genuinely-restricted sandbox.

Re-expresses opencode's confined ``execute`` interpreter (`.ai/oc_donor.md`) for
this Python host. A model-authored orchestration program runs in a **child
subprocess** (`python -I -S`, scrubbed env) whose namespace has:

- **no `import`** (a raising ``__import__``; imports are AST-rejected up front),
- **no filesystem/process/network authority** (`open`, `eval`, `exec`, `compile`,
  `input`, ... are stripped from builtins),
- **no dunder introspection** (``x.__class__`` style escapes are AST-rejected), and
- exactly one capability: ``tools.<ns>.<tool>(input)`` bridged back to the host's
  own tool invoker over a line-delimited JSON pipe, plus ``log(...)``.

This is a *policy* sandbox — a restricted-authority environment plus an allow-listed
call bridge — matching the host's own "policy enforcement, not an OS sandbox for
opaque interpreter code" stance (ARCHITECTURE §7). Expected program and tool
failures come back as :class:`~amplifier_app_newtui.model.codemode.ExecuteResult`
DATA (never raised), and the host owns authority — a program can only exercise the
tools it is handed (donor laws, `.ai/oc_donor.md` §4/§5).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from typing import IO, Any

from ..model.codemode import (
    DiagnosticKind,
    ExecuteResult,
    ExecutionLimits,
    ToolCall,
    diagnostic_result,
)

# The host seam: given a tool path ("ns.tool") and a plain-data input, return a
# JSON-serializable result, or raise :class:`ToolInvokerError` for a safe,
# model-visible refusal. Any OTHER exception is sanitized (donor law 4).
ToolInvoker = Callable[[str, Mapping[str, Any]], Any]


class ToolInvokerError(Exception):
    """A safe, model-visible tool refusal (the host analogue of ``toolError``)."""


_MAIN = "_codemode_main"

_KIND_BY_NAME: dict[str, DiagnosticKind] = {
    "parse_error": DiagnosticKind.PARSE_ERROR,
    "unsupported_syntax": DiagnosticKind.UNSUPPORTED_SYNTAX,
    "tool_failure": DiagnosticKind.TOOL_FAILURE,
    "invalid_data_value": DiagnosticKind.INVALID_DATA_VALUE,
    "execution_failure": DiagnosticKind.EXECUTION_FAILURE,
}

# The child harness, run via ``python -I -S -c``. It never writes to the protocol
# channel except structured JSON lines; user ``print`` is unavailable, and stray
# writes are diverted to stderr. Kept as a string so it is not host-imported.
_CHILD_HARNESS = r"""
import ast as _ast, json as _json, sys as _sys

_proto = _sys.stdout
_sys.stdout = _sys.stderr          # keep the protocol channel clean
_stdin = _sys.stdin


def _send(obj):
    _proto.write(_json.dumps(obj) + "\n")
    _proto.flush()


def _recv():
    line = _stdin.readline()
    if not line:
        raise SystemExit(0)
    return _json.loads(line)


_LOGS = []


def log(*args):
    _LOGS.append(" ".join(str(a) for a in args))


class _ToolFailure(Exception):
    pass


class _Node:
    __slots__ = ("_p",)

    def __init__(self, path):
        object.__setattr__(self, "_p", path)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Node(object.__getattribute__(self, "_p") + [name])

    def __call__(self, arg=None, **kwargs):
        path = object.__getattribute__(self, "_p")
        if not path:
            raise TypeError("tools is not callable")
        payload = arg if arg is not None else kwargs
        if payload is None:
            payload = {}
        _send({"t": "call", "path": ".".join(path), "input": payload})
        resp = _recv()
        if resp.get("ok"):
            return resp.get("value")
        raise _ToolFailure(resp.get("message", "tool failed"))


def _raise_import(*a, **k):
    raise ImportError("imports are not available in code mode")


_SAFE_NAMES = (
    "abs all any ascii bin bool bytearray bytes callable chr complex dict divmod "
    "enumerate filter float format frozenset hex int isinstance issubclass iter len "
    "list map max min next oct ord pow range repr reversed round set slice sorted str "
    "sum tuple zip True False None"
).split()
_SAFE_EXC = (
    "Exception ValueError TypeError KeyError IndexError RuntimeError StopIteration "
    "ZeroDivisionError ArithmeticError AttributeError AssertionError LookupError "
    "OverflowError NotImplementedError"
).split()

import builtins as _b

_SAFE = {name: getattr(_b, name) for name in _SAFE_NAMES if hasattr(_b, name)}
_SAFE.update({name: getattr(_b, name) for name in _SAFE_EXC if hasattr(_b, name)})
_SAFE["__import__"] = _raise_import


def _main():
    config = _recv()
    code = config.get("code", "")
    wrapped = "def %MAIN%():\n" + "".join("    " + ln + "\n" for ln in code.splitlines())
    try:
        compiled = compile(wrapped, "<codemode>", "exec")
    except SyntaxError as exc:
        _send({"t": "done", "ok": False, "kind": "parse_error", "message": str(exc), "logs": _LOGS})
        return
    g = {"__builtins__": _SAFE, "tools": _Node([]), "log": log}
    try:
        exec(compiled, g)
        value = g["%MAIN%"]()
    except _ToolFailure as exc:
        _send({"t": "done", "ok": False, "kind": "tool_failure", "message": str(exc), "logs": _LOGS})
        return
    except ImportError as exc:
        _send({"t": "done", "ok": False, "kind": "unsupported_syntax", "message": str(exc), "logs": _LOGS})
        return
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the program's own error, message only
        _send({"t": "done", "ok": False, "kind": "execution_failure", "message": str(exc) or type(exc).__name__, "logs": _LOGS})
        return
    try:
        _json.dumps(value)
    except (TypeError, ValueError):
        _send({"t": "done", "ok": False, "kind": "invalid_data_value", "message": "program returned non-JSON data", "logs": _LOGS})
        return
    _send({"t": "done", "ok": True, "value": value, "logs": _LOGS})


try:
    _main()
except SystemExit:
    pass
except BaseException as exc:  # noqa: BLE001
    try:
        _send({"t": "done", "ok": False, "kind": "execution_failure", "message": "sandbox harness error", "logs": []})
    except BaseException:  # noqa: BLE001
        pass
""".replace("%MAIN%", _MAIN)


def audit_program(code: str) -> tuple[DiagnosticKind, str] | None:
    """Static gate before execution: reject imports and dunder introspection.

    Returns ``None`` when the program is admissible, else a ``(kind, message)``
    for the diagnostic to surface. Auditing the function-wrapped form lets a
    program use a top-level ``return`` (donor parity) while still rejecting the
    classic ``().__class__.__subclasses__()`` authority escape and any ``import``.
    """
    wrapped = "def " + _MAIN + "():\n" + "".join("    " + line + "\n" for line in code.splitlines())
    try:
        tree = ast.parse(wrapped)
    except SyntaxError as exc:
        return (DiagnosticKind.PARSE_ERROR, str(exc))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return (DiagnosticKind.UNSUPPORTED_SYNTAX, "import is not available in code mode")
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
        ):
            return (
                DiagnosticKind.UNSUPPORTED_SYNTAX,
                "dunder attribute access is not permitted in code mode",
            )
        if (
            isinstance(node, ast.Name)
            and node.id.startswith("__")
            and node.id.endswith("__")
            and node.id != "__" + "name__"  # never emitted by the wrapper; guard anyway
        ):
            return (
                DiagnosticKind.UNSUPPORTED_SYNTAX,
                "dunder name access is not permitted in code mode",
            )
    return None


def _child_env() -> dict[str, str]:
    """A minimal, scrubbed environment for the sandbox child (defense in depth)."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _write(stream: IO[str], obj: Any) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


class SandboxRunner:
    """Runs a model-authored orchestration program under host policy limits.

    ``invoker`` is the host's tool seam; ``limits`` are the three host-policy
    knobs (timeout / max tool calls / max output bytes). Construction is cheap;
    call :meth:`run` per program. Never raises for a program or tool failure —
    those come back as an :class:`ExecuteResult` diagnostic.
    """

    def __init__(
        self,
        invoker: ToolInvoker,
        *,
        limits: ExecutionLimits | None = None,
        python: str | None = None,
    ) -> None:
        self._invoker = invoker
        self._limits = limits
        self._python = python or sys.executable

    def run(self, code: str) -> ExecuteResult:
        audit = audit_program(code)
        if audit is not None:
            kind, message = audit
            return diagnostic_result(kind, message)
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed interpreter, isolated, restricted harness
                [self._python, "-I", "-S", "-c", _CHILD_HARNESS],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=_child_env(),
            )
        except OSError as exc:
            return diagnostic_result(
                DiagnosticKind.EXECUTION_FAILURE, f"could not start the code-mode sandbox: {exc}"
            )

        holder: dict[str, ExecuteResult] = {}
        worker = threading.Thread(
            target=self._serve, args=(proc, json.dumps({"code": code}), holder), daemon=True
        )
        worker.start()
        timeout = self._limits.timeout_seconds if self._limits else None
        worker.join(timeout)
        if worker.is_alive():
            _kill(proc)
            worker.join(2)
            return diagnostic_result(
                DiagnosticKind.TIMEOUT_EXCEEDED,
                f"execution exceeded {self._limits.timeout_ms} ms"  # type: ignore[union-attr]
                if self._limits and self._limits.timeout_ms is not None
                else "execution timed out",
                tool_calls=holder.get("_partial", _empty()).tool_calls,
            )
        result = holder.get("result")
        if result is None:
            return diagnostic_result(DiagnosticKind.EXECUTION_FAILURE, "sandbox produced no result")
        return self._apply_output_limit(result)

    def _serve(self, proc: subprocess.Popen[str], payload: str, holder: dict[str, Any]) -> None:
        tool_calls: list[ToolCall] = []
        try:
            assert proc.stdin is not None and proc.stdout is not None
            _write(proc.stdin, json.loads(payload))
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break
                holder["_partial"] = ExecuteResult(ok=False, tool_calls=tuple(tool_calls))
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("t")
                if kind == "call":
                    if self._handle_call(proc, msg, tool_calls, holder):
                        return
                elif kind == "done":
                    holder["result"] = self._finish(msg, tool_calls)
                    return
            # stdout closed with no ``done`` — the child crashed or was killed.
            holder.setdefault(
                "result",
                diagnostic_result(
                    DiagnosticKind.EXECUTION_FAILURE,
                    "the code-mode sandbox exited before completing",
                    tool_calls=tool_calls,
                ),
            )
        except Exception:  # noqa: BLE001 - the worker must never escape
            holder.setdefault(
                "result",
                diagnostic_result(
                    DiagnosticKind.EXECUTION_FAILURE,
                    "code-mode sandbox protocol error",
                    tool_calls=tool_calls,
                ),
            )
        finally:
            _kill(proc)

    def _handle_call(
        self,
        proc: subprocess.Popen[str],
        msg: Mapping[str, Any],
        tool_calls: list[ToolCall],
        holder: dict[str, Any],
    ) -> bool:
        """Service one bridged tool call. Returns True when execution must stop."""
        name = str(msg.get("path", ""))
        limits = self._limits
        if (
            limits
            and limits.max_tool_calls is not None
            and len(tool_calls) >= limits.max_tool_calls
        ):
            tool_calls.append(ToolCall(name=name, status="error"))
            holder["result"] = diagnostic_result(
                DiagnosticKind.TOOL_CALL_LIMIT_EXCEEDED,
                f"exceeded max_tool_calls ({limits.max_tool_calls})",
                tool_calls=tool_calls,
            )
            return True
        raw_input = msg.get("input")
        tool_input: Mapping[str, Any] = raw_input if isinstance(raw_input, Mapping) else {}
        assert proc.stdin is not None
        try:
            value = self._invoker(name, tool_input)
            json.dumps(value)  # plain-data boundary (donor: InvalidToolOutput)
        except ToolInvokerError as exc:
            tool_calls.append(ToolCall(name=name, status="error"))
            _write(proc.stdin, {"ok": False, "message": str(exc)})
            return False
        except (TypeError, ValueError):
            tool_calls.append(ToolCall(name=name, status="error"))
            _write(proc.stdin, {"ok": False, "message": f"{name} returned non-serializable data"})
            return False
        except Exception:  # noqa: BLE001 - sanitize unknown host failure (donor law 4)
            tool_calls.append(ToolCall(name=name, status="error"))
            _write(proc.stdin, {"ok": False, "message": f"{name} failed"})
            return False
        tool_calls.append(ToolCall(name=name, status="completed"))
        _write(proc.stdin, {"ok": True, "value": value})
        return False

    def _finish(self, msg: Mapping[str, Any], tool_calls: list[ToolCall]) -> ExecuteResult:
        logs = tuple(str(item) for item in (msg.get("logs") or []))
        if msg.get("ok"):
            return ExecuteResult(
                ok=True, value=msg.get("value"), logs=logs, tool_calls=tuple(tool_calls)
            )
        kind = _KIND_BY_NAME.get(str(msg.get("kind")), DiagnosticKind.EXECUTION_FAILURE)
        return diagnostic_result(
            kind, str(msg.get("message", "execution failed")), tool_calls=tool_calls, logs=logs
        )

    def _apply_output_limit(self, result: ExecuteResult) -> ExecuteResult:
        limits = self._limits
        if not result.ok or limits is None or limits.max_output_bytes is None:
            return result
        rendered = result.render_output()
        budget = limits.max_output_bytes
        if len(rendered.encode("utf-8")) <= budget:
            return result
        clipped = rendered.encode("utf-8")[:budget].decode("utf-8", "ignore")
        return ExecuteResult(
            ok=True,
            value=f"{clipped}\n… [output truncated to {budget} bytes]",
            tool_calls=result.tool_calls,
            truncated=True,
        )


def _empty() -> ExecuteResult:
    return ExecuteResult(ok=False)


def _kill(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


__all__ = [
    "SandboxRunner",
    "ToolInvoker",
    "ToolInvokerError",
    "audit_program",
]
