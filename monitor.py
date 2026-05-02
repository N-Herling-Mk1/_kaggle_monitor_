#!/usr/bin/env python3
"""
HELIX Kaggle Leaderboard Monitor
================================

Polls a Kaggle competition leaderboard, diffs against a committed snapshot,
and emails on ANY change in the displayed board (membership, score, ordering,
or submission timestamp). A backstop heartbeat at HEARTBEAT_HOURS distinguishes
silence from outage.

Required env (from GitHub Secrets):
    KAGGLE_USERNAME, KAGGLE_KEY    Kaggle CLI auth
    RESEND_API_KEY                  Resend transactional email API key
    NOTIFY_TO                       recipient address
    COMP                            competition slug (e.g. ling-539-competition-2026)
    MY_TEAM                         substring of your team name (case-insensitive)

Optional env:
    NOTIFY_FROM         default: 'HELIX <onboarding@resend.dev>'
    HEARTBEAT_HOURS     default: 4.0
    FORCE_EMAIL         default: false; if 'true', bypasses change check
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests


# =====================================================================
# config
# =====================================================================
ROOT          = Path(__file__).resolve().parent
TMP_DIR       = ROOT / ".tmp"
STATE_PATH    = ROOT / "state.json"
SNAPSHOT_DIR  = ROOT / "snapshots"

COMP            = os.environ.get("COMP", "").strip()
MY_TEAM         = os.environ.get("MY_TEAM", "").strip()
KAGGLE_USERNAME = os.environ.get("KAGGLE_USERNAME", "").strip()
KAGGLE_KEY      = os.environ.get("KAGGLE_KEY", "").strip()
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "").strip()
NOTIFY_TO       = os.environ.get("NOTIFY_TO", "").strip()
NOTIFY_FROM     = os.environ.get("NOTIFY_FROM", "HELIX <onboarding@resend.dev>").strip()
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "4.0"))
FORCE_EMAIL     = os.environ.get("FORCE_EMAIL", "false").strip().lower() == "true"

NOW = datetime.now(timezone.utc)


# =====================================================================
# utilities
# =====================================================================
def banner(text: str) -> None:
    print("\u2500" * 60)
    print(f"  {text}")
    print("\u2500" * 60)


# =====================================================================
# preflight
# =====================================================================
def preflight() -> None:
    banner("PREFLIGHT")
    print(f"python          = {sys.version.split()[0]}")

    kaggle_bin = shutil.which("kaggle")
    print(f"kaggle CLI      = {kaggle_bin or 'NOT FOUND'}")
    if kaggle_bin:
        try:
            r = subprocess.run(
                ["kaggle", "--version"],
                capture_output=True, text=True, timeout=15,
            )
            print(f"kaggle version  = {r.stdout.strip() or r.stderr.strip()}")
        except Exception as e:
            print(f"kaggle version  = error: {e}")

    def s(v): return "set" if v else "MISSING"

    print(f"COMP            = {COMP or 'MISSING'}")
    print(f"MY_TEAM         = {s(MY_TEAM)}")
    print(f"KAGGLE_USERNAME = {s(KAGGLE_USERNAME)}")
    print(f"KAGGLE_KEY      = {s(KAGGLE_KEY)}")
    print(f"RESEND_API_KEY  = {s(RESEND_API_KEY)}")
    print(f"NOTIFY_TO       = {s(NOTIFY_TO)}")
    print(f"NOTIFY_FROM     = {NOTIFY_FROM}")
    print(f"FORCE_EMAIL     = {FORCE_EMAIL}")
    print(f"heartbeat       = {HEARTBEAT_HOURS:.1f}h")
    print(f"[poll] {NOW.isoformat()}  competition={COMP}")

    missing = [n for n, v in [
        ("COMP", COMP), ("MY_TEAM", MY_TEAM),
        ("KAGGLE_USERNAME", KAGGLE_USERNAME), ("KAGGLE_KEY", KAGGLE_KEY),
        ("RESEND_API_KEY", RESEND_API_KEY), ("NOTIFY_TO", NOTIFY_TO),
    ] if not v]
    if missing:
        print(f"FATAL: missing required env: {missing}")
        sys.exit(1)


# =====================================================================
# fetch leaderboard
# =====================================================================
def fetch_leaderboard() -> list[dict]:
    banner("FETCH LEADERBOARD")

    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    cmd = ["kaggle", "competitions", "leaderboard", COMP, "-d", "-p", str(TMP_DIR)]
    print(f"$ {' '.join(cmd)}")

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.stdout:
        print("[kaggle stdout]")
        print(r.stdout.strip())
    if r.stderr:
        print("[kaggle stderr]")
        print(r.stderr.strip())
    if r.returncode != 0:
        print(f"FATAL: kaggle CLI exited {r.returncode}")
        sys.exit(1)

    # Kaggle CLI delivers a .zip; extract it in place.
    for z in list(TMP_DIR.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(TMP_DIR)

    csvs = list(TMP_DIR.glob("*.csv"))
    if not csvs:
        print("FATAL: no CSV found after extraction")
        sys.exit(1)
    csv_path = csvs[0]
    print(f"csv             = {csv_path.name}")

    rows = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "team_id":   r.get("TeamId", "").strip(),
                "team_name": r.get("TeamName", "").strip(),
                "score":     r.get("Score", "").strip(),
                "submitted": r.get("SubmissionDate", "").strip(),
            })
    print(f"parsed rows     = {len(rows)}")
    return rows


# =====================================================================
# diff and decision
# =====================================================================
def board_signature(rows: list[dict]) -> list[list[str]]:
    """Captures every field whose change should trigger an alert.

    Membership change   -> set of team_ids differs.
    Score change        -> any (team_id, score) pair differs.
    Submission update   -> any (team_id, submitted) pair differs (catches
                            new submissions even when score is unchanged or
                            the team's relative rank is unchanged).
    Ordering change     -> implied by any of the above; rank order is
                            preserved by storing the list in board order.
    """
    return [[r["team_id"], r["score"], r["submitted"]] for r in rows]


def find_me(rows: list[dict]) -> tuple[int | None, dict | None]:
    needle = MY_TEAM.lower()
    for i, r in enumerate(rows):
        if needle and needle in r["team_name"].lower():
            return i + 1, r
    return None, None


def hours_since(iso_str: str | None) -> float:
    if not iso_str:
        return float("inf")
    try:
        prev = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        return (NOW - prev).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def diff_and_decide(rows: list[dict], prev: dict) -> dict:
    banner("DIFF + DECISION")

    my_rank, me = find_me(rows)
    if me is None:
        masked = "***" if MY_TEAM else "(empty)"
        print(f"WARNING: team fragment '{masked}' not found on board")

    current_sig = board_signature(rows)
    prev_sig    = prev.get("board_sig", []) if prev else []

    first_run = not prev_sig
    changed   = (not first_run) and (current_sig != prev_sig)

    diff_summary: list[str] = []
    if changed:
        prev_map = {row[0]: (row[1], row[2]) for row in prev_sig}
        cur_map  = {row[0]: (row[1], row[2]) for row in current_sig}
        added    = sorted(set(cur_map)  - set(prev_map))
        removed  = sorted(set(prev_map) - set(cur_map))
        moved    = sorted(tid for tid in (set(cur_map) & set(prev_map))
                          if cur_map[tid] != prev_map[tid])
        print(f"[diff] added={len(added)} removed={len(removed)} moved={len(moved)}")
        name_of = {r["team_id"]: r["team_name"] for r in rows}
        for tid in moved:
            old_s, _ = prev_map[tid]
            new_s, _ = cur_map[tid]
            line = f"  {name_of.get(tid, tid)}: {old_s} -> {new_s}"
            diff_summary.append(line)
            print(f"[diff] {line.strip()}")
        for tid in added:
            new_s, _ = cur_map[tid]
            line = f"  + {name_of.get(tid, tid)} ({new_s}) [new]"
            diff_summary.append(line)
            print(f"[diff] {line.strip()}")
        for tid in removed:
            line = f"  - team_id={tid} (score was {prev_map[tid][0]})"
            diff_summary.append(line)
            print(f"[diff] {line.strip()}")

    h_since = hours_since(prev.get("last_email") if prev else None)
    heartbeat_due = h_since >= HEARTBEAT_HOURS

    if FORCE_EMAIL:
        state, reason, send = "FORCED", "FORCE_EMAIL=true", True
    elif first_run:
        state, reason, send = "FIRST RUN (silent)", "no prior state — establishing baseline", False
    elif changed:
        state, reason, send = "CHANGE", f"{len(diff_summary)} update(s) on board", True
    elif heartbeat_due:
        state, reason, send = "HEARTBEAT", f"{h_since:.1f}h >= {HEARTBEAT_HOURS:.1f}h threshold", True
    else:
        state, reason, send = ("NO CHANGE (silent)",
                               f"{h_since:.1f}h since last email < {HEARTBEAT_HOURS:.1f}h threshold",
                               False)

    print(f"state           = {state}")
    print(f"reason          = {reason}")
    print(f"send email      = {send}")

    return {
        "my_rank":      my_rank,
        "my_score":     me["score"]      if me else None,
        "my_team":      me["team_name"]  if me else None,
        "send":         send,
        "state":        state,
        "reason":       reason,
        "diff_summary": diff_summary,
        "current_sig":  current_sig,
        "rows":         rows,
    }


# =====================================================================
# email
# =====================================================================
def send_email(d: dict) -> bool:
    rank      = d["my_rank"]
    score     = d["my_score"]
    rank_str  = f"#{rank}"  if rank  is not None else "?"
    score_str = score        if score is not None else "?"
    state     = d["state"].lower()

    subject = f"HELIX :: {state} (you: {rank_str}, score={score_str})"

    L = []
    L.append(f"Time:        {NOW.isoformat()}")
    L.append(f"Competition: {COMP}")
    L.append(f"Trigger:     {d['state']}")
    L.append(f"Reason:      {d['reason']}")
    L.append("")
    L.append("=" * 56)
    L.append("YOU")
    L.append("=" * 56)
    L.append(f"team:  {d.get('my_team') or '(not found on board)'}")
    L.append(f"rank:  {rank_str}")
    L.append(f"score: {score_str}")
    L.append("")

    if d["diff_summary"]:
        L.append("=" * 56)
        L.append("CHANGES SINCE LAST POLL")
        L.append("=" * 56)
        L.extend(d["diff_summary"])
        L.append("")

    L.append("=" * 56)
    L.append("CURRENT BOARD (top 25)")
    L.append("=" * 56)
    L.append(f"{'#':>3}  {'Score':>9}  {'Submitted':<19}  Team")
    for i, r in enumerate(d["rows"][:25], 1):
        marker = "  <-- you" if rank == i else ""
        L.append(f"{i:>3}  {r['score']:>9}  {r['submitted']:<19}  {r['team_name']}{marker}")

    body = "\n".join(L)

    payload = {
        "from":    NOTIFY_FROM,
        "to":      [NOTIFY_TO],
        "subject": subject,
        "text":    body,
    }

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        print(f"[email] resend response: {r.status_code} {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[email] EXCEPTION: {e}")
        return False


# =====================================================================
# persistence
# =====================================================================
def write_snapshot(rows: list[dict], my_rank, my_score) -> None:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    fname = NOW.strftime("%Y-%m-%dT%H-%M-%S+00-00") + ".json"
    snap = {
        "at":         NOW.isoformat(),
        "team_count": len(rows),
        "my_rank":    my_rank,
        "my_score":   my_score,
        "top_25":     rows[:25],
    }
    (SNAPSHOT_DIR / fname).write_text(json.dumps(snap, indent=2))


def write_state(d: dict, alerted: bool) -> None:
    prev = load_state()
    last_email = prev.get("last_email") if prev else None
    if alerted:
        last_email = NOW.isoformat()
    state = {
        "at":         NOW.isoformat(),
        "my_rank":    d["my_rank"],
        "my_score":   d["my_score"],
        "team_ids":   [row[0] for row in d["current_sig"]],
        "board_sig":  d["current_sig"],
        "last_email": last_email,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


# =====================================================================
# main
# =====================================================================
def main() -> None:
    preflight()
    rows = fetch_leaderboard()
    prev = load_state()
    decision = diff_and_decide(rows, prev)

    write_snapshot(rows, decision["my_rank"], decision["my_score"])

    alerted = False
    if decision["send"]:
        alerted = send_email(decision)

    write_state(decision, alerted)

    banner("DONE")
    state_lower = decision["state"].lower()
    if FORCE_EMAIL:
        trig = "forced"
    elif "change" in state_lower:
        trig = "change"
    elif "heartbeat" in state_lower:
        trig = "heartbeat"
    elif "first" in state_lower:
        trig = "first_run"
    else:
        trig = "silent"
    print(f"rank={decision['my_rank']}  score={decision['my_score']}  "
          f"trigger={trig}  alerted={alerted}")


if __name__ == "__main__":
    main()
