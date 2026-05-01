#!/usr/bin/env python3
"""
HELIX :: Kaggle leaderboard polling worker for GitHub Actions.

  Email policy:
    cold-start (first run, no prev state)   -> EMAIL
    changes detected                         -> EMAIL
    no change AND FORCE_EMAIL=true           -> EMAIL (manual test)
    no change AND >= 4h since last email     -> EMAIL (heartbeat)
    no change AND < 4h since last email      -> SILENT (action log only)

Required env (set as repo Secrets / Variables in GitHub):
  COMP             (variable)  competition slug, e.g. ling-539-competition-2026
  KAGGLE_USERNAME  (secret)    legacy username from kaggle.json
  KAGGLE_KEY       (secret)    legacy key from kaggle.json
  MY_TEAM          (secret)    your team-name fragment used to find you
  RESEND_API_KEY   (secret)    re_xxx... from resend.com (free tier 100/day)
  NOTIFY_TO        (secret)    where to send the alert
  NOTIFY_FROM      (variable)  default 'HELIX <onboarding@resend.dev>'
  FORCE_EMAIL      (input)     'true' to force an email regardless of state
"""

from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT      = Path(__file__).parent
SNAP_DIR  = ROOT / "snapshots"
STATE     = ROOT / "state.json"
TMP_DIR   = ROOT / ".tmp"
SNAP_DIR.mkdir(exist_ok=True)

COMP        = os.environ.get("COMP", "ling-539-competition-2026")
MY_TEAM     = os.environ.get("MY_TEAM", "").strip().lower()
RESEND_KEY  = os.environ.get("RESEND_API_KEY", "").strip()
NOTIFY_TO   = os.environ.get("NOTIFY_TO", "").strip()
NOTIFY_FROM = os.environ.get("NOTIFY_FROM", "HELIX <onboarding@resend.dev>").strip()
FORCE_EMAIL = os.environ.get("FORCE_EMAIL", "").strip().lower() in ("true", "1", "yes")

NO_CHANGE_HEARTBEAT_HOURS = 4.0


# ─── log helper ──────────────────────────────────────────────────────────────
def banner(label: str) -> None:
    print(f"\n{'─' * 60}\n  {label}\n{'─' * 60}")


# ─── Preflight ───────────────────────────────────────────────────────────────
def preflight() -> None:
    banner("PREFLIGHT")
    print(f"python          = {platform.python_version()}")
    kaggle_path = shutil.which("kaggle")
    print(f"kaggle CLI      = {kaggle_path or '(NOT FOUND on PATH)'}")
    if kaggle_path:
        try:
            v = subprocess.run(
                ["kaggle", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            print(f"kaggle version  = {(v.stdout + v.stderr).strip()}")
        except Exception as e:
            print(f"kaggle version  = (failed: {e})")
    print(f"COMP            = {COMP}")
    print(f"MY_TEAM         = {'set' if MY_TEAM                          else 'NOT SET'}")
    print(f"KAGGLE_USERNAME = {'set' if os.environ.get('KAGGLE_USERNAME') else 'NOT SET'}")
    print(f"KAGGLE_KEY      = {'set' if os.environ.get('KAGGLE_KEY')      else 'NOT SET'}")
    print(f"RESEND_API_KEY  = {'set' if RESEND_KEY                        else 'NOT SET'}")
    print(f"NOTIFY_TO       = {'set' if NOTIFY_TO                         else 'NOT SET'}")
    print(f"NOTIFY_FROM     = {NOTIFY_FROM}")
    print(f"FORCE_EMAIL     = {FORCE_EMAIL}")
    print(f"heartbeat       = {NO_CHANGE_HEARTBEAT_HOURS}h")


# ─── Fetch ───────────────────────────────────────────────────────────────────
def fetch_leaderboard() -> list[dict]:
    banner("FETCH LEADERBOARD")
    TMP_DIR.mkdir(exist_ok=True)
    for f in TMP_DIR.glob("*"):
        f.unlink()

    cmd = ["kaggle", "competitions", "leaderboard", COMP, "-d", "-p", str(TMP_DIR)]
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        print(f"[kaggle stdout]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        print(f"[kaggle stderr]\n{proc.stderr.rstrip()}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"kaggle CLI failed with exit {proc.returncode}. Common causes:\n"
            f"  401 -> KAGGLE_USERNAME / KAGGLE_KEY wrong, swapped, or quoted.\n"
            f"  403 -> account hasn't joined / accepted rules for '{COMP}'.\n"
            f"  404 -> COMP slug is wrong.\n"
        )

    zips = list(TMP_DIR.glob("*.zip"))
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            zf.extractall(TMP_DIR)
        zips[0].unlink()

    csvs = list(TMP_DIR.glob("*.csv"))
    if not csvs:
        raise RuntimeError("kaggle CLI produced no CSV in .tmp/")

    print(f"csv             = {csvs[0].name}")
    rows: list[dict] = []
    with csvs[0].open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "team_id":   r.get("TeamId"),
                "team_name": r.get("TeamName") or r.get("Team") or "",
                "score":     r.get("Score") or "",
                "submitted": r.get("SubmissionDate") or r.get("LastSubmissionDate") or "",
            })
    print(f"parsed rows     = {len(rows)}")
    return rows


