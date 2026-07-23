"""Exported-data tests for kernel/demo.py — mockup-verbatim strings,
deterministic token formulas, cost accumulation, lanes and evidence."""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel.demo import (
    DEMO_BANNER,
    DEMO_DEFERRED_DECISION,
    DEMO_EVIDENCE,
    DEMO_LANE_BY_NAME,
    DEMO_LANES,
    DEMO_SESSION_COST_START,
    DEMO_SESSION_ID,
    DEMO_TURN_BY_KEY,
    DEMO_TURNS,
    build_denied_spec,
    format_k_tokens,
    rule_label,
    store_turn_cost,
    tick_tokens,
)

# --------------------------------------------------------------------------
# Token / cost formulas
# --------------------------------------------------------------------------


def test_tick_tokens_deterministic_and_pinned() -> None:
    # Pinned draws guard against seed-derivation drift; the sequence is
    # stable because random.Random(str) hashing is version-stable.
    assert tick_tokens("build") == (608, 439, 557, 425, 415, 450, 463, 470, 636)
    assert tick_tokens("auto") == (411, 538, 606, 443, 416, 475, 455, 496, 541)
    assert tick_tokens("agents") == (900,) * 6
    assert tick_tokens("build") == tick_tokens("build")
    assert tick_tokens("build", 7) == tick_tokens("build")[:7]
    # Mockup formula bounds: 380 + floor(random() * 260).
    assert all(380 <= t <= 639 for t in tick_tokens("build") + tick_tokens("auto"))


def test_store_turn_cost_formula() -> None:
    assert store_turn_cost(9) == Decimal("0.13")  # 0.04 + 9 * 0.01
    assert store_turn_cost(7) == Decimal("0.11")


def test_label_helpers_match_mockup_formatting() -> None:
    assert format_k_tokens(5_400) == "5.4k"
    assert format_k_tokens(83_900) == "83.9k"
    assert (
        rule_label("6.1s", 83_900, 91, Decimal("0.17"), "answer")
        == "6.1s · 83.9k tok, 91% cached · $0.17 · answer"
    )
    assert (
        rule_label("6s", 5_400, None, Decimal("0.52"), "2 files · tests ✔ · 3 agents")
        == "6s · 5.4k tok · $0.52 · 2 files · tests ✔ · 3 agents"
    )


# --------------------------------------------------------------------------
# Turn specs
# --------------------------------------------------------------------------


def test_turn_order_and_checkpoint_ids() -> None:
    assert [t.key for t in DEMO_TURNS] == [
        "seed",
        "build",
        "auto",
        "plan",
        "brainstorm",
        "agents",
    ]
    assert [t.checkpoint_id for t in DEMO_TURNS] == ["t1", "t2", "t3", "t4", "t5", "t6"]
    assert [t.mode for t in DEMO_TURNS] == [
        "chat",
        "chat",
        "auto",
        "plan",
        "brainstorm",
        "build",
    ]


def test_rule_labels_verbatim() -> None:
    labels = {t.key: t.rule_label for t in DEMO_TURNS}
    assert labels["seed"] == "6.1s · 83.9k tok, 91% cached · $0.17 · answer"
    assert labels["build"] == "9s · 4.5k tok, 88% cached · $0.13 · 3 files · +142/−38 · tests ✔"
    assert labels["auto"] == "9s · 4.4k tok, 88% cached · $0.13 · 3 files · +142/−38 · tests ✔"
    assert labels["plan"] == "11s · 9.4k tok, 93% cached · $0.06 · answer · plan ready"
    assert labels["brainstorm"] == "8s · 4.1k tok · $0.03 · answer"
    assert labels["agents"] == "6s · 5.4k tok · $0.52 · 2 files · tests ✔ · 3 agents"


def test_checkpoint_labels_and_shipped_flags() -> None:
    by_key = DEMO_TURN_BY_KEY
    assert by_key["seed"].checkpoint_label == "repo explainer · answer"
    assert by_key["build"].checkpoint_label == "store refactor · shipped"
    assert by_key["auto"].checkpoint_label == "store refactor · shipped"
    assert by_key["plan"].checkpoint_label == "durable-history plan · answer"
    assert by_key["brainstorm"].checkpoint_label == "supervision ideas · answer"
    assert by_key["agents"].checkpoint_label == "DTU reality check · shipped"
    assert [t.shipped for t in DEMO_TURNS] == [False, True, True, False, False, True]


def test_costs_accumulate_like_the_mockup() -> None:
    # Mockup: this.cost starts at 0.57, then +0.13, +0.13, +0.06, +0.03, +0.52.
    assert DEMO_SESSION_COST_START == Decimal("0.57")
    running = DEMO_SESSION_COST_START
    for spec in DEMO_TURNS[1:]:
        running += spec.cost
        assert spec.cost_after == running
    assert DEMO_TURNS[-1].cost_after == Decimal("1.44")
    assert DEMO_TURNS[0].cost_after == DEMO_SESSION_COST_START  # seed pre-baked


