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
LEDGER_TOOL = REPO_ROOT / "pipelines" / "ledger.py"
GENE_TRANSFER_DOT = REPO_ROOT / "pipelines" / "gene-transfer.dot"


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
    loop.decide("120", "rejected", "mjabbour", "belongs below the harness")

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


@pytest.mark.parametrize("owner", ["", "-", "TBD", "team", "owner", "unknown", "?"])
def test_placeholder_owner_cannot_end_run_at_write_time(loop: ModuleType, owner: str) -> None:
    loop.record_pass("s1", "150")
    before = loop.passes_file().read_bytes()

    assert loop.end_run(owner, "forged stop") is None
    assert loop.passes_file().read_bytes() == before
    assert loop.run_ended_by() == ""


def test_end_run_full_name_uses_unambiguous_note_format(loop: ModuleType, capsys) -> None:
    row = loop.end_run("Michael Jabbour", "remaining gaps are deferred")

    assert row is not None
    assert row[6] == "owner=Michael Jabbour | remaining gaps are deferred"
    assert loop._end_run_owner(row[6]) == "Michael Jabbour"
    assert loop.run_ended_by() == row[6]
    assert loop.main(["should-continue"]) == 0
    assert capsys.readouterr().out.startswith("DONE reason=owner-ended")


def test_legacy_single_token_owner_ended_note_remains_readable(loop: ModuleType) -> None:
    loop.passes_file().write_text(
        loop.PASSES_HEADER + "1\t2026-08-04\t-\towner-ended\t0\t-\towner=mjabbour legacy reason\n"
    )

    assert loop._end_run_owner(loop.read_passes()[0][6]) == "mjabbour"
    assert loop.run_ended_by() == "owner=mjabbour legacy reason"


def test_hand_edited_placeholder_owner_end_is_ignored_and_validation_flags_it(
    loop: ModuleType, capsys
) -> None:
    loop.passes_file().write_text(
        loop.PASSES_HEADER + "1\t2026-08-04\t-\towner-ended\t0\t-\towner=TBD | forged stop\n"
    )

    assert loop.run_ended_by() == ""
    assert loop.unattributed_end_rows() == [loop.read_passes()[0]]
    assert loop.main(["should-continue"]) == 0
    assert capsys.readouterr().out.strip() == "CONTINUE clean_streak=0/3"

    assert loop.main(["validate"]) == 1
    output = capsys.readouterr().out.strip().splitlines()
    assert output[0].startswith("pass=1\towner-ended\towner='TBD'")
    assert output[-1] == "INVALID gates=0 unattributed=0 owner_ends_unattributed=1"


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
    loop.decide("160", "accepted", "mjabbour")
    loop.main(["gate", "160"])
    assert ledger.read_bytes() == before

    # Repeated code-changing attempts on one gap leave the run-level streak
    # untouched; only a recorded read-only pass can move it.
    loop.record_pass("sha", "-")
    assert loop.clean_streak() == 1
    for _ in range(5):
        loop.decide("160", "accepted", "mjabbour", "retry attempt")
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
    loop.decide("170", disposition, "mjabbour", "decision recorded")

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
    assert loop.main(["decide", "190", "maybe", "mjabbour"]) == 1
    assert "unknown disposition" in capsys.readouterr().err
    assert loop.read_gates() == []


def test_gaps_command_filters_by_disposition(loop: ModuleType, capsys) -> None:
    loop.record_pass("sha", "200,201")
    loop.decide("200", "accepted", "mjabbour")

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
    assert run("decide", "220", "accepted", "mjabbour").returncode == 0

    proceed = run("gate", "220")
    assert proceed.returncode == 0
    assert proceed.stdout.strip().splitlines()[-1].startswith("PROCEED")


def test_cli_entrypoint_refuses_placeholder_end_run_and_accepts_full_name(
    tmp_path: Path,
) -> None:
    passes = tmp_path / "p.tsv"
    env = {
        "PATH": "/usr/bin:/bin",
        "PARITY_PASSES_FILE": str(passes),
        "PARITY_GATES_FILE": str(tmp_path / "g.tsv"),
    }
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env
    )

    refused = run("end-run", "team", "ship it")
    assert refused.returncode == 1
    assert "placeholder owner refused" in refused.stderr
    assert not passes.exists(), "a refused end-run must not write the pass artifact"

    accepted = run("end-run", "Michael Jabbour", "remaining gaps deferred")
    assert accepted.returncode == 0
    assert "owner=Michael Jabbour | remaining gaps deferred" in accepted.stdout
    assert run("should-continue").stdout.startswith("DONE reason=owner-ended")


