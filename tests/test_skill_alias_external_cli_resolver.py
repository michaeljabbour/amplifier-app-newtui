"""Cross-product alias parity against the REAL external amplifier-app-cli
resolver -- closes the outstanding B2 gap.

**The gap this file closes.** B2 ("honor skill aliases in the TUI") landed
with one shared fixture (``tests/test_skill_alias_fixture.py``) proven
identical across TWO paths -- but both paths live in THIS repo: the
registry-level path (``test_commands_skills.py``) and the Pilot-driven TUI
path (``test_flow_skill_aliases.py``), tied together by
``test_skill_alias_parity.py``. That proved internal self-consistency, not
cross-product parity with the actual OTHER product the original bug report
named ("/cosam, /rob ... work in the CLI but not in the TUI"). This file
drives the REAL ``amplifier-app-cli`` resolver -- not a second in-repo
stand-in for it.

**What "the real external resolver" turned out to be** (investigated
against a sibling checkout of ``amplifier-app-cli``): there is no separate
"alias resolution" module -- ALL slash-command dispatch (built-ins, modes,
and skill shortcuts alike) flows through one typed registry,
``amplifier_app_cli.ui.command_registry.compose_command_registry()`` /
``CommandRegistry.resolve()``. ``CommandProcessor.process_input()``
(``ui/command_processor.py``) calls exactly this on every ``/``-prefixed
line; a skill's ``shortcut:`` frontmatter reaches it as
``SKILL_SHORTCUTS``, populated from the ``skills_discovery`` capability's
``get_shortcuts()`` -- which, per the shared skills-discovery module both
products mount, returns ONE entry keyed by the canonical name and ANOTHER
keyed by the shortcut (stored lowercase at discovery time), so both the
bare name and the alias become independent slash triggers on the CLI side
too, exactly like ``commands/skills.py`` does here.

``command_registry.py`` is a self-contained, dependency-free module
(stdlib only -- ``dataclasses``/``enum``/``typing``, no ``amplifier_core``
import), so it can be loaded straight from the sibling checkout's own file
with zero install step and zero new dependency of this repo's own -- this
is option (a) from the compliance brief: "if the resolver is importable as
a library, drive both it and this repo's registry from the one shared
fixture and assert identical canonical resolution." It is loaded via
``importlib`` at test time, never vendored, copied, or committed here.

**Real spellings, not synthetic ones.** ``REPORTED_ALIAS_FIXTURE``
(``tests/test_skill_alias_fixture.py``) is the literal pair named in the
original report: ``cranky-old-sam`` (``/cosam``) and ``restless-old-brian``
(``/rob``) -- both real persona-advisor skills shipped in this ecosystem's
skill catalog, confirmed directly against each one's own ``SKILL.md``
``shortcut:`` frontmatter.

**Skip semantics (hard requirement).** ``amplifier-app-cli`` is a sibling
product this repo has zero dependency ties to by policy (see
``pipelines/README.md``'s gene-transfer note) and must never be a hard
dependency of the default gate. When no candidate path resolves to a real
``command_registry.py``, EVERY test below is cleanly ``SKIPPED`` with a
reason naming exactly what was checked -- never a failure, never a silent
no-op, never network/credentials. Point ``AMPLIFIER_APP_CLI_PATH`` at a
real checkout to actually run this tier::

    AMPLIFIER_APP_CLI_PATH=~/dev/amplifier-app-cli uv run pytest -q \\
        tests/test_skill_alias_external_cli_resolver.py -v

**Honest boundary.** This proves canonical-resolution parity (AC1/AC5) by
loading and calling the real CLI's own resolution code -- execution-level,
not merely contract-level, whenever the sibling checkout is present. It
does NOT boot the real CLI's full interactive REPL/session (that needs
``amplifier_core`` + a live provider), so argument-forwarding-through-the-
whole-CLI-process and the CLI's collision policy (which raises on a
duplicate registration, vs. this repo's report-and-skip -- a real,
pre-existing divergence noticed during this investigation, out of scope
for this gap and left as-is) remain untested here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from amplifier_app_tui.commands.builtin import build_registry
from amplifier_app_tui.commands.skills import register_skill_commands
from amplifier_app_tui.kernel.session_ops import SkillInfo

from .test_skill_alias_fixture import REPORTED_ALIAS_FIXTURE

_RESOLVER_RELATIVE_PATH = Path("amplifier_app_cli") / "ui" / "command_registry.py"


def _candidate_checkouts() -> tuple[Path, ...]:
    """Where a real ``amplifier-app-cli`` checkout might live, in priority
    order -- never a network fetch, always a local path check.

    1. ``$AMPLIFIER_APP_CLI_PATH`` -- explicit override, any location.
    2. ``~/dev/amplifier-app-cli`` -- the convention this repo's own docs
       already use (``docs/audits/``, ``pipelines/README.md``).
    3. A sibling of this repo's own checkout -- CI layouts that clone both
       repos side by side.
    """
    candidates: list[Path] = []
    env = os.environ.get("AMPLIFIER_APP_CLI_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "dev" / "amplifier-app-cli")
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root.parent / "amplifier-app-cli")
    ordered: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return tuple(ordered)


def _load_external_command_registry() -> ModuleType | None:
    """Load the REAL ``amplifier_app_cli.ui.command_registry`` module
    straight from a sibling checkout's own file on disk -- never vendored,
    never copied, never installed as a dependency of this repo. Returns
    ``None`` (never raises) when no candidate checkout has the file, or
    loading it fails for any reason -- the caller turns that into a clean
    skip, never a failure.
    """
    for base in _candidate_checkouts():
        module_path = base / _RESOLVER_RELATIVE_PATH
        if not module_path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "_external_amplifier_app_cli_command_registry", module_path
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # Register BEFORE exec: CommandSpec is an ``@dataclass(slots=True)``,
            # and dataclass's slots processing looks up ``sys.modules[cls.__module__]``
            # while building the class -- a module executed without first being
            # registered in ``sys.modules`` fails that lookup (``AttributeError:
            # 'NoneType' object has no attribute '__dict__'``). Registering first
            # is the standard fix for loading a dataclass-bearing module this way.
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 -- any load failure degrades to a clean skip
            sys.modules.pop(spec.name, None) if spec is not None else None
            continue
        return module
    return None


_EXTERNAL_CLI: ModuleType | None = _load_external_command_registry()
_CHECKED = ", ".join(str(base / _RESOLVER_RELATIVE_PATH) for base in _candidate_checkouts())

pytestmark = pytest.mark.skipif(
    _EXTERNAL_CLI is None,
    reason=(
        "sibling amplifier-app-cli checkout not found (or its "
        "command_registry.py failed to load) -- checked: "
        f"{_CHECKED} -- this cross-product parity tier is opt-in and never "
        "blocks the default gate; set AMPLIFIER_APP_CLI_PATH to point at a "
        "real checkout to run it"
    ),
)


def _cli_skill_shortcuts(skills: tuple[SkillInfo, ...]) -> dict[str, dict[str, str]]:
    """Reshape the shared fixture into the ``Mapping[trigger, {"name": ...}]``
    shape amplifier-app-cli's own skills discovery produces (one entry
    keyed by the canonical name, one keyed by the shortcut, when they
    differ) -- the exact input shape
    ``CommandProcessor._populate_skill_shortcuts`` feeds to
    ``compose_command_registry`` in the real product.
    """
    shortcuts: dict[str, dict[str, str]] = {}
    for skill in skills:
        entry = {"name": skill.name, "description": skill.description}
        shortcuts[skill.name] = entry
        if skill.shortcut and skill.shortcut != skill.name:
            shortcuts[skill.shortcut] = entry
    return shortcuts


def _cli_resolve(spelling: str) -> str | None:
    """Resolve *spelling* through the REAL external resolver; ``None`` when
    it has no route (mirrors ``CommandRegistry.resolve``)."""
    external = _EXTERNAL_CLI
    assert external is not None  # guaranteed when tests run (pytestmark)
    registry = external.compose_command_registry(
        {}, skill_shortcuts=_cli_skill_shortcuts(REPORTED_ALIAS_FIXTURE)
    )
    spec = registry.resolve(spelling)
    return None if spec is None else spec.target


def _tui_resolve(spelling: str, fake_command_context) -> str | None:
    """Resolve *spelling* through THIS repo's real registry + skill
    commands (the same production code ``ui/app.py`` drives), observing
    what it actually invokes ``load_skill`` with."""
    registry = build_registry()
    register_skill_commands(registry, REPORTED_ALIAS_FIXTURE)
    if not registry.parse_and_run(fake_command_context, spelling):
        return None
    last_call = fake_command_context.calls[-1]
    assert last_call.startswith("load_skill:")
    return last_call.removeprefix("load_skill:")


_SPELLINGS = [
    ("/cranky-old-sam", "cranky-old-sam"),
    ("/cosam", "cranky-old-sam"),
    ("/restless-old-brian", "restless-old-brian"),
    ("/rob", "restless-old-brian"),
]
"""Every canonical name and every alias for BOTH real skills named in the
original report, paired with the canonical skill each must resolve to --
both the CLI-real test and the TUI-real test below are parametrized over
this SAME list."""


def test_fixture_carries_the_exact_reported_shortcuts() -> None:
    """Guard the fixture itself: if this ever stops naming the real
    ``/cosam``/``/rob`` spellings from the original report, every test
    below would quietly start proving something else."""
    assert {skill.shortcut for skill in REPORTED_ALIAS_FIXTURE} == {"cosam", "rob"}


@pytest.mark.parametrize(("spelling", "expected"), _SPELLINGS)
def test_real_external_cli_resolver_resolves_to_the_canonical_skill(
    spelling: str, expected: str
) -> None:
    """AC1 (external half): the REAL amplifier-app-cli resolver -- its own
    ``compose_command_registry`` / ``CommandRegistry.resolve``, loaded
    unmodified from the sibling checkout -- resolves every reported
    spelling to its canonical skill."""
    assert _cli_resolve(spelling) == expected


@pytest.mark.parametrize(("spelling", "expected"), _SPELLINGS)
def test_this_repos_resolver_resolves_to_the_same_canonical_skill(
    spelling: str, expected: str, fake_command_context
) -> None:
    """AC1 (TUI half): this repo's OWN registry, over the identical
    fixture, agrees -- matches the existing in-repo parity test's shape
    (``test_skill_alias_parity.py``) but over the real-world spellings."""
    assert _tui_resolve(spelling, fake_command_context) == expected


@pytest.mark.parametrize(("spelling", "expected"), _SPELLINGS)
def test_real_cli_and_this_repos_tui_agree_on_every_reported_alias(
    spelling: str, expected: str, fake_command_context
) -> None:
    """AC5, the literal cross-product proof: the REAL external CLI
    resolver and this repo's own resolver, driven by the SAME fixture,
    resolve the SAME spelling to the SAME canonical skill -- not just each
    independently matching *expected* (the two tests above), but matching
    EACH OTHER."""
    cli_result = _cli_resolve(spelling)
    tui_result = _tui_resolve(spelling, fake_command_context)
    assert cli_result == expected
    assert tui_result == expected
    assert cli_result == tui_result