# ─── Lookup ──────────────────────────────────────────────────────────────────
def find_me(board: list[dict]) -> tuple[int, dict] | None:
    if not MY_TEAM:
        return None
    for i, row in enumerate(board, 1):
        if MY_TEAM in (row["team_name"] or "").lower():
            return i, row
    return None


# ─── Email ───────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str) -> bool:
    if not (RESEND_KEY and NOTIFY_TO):
        print("[email] skipped (RESEND_API_KEY or NOTIFY_TO missing)")
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    NOTIFY_FROM,
                "to":      [NOTIFY_TO],
                "subject": subject,
                "text":    body,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[email] network error: {e}")
        return False

    print(f"[email] HTTP {r.status_code}")
    print(f"[email] body : {r.text[:400]}")
    if not r.ok:
        if r.status_code == 403:
            print("[email] hint: Resend free tier only delivers to your signup")
            print("[email]       address unless you verify a domain.")
        elif r.status_code == 422:
            print("[email] hint: NOTIFY_FROM format probably invalid.")
        elif r.status_code == 429:
            print("[email] hint: Resend rate limit (100/day on free tier).")
        return False
    return True


# ─── Diff ────────────────────────────────────────────────────────────────────
def compute_diff(prev_board: list[dict] | None,
                 curr_board: list[dict]) -> dict | None:
    if not prev_board:
        return None

    prev_by_id = {r["team_id"]: (i + 1, r) for i, r in enumerate(prev_board)}
    curr_by_id = {r["team_id"]: (i + 1, r) for i, r in enumerate(curr_board)}

    new_teams       : list[dict] = []
    removed_teams   : list[dict] = []
    score_changes   : list[dict] = []
    new_submissions : list[dict] = []

    for tid, (curr_rank, curr_row) in curr_by_id.items():
        if tid not in prev_by_id:
            new_teams.append({
                "rank":      curr_rank,
                "team_name": curr_row["team_name"],
                "score":     curr_row["score"],
                "submitted": curr_row["submitted"],
            })
            continue

        prev_rank, prev_row = prev_by_id[tid]
        prev_score     = prev_row.get("score", "") or ""
        curr_score     = curr_row.get("score", "") or ""
        prev_submitted = prev_row.get("submitted", "") or ""
        curr_submitted = curr_row.get("submitted", "") or ""

        if prev_score != curr_score:
            score_changes.append({
                "team_name":  curr_row["team_name"],
                "prev_rank":  prev_rank,
                "curr_rank":  curr_rank,
                "prev_score": prev_score,
                "curr_score": curr_score,
                "submitted":  curr_submitted,
            })
        elif curr_submitted and prev_submitted != curr_submitted:
            new_submissions.append({
                "team_name": curr_row["team_name"],
                "rank":      curr_rank,
                "score":     curr_score,
                "submitted": curr_submitted,
            })

    for tid, (prev_rank, prev_row) in prev_by_id.items():
        if tid not in curr_by_id:
            removed_teams.append({
                "prev_rank": prev_rank,
                "team_name": prev_row["team_name"],
                "score":     prev_row.get("score", ""),
            })

    any_change = bool(new_teams or removed_teams or score_changes or new_submissions)
    return {
        "new_teams":       new_teams,
        "removed_teams":   removed_teams,
        "score_changes":   score_changes,
        "new_submissions": new_submissions,
        "any_change":      any_change,
    }


