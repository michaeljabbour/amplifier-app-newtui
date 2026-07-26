#!/usr/bin/env python3
"""Comparative TUI startup/memory benchmark for the amplifier-app-newtui Rust migration.

Drives real terminal sessions through the forge PTY daemon (stdlib only; shells
out to forge.py). For each candidate it measures:

  startup_ms : wall clock from just before the launch command is typed into the
               PTY (t0) until the first poll of the rendered screen that matches
               the candidate's "ready" regex (t1). Screen polls run every
               POLL_S seconds plus ~50-150 ms of forge subprocess overhead per
               poll, so values carry roughly +-(POLL_S*1000 + 150) ms of
               one-sided granularity (they can only overshoot, never undershoot).
  rss_mb     : after ready, `ps -axo pid,ppid,rss` summed over every descendant
               of the session's zsh (the app plus all children: serve_mock.py,
               `uv run ... serve`, node children, etc.). The zsh shell itself
               (~2 MB) is excluded.
  turn_ms    : demo candidates only. Wall clock from just before the prompt is
               typed (t2) until a new `+N/-M` diffstat rule appears in the
               transcript (t3). The scripted demo turn may require one approval
               ("Allow once"); the driver confirms it with Enter and records
               whether an approval was seen.

Every run is cold: a fresh zsh PTY session per run, app closed and session
destroyed afterwards. Raw records append to results.jsonl; a rendered screen
snapshot per run is saved under screens/.

Live/paid candidates (rust-live, py-live, amplifier-cli, codex, claude) are
boot-only: NO prompt is ever submitted; the app is quit right after the ready
screenshot. Failures are recorded honestly with the error screen attached.

Usage:
  python3 bench.py                 # run everything
  python3 bench.py rust-demo py-demo   # run selected candidates
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

FORGE = os.environ.get(
    "FORGE",
    "/Users/michaeljabbour/.claude/skills/amplifier-skill-forge/tools/forge.py",
)
REPO = "/Users/michaeljabbour/dev/amplifier-app-newtui"
RUST = os.path.join(REPO, "rust-mvp")
PERF = os.path.join(RUST, "perf")
SCREENS = os.path.join(PERF, "screens")
RESULTS = os.path.join(PERF, "results.jsonl")

POLL_S = 0.25          # target interval between screen polls
TAG = "perfbench"
DIFFSTAT = re.compile(r"\+\d+/[−-]\d+")  # turn-complete rule, e.g. "+18/−0"
APPROVAL = re.compile(r"Allow once")

TURN_PROMPT = "Add a health check endpoint"


# ---------------------------------------------------------------- forge glue

def forge(*args, timeout=60):
    return subprocess.run(
        ["python3", FORGE, *args], capture_output=True, text=True, timeout=timeout
    )


def new_session(name, cwd):
    p = forge("new", "--name", name, "--cwd", cwd, "--tag", TAG)
    sid = p.stdout.strip().strip('"')
    if p.returncode != 0 or not sid:
        raise RuntimeError(f"forge new failed: {p.stdout} {p.stderr}")
    return sid


def screen(sid):
    return forge("screen", sid).stdout


def close(sid):
    forge("close", sid)


def session_pid(sid):
    p = forge("list")
    try:
        for s in json.loads(p.stdout):
            if s["id"] == sid:
                return s["pid"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


# ------------------------------------------------------------- measurements

def poll_until(sid, regex, timeout_s):
    """Poll the rendered screen until regex matches. Returns (t_match|None, last_screen)."""
    pat = re.compile(regex)
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        last = screen(sid)
        if pat.search(last):
            return time.time(), last
        time.sleep(POLL_S)
    return None, last


def tree_rss(root_pid):
    """Sum RSS (KB) over all descendants of root_pid (root zsh excluded)."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True
    ).stdout
    procs, kids = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        procs[pid] = (rss, parts[3] if len(parts) > 3 else "")
        kids.setdefault(ppid, []).append(pid)
    total, plist, stack = 0, [], list(kids.get(root_pid, []))
    while stack:
        p = stack.pop()
        if p in procs:
            total += procs[p][0]
            plist.append({"pid": p, "rss_kb": procs[p][0], "cmd": procs[p][1][:120]})
        stack.extend(kids.get(p, []))
    return total, plist