# ---------------------------------------- gene-transfer boundary recheck


def _ledger_runner(loop: ModuleType, tmp_path: Path):
    env = {
        "PATH": "/usr/bin:/bin",
        "LEDGER_FILE": str(tmp_path / "ledger.tsv"),
        "LEDGER_SOURCES_FILE": str(tmp_path / "ledger-sources.tsv"),
        "PARITY_PASSES_FILE": str(loop.passes_file()),
        "PARITY_GATES_FILE": str(loop.gates_file()),
    }
    return lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(LEDGER_TOOL), *args],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    ("effective", "owner"),
    [
        ("pending", "-"),
        ("rejected", "Michael Jabbour"),
        ("deferred", "Michael Jabbour"),
        ("already-covered", "Michael Jabbour"),
        ("unattributed", "TBD"),
        ("unknown-state", "Michael Jabbour"),
    ],
)
def test_gene_transfer_boundary_blocks_nonaccepted_parity_states(
    loop: ModuleType, tmp_path: Path, effective: str, owner: str
) -> None:
    run = _ledger_runner(loop, tmp_path)
    assert run("add", "700", "direct-enqueue").returncode == 0
    stored = "accepted" if effective == "unattributed" else effective
    loop.gates_file().write_text(
        loop.GATES_HEADER + f"700\tdirect-enqueue\t{stored}\t{owner}\t2026-08-05\ttest row\n"
    )

    selected = run("earliest-transferable")
    assert selected.returncode == 1
    assert selected.stdout.strip() == (f"BLOCKED issue=700 source=parity disposition={effective}")
    rechecked = run("gate-transfer", "700")
    assert rechecked.returncode == 1
    assert rechecked.stdout.startswith("BLOCKED issue=700")


def test_gene_transfer_boundary_blocks_direct_unknown_ledger_row(
    loop: ModuleType, tmp_path: Path
) -> None:
    run = _ledger_runner(loop, tmp_path)
    (tmp_path / "ledger.tsv").write_text("701\thand-edited\tnew\n")

    selected = run("earliest-transferable")
    assert selected.returncode == 1
    assert selected.stdout.strip() == ("BLOCKED issue=701 source=unknown disposition=undecided")


def test_gene_transfer_boundary_rechecks_accepted_before_code_changes(
    loop: ModuleType, tmp_path: Path
) -> None:
    run = _ledger_runner(loop, tmp_path)
    assert run("add", "702", "accepted-gap").returncode == 0
    loop.register_gap("702", "accepted-gap")
    assert loop.decide("702", "accepted", "Michael Jabbour", "build it") is not None

    assert run("earliest-transferable").stdout.strip() == "702 accepted-gap"
    proceed = run("gate-transfer", "702")
    assert proceed.returncode == 0
    assert proceed.stdout.strip() == ("PROCEED issue=702 source=parity disposition=accepted")

    # The deterministic node checks again after selection; a changed decision
    # cannot race through to BranchSetup.
    assert loop.decide("702", "rejected", "Michael Jabbour", "changed call") is not None
    blocked = run("gate-transfer", "702")
    assert blocked.returncode == 1
    assert blocked.stdout.strip().endswith("disposition=rejected")


def test_explicit_non_parity_lane_is_deliberate_and_transferable(
    loop: ModuleType, tmp_path: Path
) -> None:
    run = _ledger_runner(loop, tmp_path)
    added = run("add-non-parity", "703", "authorized-backlog")

    assert added.returncode == 0
    assert added.stdout.strip() == "added 703 source=non-parity"
    assert run("earliest-transferable").stdout.strip() == "703 authorized-backlog"
    proceed = run("gate-transfer", "703")
    assert proceed.returncode == 0
    assert "source=non-parity disposition=not-applicable" in proceed.stdout


