"""Owner-gated parity loop: pass record, clean-pass streak, and the owner gate.

`pipelines/parity_loop.py` is stdlib-only tooling that lives outside the package
(like `pipelines/ledger.py`), so it is loaded from its path rather than imported.
The two artifacts it writes are redirected at a tmp dir through the same env
overrides the pipeline uses.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import tokenize
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "pipelines" / "parity_loop.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("parity_loop_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load()
    monkeypatch.setenv(module.PASSES_ENV, str(tmp_path / "passes.tsv"))
    monkeypatch.setenv(module.GATES_ENV, str(tmp_path / "gates.tsv"))
    return module


# --------------------------------------------------------------- pass records


def test_record_pass_numbers_rows_and_flags_outcome(loop: ModuleType) -> None:
    first = loop.record_pass("abc1234", "-", "2026-08-02", "first re-audit")
    second = loop.record_pass("def5678", "120:notify-cli", "2026-08-03")

    assert first[0] == "1"
    assert loop.outcome_of(first) == loop.OUTCOME_CLEAN
    assert first[4] == "0" and first[5] == "-"

    assert second[0] == "2"
    assert loop.outcome_of(second) == loop.OUTCOME_GAPS
    assert second[4] == "1" and second[5] == "120"


def test_record_pass_registers_every_discovered_gap_as_pending(loop: ModuleType) -> None:
    loop.record_pass("abc1234", "120:notify-cli,121")

    assert loop.disposition_of("120") == "pending"
    assert loop.disposition_of("121") == "pending"
    assert loop.may_proceed("120") is False
    rows = {row[0]: row for row in loop.read_gates()}
    assert rows["120"][1] == "notify-cli"
    assert rows["121"][1] == "-"


def test_registering_a_known_gap_never_overwrites_its_disposition(loop: ModuleType) -> None:
    loop.record_pass("abc1234", "120:notify-cli")
    loop.decide("120", "rejected", "owner", "belongs below the harness")

    loop.record_pass("def5678", "120:notify-cli")

    assert loop.disposition_of("120") == "rejected"
    assert len([r for r in loop.read_gates() if r[0] == "120"]) == 1


def test_artifact_is_rerunnable_against_a_later_commit(loop: ModuleType) -> None:
    loop.record_pass("e6b50cd", "120", "2026-07-23")
    loop.record_pass("4767699", "-", "2026-08-02")

    commits = [row[2] for row in loop.read_passes()]
    assert commits == ["e6b50cd", "4767699"]


# ---------------------------------------------------- consecutive-clean-pass counter


def test_clean_streak_counts_only_the_trailing_clean_passes(loop: ModuleType) -> None:
    assert loop.clean_streak() == 0
    loop.record_pass("c1", "-")
    loop.record_pass("c2", "-")
    assert loop.clean_streak() == 2

    loop.record_pass("c3", "130")  # a gap resets the streak
    assert loop.clean_streak() == 0

    loop.record_pass("c4", "-")
    assert loop.clean_streak() == 1


def test_streak_is_derived_from_evidence_not_the_stored_word(loop: ModuleType) -> None:
    """A hand-edited `clean` flag cannot manufacture a streak."""
    path = loop.passes_file()
    path.write_text(
        loop.PASSES_HEADER + "1\t2026-08-02\tabc\tclean\t2\t130,131\tflag says clean, gaps say no\n"
    )

    assert loop.outcome_of(loop.read_passes()[0]) == loop.OUTCOME_GAPS
    assert loop.clean_streak() == 0


def test_run_ends_after_three_consecutive_clean_passes(loop: ModuleType, capsys) -> None:
    for _ in range(2):
        loop.record_pass("sha", "-")
    assert loop.main(["should-continue"]) == 0
    assert capsys.readouterr().out.strip() == "CONTINUE clean_streak=2/3"

    loop.record_pass("sha", "-")
    assert loop.main(["should-continue"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("DONE reason=three-consecutive-clean-passes")


def test_three_clean_passes_that_are_not_consecutive_do_not_end_the_run(
    loop: ModuleType, capsys
) -> None:
    loop.record_pass("s1", "-")
    loop.record_pass("s2", "-")
    loop.record_pass("s3", "140")
    loop.record_pass("s4", "-")

    loop.main(["should-continue"])
    assert capsys.readouterr().out.strip() == "CONTINUE clean_streak=1/3"


def test_owner_can_end_the_run_before_any_clean_pass(loop: ModuleType, capsys) -> None:
    loop.record_pass("s1", "150")
    loop.end_run("mjabbour", "parity is a decision process; remaining gaps are deferred")

    assert loop.clean_streak() == 0
    loop.main(["should-continue"])
    out = capsys.readouterr().out.strip()
    assert out.startswith("DONE reason=owner-ended")
    assert "owner=mjabbour" in out


# ------------------------------------------------- the counters stay separate


def test_clean_pass_counter_is_not_the_fix_retry_counter(loop: ModuleType) -> None:
    """The run-level streak and the per-gap fix-retry budget never interact."""
    # This tool owns the read-only lane only. No executable token in it names
    # the transfer pipeline's ledger, where the 3-fix-retry budget is settled
    # (the docstring may discuss it; the code may not touch it).
    code = tokenize.generate_tokens(io.StringIO(TOOL.read_text()).readline)
    executable = [
        tok.string
        for tok in code
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL)
    ]
    assert not [tok for tok in executable if "ledger" in tok.lower()]

    ledger = REPO_ROOT / "pipelines" / "ledger.tsv"
    before = ledger.read_bytes()
    loop.record_pass("sha", "160")
    loop.decide("160", "accepted", "owner")
    loop.main(["gate", "160"])
    assert ledger.read_bytes() == before

    # Repeated code-changing attempts on one gap leave the run-level streak
    # untouched; only a recorded read-only pass can move it.
    loop.record_pass("sha", "-")
    assert loop.clean_streak() == 1
    for _ in range(5):
        loop.decide("160", "accepted", "owner", "retry attempt")
    assert loop.clean_streak() == 1


# ----------------------------------------------------------------- owner gate


@pytest.mark.parametrize(
    ("disposition", "expected_code"),
    [
        ("accepted", 0),
        ("rejected", 1),
        ("deferred", 1),
        ("already-covered", 1),
        ("pending", 1),
    ],
)
def test_only_accepted_opens_the_code_changing_route(
    loop: ModuleType, capsys, disposition: str, expected_code: int
) -> None:
    loop.record_pass("sha", "170")
    loop.decide("170", disposition, "owner", "decision recorded")

    assert loop.main(["gate", "170"]) == expected_code
    out = capsys.readouterr().out.strip()
    assert out.startswith("PROCEED" if expected_code == 0 else "BLOCKED")
    assert loop.may_proceed("170") is (expected_code == 0)


def test_a_gap_with_no_disposition_is_blocked(loop: ModuleType, capsys) -> None:
    assert loop.main(["gate", "999"]) == 1
    assert capsys.readouterr().out.strip() == "BLOCKED gap=999 disposition=undecided"
    assert loop.may_proceed("999") is False


def test_decide_upserts_and_records_owner_and_note(loop: ModuleType) -> None:
    loop.record_pass("sha", "180:reset-command")
    loop.decide("180", "deferred", "mjabbour", "after the 0.2 release")
    row = loop.decide("180", "accepted", "mjabbour", "pulled into 0.2")

    assert [r[0] for r in loop.read_gates()].count("180") == 1
    assert row[2] == "accepted"
    assert row[3] == "mjabbour"
    assert row[5] == "pulled into 0.2"


def test_unknown_disposition_is_rejected_without_writing(loop: ModuleType, capsys) -> None:
    assert loop.main(["decide", "190", "maybe", "owner"]) == 1
    assert "unknown disposition" in capsys.readouterr().err
    assert loop.read_gates() == []


def test_gaps_command_filters_by_disposition(loop: ModuleType, capsys) -> None:
    loop.record_pass("sha", "200,201")
    loop.decide("200", "accepted", "owner")

    loop.main(["gaps", "accepted"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("200\t")


# ------------------------------------------------------- tool-node robustness


def test_malformed_and_commented_rows_are_skipped_not_fatal(loop: ModuleType) -> None:
    loop.passes_file().write_text(
        loop.PASSES_HEADER + "not\ta\tvalid\trow\n\n1\t2026-08-02\tabc\tclean\t0\t-\tok\n"
    )
    assert len(loop.read_passes()) == 1
    assert loop.clean_streak() == 1


def test_main_never_raises_on_bad_input(loop: ModuleType, capsys) -> None:
    assert loop.main([]) == 0  # defaults to stats
    capsys.readouterr()
    assert loop.main(["nope"]) == 1
    assert "unknown command" in capsys.readouterr().err
    assert loop.main(["gate"]) == 1
    assert loop.main(["record-pass"]) == 1
    assert loop.main(["decide", "1"]) == 1


def test_main_never_raises_when_an_artifact_is_a_directory(
    loop: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocked = tmp_path / "as-a-dir"
    blocked.mkdir()
    monkeypatch.setenv(loop.PASSES_ENV, str(blocked))

    assert loop.main(["record-pass", "sha"]) == 1
    assert "parity_loop error" in capsys.readouterr().err


def test_fields_with_tabs_cannot_corrupt_the_tsv(loop: ModuleType) -> None:
    loop.record_pass("sha", "210", "2026-08-02", "note\twith\ttabs\nand a newline")
    rows = loop.read_passes()
    assert len(rows) == 1
    assert rows[0][6] == "note with tabs and a newline"


def test_cli_entrypoint_exit_codes(tmp_path: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "PARITY_PASSES_FILE": str(tmp_path / "p.tsv"),
        "PARITY_GATES_FILE": str(tmp_path / "g.tsv"),
    }
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env
    )

    assert run("record-pass", "sha", "220").returncode == 0
    assert run("gate", "220").returncode == 1
    assert run("decide", "220", "accepted", "owner").returncode == 0

    proceed = run("gate", "220")
    assert proceed.returncode == 0
    assert proceed.stdout.strip().splitlines()[-1].startswith("PROCEED")


# --------------------------------------------- the artifacts shipped in-repo


def test_shipped_artifacts_are_well_formed() -> None:
    module = _load()  # no env override: read the repo's own artifacts
    passes = module.read_passes()
    gates = module.read_gates()

    assert passes, "the pass record ships with the baseline audit pass"
    assert [row[0] for row in passes] == [str(n + 1) for n in range(len(passes))]
    assert all(row[2] in module.DISPOSITIONS for row in gates)

    recorded = {gap_id for row in passes for gap_id, _ in module.parse_gap_ids(row[5])}
    gated = {row[0] for row in gates}
    assert recorded <= gated, "every discovered gap must carry a gate row"