def snapshot(sid, name):
    path = os.path.join(SCREENS, name + ".txt")
    with open(path, "w") as f:
        f.write(screen(sid))
    return path


def perf_log_events(path, offset):
    """Parse rust AMPLIFIER_PERF_LOG lines appended after byte `offset`."""
    events = {}
    try:
        with open(path) as f:
            f.seek(offset)
            for line in f:
                try:
                    rec = json.loads(line)
                    events[rec["event"]] = rec["ms"]
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        pass
    return events


def file_size(path):
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


# ------------------------------------------------------------- driver core

def run_candidate(cand, run_idx):
    name = f"{cand['name']}-run{run_idx}"
    rec = {
        "candidate": cand["name"],
        "run": run_idx,
        "ts": datetime.now(timezone.utc).isoformat(),
        "cmd": cand["cmd"],
        "cwd": cand["cwd"],
        "ready_regex": cand["ready"],
        "poll_interval_s": POLL_S,
        "ok": False,
    }
    plog = cand.get("perf_log")
    plog_offset = file_size(plog) if plog else 0

    sid = new_session(name, cand["cwd"])
    rec["forge_session"] = sid
    try:
        time.sleep(2)  # let zsh settle
        t0 = time.time()
        forge("run", sid, cand["cmd"], "--wait", "0")
        t1, last = poll_until(sid, cand["ready"], cand.get("ready_timeout_s", 120))
        if t1 is None:
            rec["error"] = "ready marker never matched"
            rec["screen"] = snapshot(sid, name + "-FAILED")
            return rec
        rec["startup_ms"] = round((t1 - t0) * 1000, 1)

        # optional second gate on the rendered screen (e.g. py-live: session
        # identity in the footer proves the runtime finished booting)
        if cand.get("gate_regex"):
            tg, _ = poll_until(sid, cand["gate_regex"], cand.get("gate_timeout_s", 240))
            if tg is not None:
                rec["gate_ms"] = round((tg - t0) * 1000, 1)
                rec["gate_regex"] = cand["gate_regex"]
            else:
                rec["gate_error"] = f"gate regex {cand['gate_regex']!r} never matched"

        # extra gate (e.g. rust-live: session identity from perf log)
        if cand.get("ready_gate") == "session_started" and plog:
            gate_deadline = time.time() + cand.get("gate_timeout_s", 240)
            while time.time() < gate_deadline:
                if "session_started" in perf_log_events(plog, plog_offset):
                    break
                time.sleep(1)
            rec["session_started_seen"] = (
                "session_started" in perf_log_events(plog, plog_offset)
            )

        root = session_pid(sid)
        if root:
            rss_kb, plist = tree_rss(root)
            rec["rss_mb"] = round(rss_kb / 1024, 1)
            rec["processes"] = plist
        rec["screen"] = snapshot(sid, name)

        if cand.get("turn"):
            base = len(DIFFSTAT.findall(screen(sid)))
            t2 = time.time()
            forge("type", sid, TURN_PROMPT)
            forge("key", sid, "enter")
            approved = False
            t3 = None
            deadline = time.time() + cand.get("turn_timeout_s", 120)
            while time.time() < deadline:
                s = screen(sid)
                if len(DIFFSTAT.findall(s)) > base:
                    t3 = time.time()
                    break
                if not approved and APPROVAL.search(s):
                    forge("key", sid, "enter")
                    approved = True
                time.sleep(POLL_S)
            rec["turn_approval_seen"] = approved
            if t3 is not None:
                rec["turn_ms"] = round((t3 - t2) * 1000, 1)
            else:
                rec["turn_error"] = "diffstat rule never appeared"
            rec["screen_after_turn"] = snapshot(sid, name + "-turn")

        if plog:
            rec["internal_perf"] = perf_log_events(plog, plog_offset)

        rec["ok"] = "startup_ms" in rec and "turn_error" not in rec
        return rec
    finally:
        for key in cand.get("quit_keys", ["ctrl+c"]):
            forge("key", sid, key)
            time.sleep(0.6)
        close(sid)