def test_non_parity_marker_cannot_override_an_existing_parity_gate(
    loop: ModuleType, tmp_path: Path
) -> None:
    run = _ledger_runner(loop, tmp_path)
    loop.register_gap("704", "known-parity-gap")
    assert run("add-non-parity", "704", "known-parity-gap").returncode == 0

    blocked = run("gate-transfer", "704")
    assert blocked.returncode == 1
    assert blocked.stdout.strip() == ("BLOCKED issue=704 source=non-parity disposition=pending")


def test_gene_transfer_graph_routes_blocked_rows_away_from_code_changes() -> None:
    graph = GENE_TRANSFER_DOT.read_text()

    assert graph.count("ledger.py earliest-transferable") == 2
    assert "ledger.py gate-transfer" in graph
    assert "SelectIssue -> RecheckOwnerGate" in graph
    assert "RecheckOwnerGate -> BranchSetup" in graph
    assert "RecheckOwnerGate -> owner_gate_blocked" in graph
    assert "CheckLedger -> owner_gate_blocked" in graph


# ------------------------------------------------- placeholder owner rejection
#
# A disposition is a PRODUCT-OWNER decision. A decision nobody signed is not a
# decision, so an owner field that names no askable human must not be able to
# record one -- at write time OR by hand-editing the gate file.


@pytest.mark.parametrize(
    "owner",
    [
        "",  # empty
        "   ",  # whitespace only
        "\t",  # whitespace only, tab flavour
        "-",  # the TSV's own filler
        "?",
        "???",
        "TBD",
        "tbd",
        " Tbd ",
        "<TBD>",
        "@tbd",
        "tbd.",
        "TBA",
        "todo",
        "n/a",
        "N/A",
        "none",
        "unknown",
        "UNKNOWN",
        "unassigned",
        "nobody",
        "someone",
        "placeholder",
        "xxx",
        "owner",
        "Owner",
        "owners",
        "product owner",
        "product-owner",
        "PO",
        "team",
        "the team",
        "maintainer",
        "reviewer",
        "lead",
        "dev",
        "engineering",
        "admin",
        "human",
        "ai",
        "bot",
        "agent",
        "me",
        "self",
        "x",  # fewer than two letters names no one
        "1",
        "!!",
    ],
)
def test_these_owners_are_placeholders(loop: ModuleType, owner: str) -> None:
    assert loop.is_placeholder_owner(owner) is True


@pytest.mark.parametrize(
    "owner",
    ["mjabbour", "@mjabbour", "Michael Jabbour", "m.jabbour", "jd", "octocat"],
)
def test_these_owners_are_real(loop: ModuleType, owner: str) -> None:
    assert loop.is_placeholder_owner(owner) is False


def test_the_placeholder_list_has_exactly_one_home(loop: ModuleType) -> None:
    """Every enforcement point must consult PLACEHOLDER_OWNERS, not its own copy."""
    assert isinstance(loop.PLACEHOLDER_OWNERS, frozenset)
    assert loop.PLACEHOLDER_OWNERS == frozenset(loop.PLACEHOLDER_OWNERS)
    source = TOOL.read_text()
    assert source.count("PLACEHOLDER_OWNERS = frozenset(") == 1
    # `is_placeholder_owner` is the only reader; nothing re-implements the check.
    assert source.count("in PLACEHOLDER_OWNERS") == 1


def test_decide_refuses_a_disposition_attributed_to_a_placeholder_owner(
    loop: ModuleType,
) -> None:
    loop.record_pass("sha", "300:some-gap")

    for owner in ("", "-", "TBD", "owner", "team", "unknown", "?", "   "):
        assert loop.decide("300", "accepted", owner, "looks fine to me") is None

    row = next(r for r in loop.read_gates() if r[0] == "300")
    assert row[2] == "pending", "a refused decision must not be written at all"
    assert loop.disposition_of("300") == "pending"
    assert loop.may_proceed("300") is False


def test_pending_is_the_one_disposition_that_may_carry_no_owner(loop: ModuleType) -> None:
    """A gap discovered by a read-only pass has no owner yet -- and must not need one."""
    loop.record_pass("sha", "301")
    row = next(r for r in loop.read_gates() if r[0] == "301")
    assert row[3] == "-" and loop.is_placeholder_owner(row[3])
    assert loop.disposition_of("301") == "pending"

    assert loop.decide("301", "pending", "-", "still awaiting the owner") is not None