# ─── Email body ──────────────────────────────────────────────────────────────
def short_dt(s: str) -> str:
    if not s:
        return ""
    s = s.replace("T", " ").replace("Z", "")
    return s[5:16] if len(s) >= 16 else s


def render_full_board(board: list[dict],
                      me: tuple[int, dict] | None) -> list[str]:
    L: list[str] = []
    L.append("=" * 60)
    L.append(f"  COMPLETE LEADERBOARD ({len(board)} teams)")
    L.append("=" * 60)
    L.append("")
    for i, r in enumerate(board, 1):
        name = (r["team_name"] or "")[:32]
        marker = "  <-- YOU" if (me and i == me[0]) else ""
        L.append(f"  #{i:>3}  {name:32}  {r['score']:>10}  "
                 f"[{short_dt(r['submitted'])}]{marker}")
    return L


def build_email(iso: str,
                diff: dict | None,
                board: list[dict],
                me: tuple[int, dict] | None,
                trigger: str) -> tuple[str, str]:
    """trigger ∈ {'cold-start', 'changes', 'heartbeat', 'forced'}"""

    me_tag = f"  (you: #{me[0]}, score={me[1]['score']})" if me else ""
    change_lines: list[str] = []

    if trigger == "cold-start":
        summary = "baseline established"
        change_lines.append("First run / no previous state to compare against.")
        change_lines.append("Baseline below.")

    elif trigger == "heartbeat":
        summary = f"no change ({NO_CHANGE_HEARTBEAT_HOURS:.0f}h heartbeat)"
        change_lines.append("No changes since last poll. "
                            f"Routine {NO_CHANGE_HEARTBEAT_HOURS:.0f}h heartbeat fire.")

    elif trigger == "forced":
        summary = "no change (manual test)"
        change_lines.append("No changes since last poll. Manual test fire (FORCE_EMAIL=true).")

    else:  # 'changes'
        nc = len(diff["score_changes"])
        nn = len(diff["new_teams"])
        nr = len(diff["removed_teams"])
        ns = len(diff["new_submissions"])
        bits = []
        if nc: bits.append(f"{nc} score change{'s' if nc != 1 else ''}")
        if nn: bits.append(f"{nn} new team{'s' if nn != 1 else ''}")
        if nr: bits.append(f"{nr} removed")
        if ns: bits.append(f"{ns} resubmission{'s' if ns != 1 else ''}")
        summary = ", ".join(bits)

        if diff["score_changes"]:
            change_lines.append("")
            change_lines.append(f"Score changes ({nc}):")
            for c in diff["score_changes"]:
                name = (c["team_name"] or "")[:32]
                rank_arrow = (f"#{c['prev_rank']:>3} -> #{c['curr_rank']:<3}"
                              if c["prev_rank"] != c["curr_rank"]
                              else f"#{c['curr_rank']:<3} (no rank change)")
                change_lines.append(
                    f"  {name:32}  {rank_arrow}  "
                    f"{c['prev_score']} -> {c['curr_score']}  "
                    f"[{short_dt(c['submitted'])}]"
                )

        if diff["new_teams"]:
            change_lines.append("")
            change_lines.append(f"New teams ({nn}):")
            for t in diff["new_teams"]:
                name = (t["team_name"] or "")[:32]
                change_lines.append(
                    f"  {name:32}  #{t['rank']:<3}                 "
                    f"score={t['score']}  [{short_dt(t['submitted'])}]"
                )

        if diff["removed_teams"]:
            change_lines.append("")
            change_lines.append(f"Removed teams ({nr}):")
            for t in diff["removed_teams"]:
                name = (t["team_name"] or "")[:32]
                change_lines.append(
                    f"  {name:32}  was #{t['prev_rank']:<3}             "
                    f"score={t['score']}"
                )

        if diff["new_submissions"]:
            change_lines.append("")
            change_lines.append(f"Resubmissions, no score improvement ({ns}):")
            for t in diff["new_submissions"]:
                name = (t["team_name"] or "")[:32]
                change_lines.append(
                    f"  {name:32}  #{t['rank']:<3}                 "
                    f"score={t['score']}  [{short_dt(t['submitted'])}]"
                )

    subject = f"HELIX :: {summary}{me_tag}"

    L: list[str] = []
    L.append(f"Time:        {iso}")
    L.append(f"Competition: {COMP}")
    L.append(f"Trigger:     {trigger}")
    L.append("")
    L.append("=" * 60)
    L.append("  CHANGES SINCE LAST POLL")
    L.append("=" * 60)
    L.extend(change_lines)
    L.append("")
    L.extend(render_full_board(board, me))
    L.append("")
    L.append(f"https://www.kaggle.com/competitions/{COMP}/leaderboard")
    return subject, "\n".join(L)


