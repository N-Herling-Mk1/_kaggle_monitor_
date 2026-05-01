#!/usr/bin/env python3
"""
HELIX :: Kaggle leaderboard polling worker for GitHub Actions.

  - Pulls the leaderboard via the official kaggle CLI.
  - Compares your rank/score against the last committed state.json.
  - On rank drop (someone passed you) -> emails via Resend.
  - Writes a fresh state.json + per-poll snapshot for the git commit.

Required env (set as repo Secrets / Variables in GitHub):
  COMP             (variable)  competition slug, e.g. ling-539-competition-2026
  KAGGLE_USERNAME  (secret)    from kaggle.json
  KAGGLE_KEY       (secret)    from kaggle.json
  MY_TEAM          (secret)    your team-name fragment used to find you
  RESEND_API_KEY   (secret)    re_xxx... from resend.com (free tier 100/day)
  NOTIFY_TO        (secret)    where to send the alert
  NOTIFY_FROM      (variable)  default 'HELIX <onboarding@resend.dev>'
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


# ─── log helper ──────────────────────────────────────────────────────────────
def banner(label: str) -> None:
    print(f"\n{'─' * 60}\n  {label}\n{'─' * 60}")


# ─── Preflight: confirm env + tooling are wired (no secret values printed) ───
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


# ─── Fetch ───────────────────────────────────────────────────────────────────
def fetch_leaderboard() -> list[dict]:
    """Use the kaggle CLI to download a CSV; return list of rows."""
    banner("FETCH LEADERBOARD")

    TMP_DIR.mkdir(exist_ok=True)
    for f in TMP_DIR.glob("*"):
        f.unlink()

    cmd = ["kaggle", "competitions", "leaderboard", COMP, "-d", "-p", str(TMP_DIR)]
    print(f"$ {' '.join(cmd)}")

    # Capture but ALSO surface stdout/stderr so we can see what kaggle says.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        print(f"[kaggle stdout]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        print(f"[kaggle stderr]\n{proc.stderr.rstrip()}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"kaggle CLI failed with exit {proc.returncode}. Common causes:\n"
            f"  401 -> KAGGLE_USERNAME / KAGGLE_KEY secrets are wrong or swapped.\n"
            f"  403 -> the account that owns the token has not joined / accepted\n"
            f"         rules for '{COMP}'. Open the competition page while\n"
            f"         logged in as that account and click Join / Accept.\n"
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
                "submitted": r.get("SubmissionDate") or r.get("LastSubmissionDate"),
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


# ─── Email (never raises; failure is logged + returned as False) ─────────────
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
        return False
    return True


# ─── Diff logic: who passed me (keyed on team_id, not name) ──────────────────
def passers(prev_board: list[dict] | None,
            curr_board: list[dict],
            prev_rank: int | None,
            curr_rank: int) -> list[dict]:
    """Rows currently ranked above me that were NOT above me last time."""
    if not prev_board or prev_rank is None:
        return []
    prev_above_ids = {r.get("team_id") for r in prev_board[:prev_rank - 1]}
    curr_above     = curr_board[:curr_rank - 1]
    return [r for r in curr_above if r.get("team_id") not in prev_above_ids]


# ─── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    preflight()

    if not COMP:
        sys.exit("COMP env var required")

    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    print(f"\n[poll] {iso}  competition={COMP}")

    board = fetch_leaderboard()

    banner("RANK DIFF")
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

    prev_rank  = prev.get("my_rank")
    prev_score = prev.get("my_score")
    prev_board = prev.get("full_board")

    print(f"prev rank/score : {prev_rank} / {prev_score}")
    print(f"curr rank/score : {curr_rank} / {curr_score}")

    # per-poll snapshot for git history
    snap = SNAP_DIR / f"{iso.replace(':','-')}.json"
    snap.write_text(json.dumps({
        "at":         iso,
        "team_count": len(board),
        "my_rank":    curr_rank,
        "my_score":   curr_score,
        "top_25":     board[:25],
    }, indent=2))

    # alert decision
    alerted = False
    if curr_rank and prev_rank and curr_rank > prev_rank:
        banner("ALERT")
        new_above = passers(prev_board, board, prev_rank, curr_rank)
        delta     = curr_rank - prev_rank
        subject   = f"HELIX :: rank drop  #{prev_rank} -> #{curr_rank}  (-{delta})"
        lines = [
            f"Time:        {iso}",
            f"Competition: {COMP}",
            f"",
            f"Your rank dropped from #{prev_rank} to #{curr_rank}  ({-delta:+d}).",
            f"Your score:  {curr_score}   (prev: {prev_score})",
            f"",
        ]
        if new_above:
            lines.append(f"Teams that passed you ({len(new_above)}):")
            for r in new_above:
                name = (r["team_name"] or "")[:30]
                lines.append(f"   - {name:30}  score={r['score']}")
            lines.append("")
        lines += [
            f"Top of board now:",
            *[f"  #{i+1:3}  {(r['team_name'] or '')[:30]:30}  {r['score']}"
              for i, r in enumerate(board[:10])],
            "",
            f"https://www.kaggle.com/competitions/{COMP}/leaderboard",
        ]
        alerted = send_email(subject, "\n".join(lines))
    elif curr_rank and prev_rank and curr_rank < prev_rank:
        print(f"climbed: #{prev_rank} -> #{curr_rank}  (no email; only on drops)")
    else:
        print("no rank change")

    # persist new state regardless of email outcome
    STATE.write_text(json.dumps({
        "at":         iso,
        "my_rank":    curr_rank,
        "my_score":   curr_score,
        "team_count": len(board),
        "alerted":    alerted,
        "full_board": board,
    }, indent=2))

    banner("DONE")
    print(f"rank={curr_rank}  score={curr_score}  alerted={alerted}")


if __name__ == "__main__":
    main()