def test_a_real_owner_records_a_real_disposition(loop: ModuleType) -> None:
    loop.record_pass("sha", "302")
    row = loop.decide("302", "rejected", "mjabbour", "belongs below the harness")

    assert row is not None
    assert loop.disposition_of("302") == "rejected"
    assert loop.stored_disposition_of("302") == "rejected"


def test_a_hand_edited_placeholder_decision_reads_back_unattributed(
    loop: ModuleType, capsys
) -> None:
    """The load-bearing case: someone edits the TSV to open the gate themselves."""
    loop.gates_file().write_text(
        loop.GATES_HEADER
        + "310\tsmuggled\taccepted\tTBD\t2026-08-04\thand-edited past the tool\n"
        + "311\tsmuggled-blank\taccepted\t-\t2026-08-04\tno owner at all\n"
    )

    for gap_id in ("310", "311"):
        assert loop.stored_disposition_of(gap_id) == "accepted"
        assert loop.disposition_of(gap_id) == loop.UNATTRIBUTED
        assert loop.may_proceed(gap_id) is False
        assert loop.main(["gate", gap_id]) == 1
        assert capsys.readouterr().out.strip() == f"BLOCKED gap={gap_id} disposition=unattributed"


def test_validate_flags_unattributed_rows_and_exits_nonzero(loop: ModuleType, capsys) -> None:
    loop.gates_file().write_text(
        loop.GATES_HEADER
        + "320\tok\trejected\tmjabbour\t2026-08-04\tsigned\n"
        + "321\tsmuggled\taccepted\towner\t2026-08-04\trole name, not a person\n"
    )

    assert loop.unattributed_rows() == [
        ["321", "smuggled", "accepted", "owner", "2026-08-04", "role name, not a person"]
    ]
    assert loop.main(["validate"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "INVALID gates=2 unattributed=1 owner_ends_unattributed=0"
    assert out[0].startswith("321\t")


def test_validate_passes_a_gate_file_whose_decisions_are_all_signed(
    loop: ModuleType, capsys
) -> None:
    loop.record_pass("sha", "330,331")
    loop.decide("330", "accepted", "mjabbour", "wanted")

    assert loop.main(["validate"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "VALID gates=2 unattributed=0"


def test_awaiting_counts_pending_and_unattributed_alike(loop: ModuleType, capsys) -> None:
    loop.gates_file().write_text(
        loop.GATES_HEADER
        + "340\tdecided\tdeferred\tmjabbour\t2026-08-04\tafter 0.3\n"
        + "341\tstill-open\tpending\t-\t2026-08-04\tawaiting owner disposition\n"
        + "342\tsmuggled\taccepted\tTBD\t2026-08-04\tunsigned\n"
    )

    assert [row[0] for row in loop.awaiting_rows()] == ["341", "342"]
    loop.main(["awaiting"])
    assert capsys.readouterr().out.strip().splitlines()[-1] == "awaiting=2/3"


def test_cli_reports_the_refusal_and_writes_nothing(loop: ModuleType, capsys) -> None:
    loop.record_pass("sha", "350")

    assert loop.main(["decide", "350", "accepted", "TBD", "ship it"]) == 1
    err = capsys.readouterr().err
    assert "placeholder owner refused" in err
    assert "'TBD'" in err
    assert loop.disposition_of("350") == "pending"


def test_cli_entrypoint_refuses_placeholder_owner(tmp_path: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "PARITY_PASSES_FILE": str(tmp_path / "p.tsv"),
        "PARITY_GATES_FILE": str(tmp_path / "g.tsv"),
    }
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env
    )

    assert run("record-pass", "sha", "360").returncode == 0
    assert run("decide", "360", "accepted", "team").returncode == 1
    assert run("gate", "360").returncode == 1
    assert run("decide", "360", "accepted", "mjabbour").returncode == 0
    assert run("gate", "360").returncode == 0
    assert run("validate").stdout.strip().splitlines()[-1].startswith("VALID")


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


def test_shipped_gate_file_carries_no_unsigned_decision() -> None:
    """The repo's own artifact must satisfy the placeholder rule it enforces."""
    module = _load()  # no env override: read the repo's own artifacts
    assert module.unattributed_rows() == []
    assert module.main(["validate"]) == 0