# ─── time helpers ────────────────────────────────────────────────────────────
def hours_since(iso_str: str) -> float:
    if not iso_str:
        return 9999.0
    try:
        then = datetime.fromisoformat(iso_str)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
    except Exception:
        return 9999.0


# ─── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    preflight()

    if not COMP:
        sys.exit("COMP env var required")

    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    print(f"\n[poll] {iso}  competition={COMP}")

    board = fetch_leaderboard()

    banner("DIFF + DECISION")
    me = find_me(board)
    if me is None:
        print(f"WARNING: team fragment '{MY_TEAM}' not found on board")

    curr_rank  = me[0] if me else None
    curr_score = me[1]["score"] if me else None

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception as e:
            print(f"[state] could not parse previous state: {e}")

    prev_board       = prev.get("full_board")
    last_email_iso   = prev.get("last_email_at", "")
    hrs_since_email  = hours_since(last_email_iso)

    diff = compute_diff(prev_board, board)

    # decide trigger
    if diff is None:
        state_label = "COLD START"
        trigger     = "cold-start"
        should_send = True
        reason      = "first run, no prev state"
    elif diff["any_change"]:
        state_label = "CHANGES DETECTED"
        trigger     = "changes"
        should_send = True
        reason      = (f"score:{len(diff['score_changes'])} "
                       f"new:{len(diff['new_teams'])} "
                       f"removed:{len(diff['removed_teams'])} "
                       f"resub:{len(diff['new_submissions'])}")
    elif FORCE_EMAIL:
        state_label = "NO CHANGE (forced)"
        trigger     = "forced"
        should_send = True
        reason      = "FORCE_EMAIL=true"
    elif hrs_since_email >= NO_CHANGE_HEARTBEAT_HOURS:
        state_label = "NO CHANGE (heartbeat)"
        trigger     = "heartbeat"
        should_send = True
        reason      = (f"{hrs_since_email:.1f}h since last email "
                       f">= {NO_CHANGE_HEARTBEAT_HOURS}h threshold")
    else:
        state_label = "NO CHANGE (silent)"
        trigger     = "silent"
        should_send = False
        reason      = (f"{hrs_since_email:.1f}h since last email "
                       f"< {NO_CHANGE_HEARTBEAT_HOURS}h threshold")

    print(f"state           = {state_label}")
    print(f"reason          = {reason}")
    print(f"send email      = {should_send}")

    # per-poll snapshot (always)
    snap = SNAP_DIR / f"{iso.replace(':','-')}.json"
    snap.write_text(json.dumps({
        "at":         iso,
        "team_count": len(board),
        "my_rank":    curr_rank,
        "my_score":   curr_score,
        "full_board": board,
    }, indent=2))

    # email if triggered
    alerted = False
    if should_send:
        banner("ALERT")
        subject, body = build_email(iso, diff, board, me, trigger)
        print(f"subject: {subject}")
        alerted = send_email(subject, body)

    # update last_email_at on successful send (preserves throttle correctness)
    new_last_email = iso if alerted else last_email_iso

    # persist state
    STATE.write_text(json.dumps({
        "at":             iso,
        "my_rank":        curr_rank,
        "my_score":       curr_score,
        "team_count":     len(board),
        "alerted":        alerted,
        "last_email_at":  new_last_email,
        "last_trigger":   trigger,
        "full_board":     board,
    }, indent=2))

    banner("DONE")
    print(f"rank={curr_rank}  score={curr_score}  trigger={trigger}  alerted={alerted}")


if __name__ == "__main__":
    main()