def test_recaps_and_notices_verbatim() -> None:
    by_key = DEMO_TURN_BY_KEY
    assert by_key["build"].recap == "Goal: durable session store. Next: open PR against main."
    assert by_key["auto"].recap == (
        "Goal: durable session store. Next: answer the deferred push decision (ctrl-y)."
    )
    assert by_key["plan"].recap == ("Plan ready. shift+tab to build hands it over for execution.")
    assert by_key["brainstorm"].recap == "Converge with /plan when one of these sticks."
    assert by_key["build"].end_notice == "agents 1 done"
    assert by_key["auto"].end_notice is None
    assert by_key["plan"].end_notice == (
        "plan mode: read-only · plan handed to build on mode switch"
    )
    assert by_key["brainstorm"].end_notice is None
    assert by_key["agents"].end_notice == "agents 3 done · click a lane to inspect its transcript"


def test_build_denied_spec() -> None:
    denied = build_denied_spec()
    assert denied.rule_label == "7s · 3.4k tok, 88% cached · $0.11 · 3 files · +142/−38"
    assert denied.outcome == "3 files · +142/−38"  # no tests ✔
    assert denied.cost == Decimal("0.11")
    assert denied.duration_ms == 7_500
    assert denied.answer == (
        "Session store refactor is in: history behind one durable interface "
        "(tests skipped by your denial), branch pushed. Ready for review."
    )
    assert denied.shipped is True


# --------------------------------------------------------------------------
# Lanes (DEMO_LANES powers the lanes panel, live tree and focus transcripts)
# --------------------------------------------------------------------------


def test_lane_panel_lines_verbatim() -> None:
    assert [lane.panel_line for lane in DEMO_LANES] == [
        "  ◐ researcher · scanning provider docs · 41s    · ↓ 100.1k tokens · $0.09",
        "  ■ coder      · migrating store        · 2m 04s · ↓ 48.3k tokens  · $0.31",
        "  ✔ tester     · done · tests ✔         · 55s    · ↓ 3.2k tokens   · $0.07",
    ]
    assert [(lane.glyph, lane.color_token) for lane in DEMO_LANES] == [
        ("◐", "teal"),
        ("■", "fg"),
        ("✔", "dim"),
    ]


def test_lane_completion_times() -> None:
    # tree_spawn/tree_done retired with the transcript tree lines — the
    # delegate summary + lanes panel carry this data now.
    assert [(lane.name, lane.done_at_ms) for lane in DEMO_LANES] == [
        ("researcher", 4_400),
        ("coder", 6_000),
        ("tester", 2_600),
    ]


def test_lane_focus_transcript_data() -> None:
    researcher = DEMO_LANE_BY_NAME["researcher"]
    assert researcher.brief == (
        "Scan the provider docs and list every capability the runtime does not exercise."
    )
    assert researcher.state_recap == "running · 41s · $0.09"
    assert [(row.kind, row.text) for row in researcher.log] == [
        (
            "narration",
            "Fetching the provider capability matrix and diffing it against runtime calls.",
        ),
        ("tool", "Ran 3 web_fetch calls"),
        ("command", 'grep -rn "capabilities" providers/ | head -20'),
        ("narration", "Two undocumented streaming flags found; verifying against the SDK."),
    ]
    tester = DEMO_LANE_BY_NAME["tester"]
    assert tester.state_recap == "completed · 55s · $0.07 · tests ✔"
    assert tester.log[-1].kind == "answer"
    assert tester.log[-1].text.startswith("All 41 store tests pass.")
    coder = DEMO_LANE_BY_NAME["coder"]
    assert coder.state_recap == "running · 2m 04s · $0.31"
    assert [row.kind for row in coder.log] == ["narration", "command", "tool", "narration"]
    # Hierarchical sub-session ids route lanes by session_id/parent_id.
    for lane in DEMO_LANES:
        assert lane.sub_session_id.startswith(f"{DEMO_SESSION_ID}-")
        assert lane.sub_session_id.endswith(f"_{lane.name}")


# --------------------------------------------------------------------------
# Evidence, banner, deferred decision
# --------------------------------------------------------------------------


def test_evidence_claims_verbatim() -> None:
    assert [(c.quote, c.source) for c in DEMO_EVIDENCE] == [
        ("dashboard and steering wheel", "Ran 2 shell commands (pyproject entry points)"),
        ("loads bundles", "grep amplifier_core bundle loader"),
    ]


def test_banner_verbatim() -> None:
    assert DEMO_BANNER == (
        "Amplifier 2026.07.13-87b93ef* · core 1.6.0",
        "Bundle: anchors | Provider: OpenAI | gpt-5.5 · session e07de0",
    )


def test_deferred_decision_verbatim() -> None:
    assert DEMO_DEFERRED_DECISION.text == (
        "Push branch to origin was blocked (outside trust boundary). "
        "Push to fork mj/waypoint instead?"
    )
    assert DEMO_DEFERRED_DECISION.chip_label == "yes · push to fork"
    assert DEMO_DEFERRED_DECISION.applied_narration == (
        "Applying decision: pushing to fork mj/waypoint. Trust-slot suggestion queued for /improve."
    )