CANDIDATES = [
    {
        "name": "rust-demo",
        "cwd": RUST,
        "cmd": (
            f"AMPLIFIER_PERF_LOG={PERF}/rust-demo.perf.jsonl "
            "./target/release/amplifier-newtui-rs --demo"
        ),
        "ready": r"Message",
        "runs": 3,
        "turn": True,
        "perf_log": f"{PERF}/rust-demo.perf.jsonl",
        "quit_keys": ["ctrl+c"],
    },
    {
        "name": "rust-mock",
        "cwd": RUST,
        "cmd": (
            f"AMPLIFIER_PERF_LOG={PERF}/rust-mock.perf.jsonl "
            "./target/release/amplifier-newtui-rs"
        ),
        "ready": r"Message",
        "runs": 3,
        "perf_log": f"{PERF}/rust-mock.perf.jsonl",
        "quit_keys": ["ctrl+c"],
    },
    {
        "name": "py-demo",
        "cwd": REPO,
        "cmd": "uv run amplifier-newtui --demo",
        "ready": r"Message",
        "runs": 3,
        "turn": True,
        "quit_keys": ["ctrl+c"],
    },
    {
        "name": "rust-live",
        "cwd": RUST,
        "cmd": (
            f'AMPLIFIER_SERVE_CMD="uv run amplifier-newtui serve" '
            f"AMPLIFIER_PERF_LOG={PERF}/rust-live.perf.jsonl "
            "./target/release/amplifier-newtui-rs"
        ),
        "ready": r"Message",
        "ready_timeout_s": 240,
        "ready_gate": "session_started",
        "gate_timeout_s": 240,
        "runs": 1,
        "perf_log": f"{PERF}/rust-live.perf.jsonl",
        "quit_keys": ["ctrl+c"],
    },
    {
        "name": "py-live",
        "cwd": REPO,
        "cmd": "uv run amplifier-newtui",
        "ready": r"Message",
        "ready_timeout_s": 240,
        # composer draws before the runtime is up; session identity (model name
        # in the footer) is the honest "boot complete" marker
        "gate_regex": r"fable",
        "gate_timeout_s": 240,
        "runs": 1,
        "quit_keys": ["ctrl+c", "ctrl+q"],
    },
    {
        "name": "amplifier-cli",
        "cwd": REPO,
        "cmd": "amplifier",
        "ready": r"Interactive",
        "ready_timeout_s": 240,
        "runs": 1,
        "quit_keys": ["ctrl+c", "ctrl+c"],
    },
    {
        "name": "codex",
        "cwd": REPO,
        "cmd": "codex",
        # first interactive screen: either the full UI banner or an
        # update/trust gate (recorded as time-to-gate, see PERFORMANCE.md)
        "ready": r"OpenAI Codex|permissions:|Update available",
        "ready_timeout_s": 240,
        "runs": 1,
        "quit_keys": ["ctrl+c", "ctrl+c"],
    },
    {
        "name": "claude",
        "cwd": REPO,
        "cmd": "claude",
        "ready": r"shortcuts|bypass|Welcome to Claude",
        "ready_timeout_s": 240,
        "runs": 1,
        "quit_keys": ["ctrl+c", "ctrl+c"],
    },
]


def main():
    os.makedirs(SCREENS, exist_ok=True)
    selected = sys.argv[1:]
    for cand in CANDIDATES:
        if selected and cand["name"] not in selected:
            continue
        for i in range(1, cand["runs"] + 1):
            print(f"=== {cand['name']} run {i}/{cand['runs']}", flush=True)
            try:
                rec = run_candidate(cand, i)
            except Exception as e:  # record, keep going
                rec = {
                    "candidate": cand["name"],
                    "run": i,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "ok": False,
                    "error": f"driver exception: {e}",
                }
            with open(RESULTS, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(json.dumps({k: v for k, v in rec.items() if k != "processes"},
                             indent=2), flush=True)


if __name__ == "__main__":
    main()
