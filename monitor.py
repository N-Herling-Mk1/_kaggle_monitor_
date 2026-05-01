#!/usr/bin/env python3
"""
HELIX :: Kaggle leaderboard polling worker for GitHub Actions.

  - Pulls the leaderboard via the official kaggle CLI.
  - Compares your rank/score against the last committed state.json.
  - On rank drop (someone passed you) -> emails via Resend.
  - Writes a fresh state.json + per-poll snapshot for the git commit.

Required env (set as repo Secrets / Variables in GitHub):
  COMP            (variable)  competition slug, e.g. ling-539-competition-2026
  MY_TEAM         (secret)    your team-name fragment used to find you
  RESEND_API_KEY  (secret)    re_xxx... from resend.com (free tier 100/day)
  NOTIFY_TO       (secret)    where to send the alert
  NOTIFY_FROM     (variable)  default 'HELIX <onboarding@resend.dev>'
                              (works without verifying a domain)
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT      = Path(__file__).parent
SNAP_DIR  = ROOT / "snapshots"
STATE     = ROOT / "state.json"
SNAP_DIR.mkdir(exist_ok=True)

COMP        = os.environ.get("COMP", "ling-539-competition-2026")
MY_TEAM     = os.environ.get("MY_TEAM", "").strip().lower()
RESEND_KEY  = os.environ.get("RESEND_API_KEY", "").strip()
NOTIFY_TO   = os.environ.get("NOTIFY_TO", "").strip()
NOTIFY_FROM = os.environ.get("NOTIFY_FROM", "HELIX <onboarding@resend.dev>").strip()


# ─── Fetch ───────────────────────────────────────────────────────────────────
def fetch_leaderboard() -> list[dict]:
    """Use the kaggle CLI to download a CSV; return list of {team_name, score, ...}."""
    tmp = ROOT / ".tmp"
    tmp.mkdir(exist_ok=True)
    for f in tmp.glob("*"):
        f.unlink()

    subprocess.run(
        ["kaggle", "competitions", "leaderboard", COMP, "-d", "-p", str(tmp)],
        check=True, capture_output=True, text=True,
    )

    zips = list(tmp.glob("*.zip"))
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            zf.extractall(tmp)
        zips[0].unlink()

    csvs = list(tmp.glob("*.csv"))
    if not csvs:
        raise RuntimeError("kaggle CLI produced no CSV")

    rows: list[dict] = []
    with csvs[0].open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "team_id":   r.get("TeamId"),
                "team_name": r.get("TeamName") or r.get("Team") or "",
                "score":     r.get("Score") or "",
                "submitted": r.get("SubmissionDate") or r.get("LastSubmissionDate"),
            })
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
def send_email(subject: str, body: str) -> None:
    if not (RESEND_KEY and NOTIFY_TO):
        print("[email] skipped (RESEND_API_KEY or NOTIFY_TO missing)")
        return
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
    print(f"[email] {r.status_code}: {r.text[:200]}")
    r.raise_for_status()


# ─── Diff logic: who passed me ───────────────────────────────────────────────
def passers(prev_board: list[dict] | None,
            curr_board: list[dict],
            prev_rank: int | None,
            curr_rank: int) -> list[str]:
    """Teams currently ranked above me that were NOT above me last time."""
    if not prev_board or prev_rank is None:
        return []
    prev_above = {r["team_name"] for r in prev_board[:prev_rank - 1]}
    curr_above = {r["team_name"] for r in curr_board[:curr_rank - 1]}
    new_above  = [t for t in curr_above if t and t not in prev_above]
    # preserve current-board order
    by_name = {r["team_name"]: r for r in curr_board}
    new_above.sort(key=lambda n: curr_board.index(by_name[n]))
    return new_above


# ─── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    if not COMP:
        sys.exit("COMP env var required")

    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    print(f"[poll] {iso} competition={COMP}")

    board = fetch_leaderboard()
    print(f"[poll] {len(board)} teams")

    me = find_me(board)
    if me is None:
        print(f"[poll] WARNING: team '{MY_TEAM}' not found on board")

    curr_rank  = me[0] if me else None
    curr_score = me[1]["score"] if me else None

    # load previous state
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception as e:
            print(f"[state] could not parse previous state: {e}")

    prev_rank  = prev.get("my_rank")
    prev_score = prev.get("my_score")
    prev_board = prev.get("full_board")

    # compact per-poll snapshot for git history
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
        new_above = passers(prev_board, board, prev_rank, curr_rank)
        delta = curr_rank - prev_rank
        subject = f"HELIX :: rank drop  #{prev_rank} -> #{curr_rank}  (-{delta})"
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
            for t in new_above:
                row = next(r for r in board if r["team_name"] == t)
                lines.append(f"   - {t:30}  score={row['score']}")
            lines.append("")
        lines += [
            f"Top of board now:",
            *[f"  #{i+1:3}  {r['team_name'][:30]:30}  {r['score']}"
              for i, r in enumerate(board[:10])],
            "",
            f"https://www.kaggle.com/competitions/{COMP}/leaderboard",
        ]
        send_email(subject, "\n".join(lines))
        alerted = True
    elif curr_rank and prev_rank and curr_rank < prev_rank:
        print(f"[poll] you climbed: #{prev_rank} -> #{curr_rank}  (no email; only on drops)")
    else:
        print(f"[poll] no rank change  (prev={prev_rank}, curr={curr_rank})")

    # persist new state
    STATE.write_text(json.dumps({
        "at":         iso,
        "my_rank":    curr_rank,
        "my_score":   curr_score,
        "team_count": len(board),
        "alerted":    alerted,
        "full_board": board,
    }, indent=2))

    print(f"[done]  rank={curr_rank}  score={curr_score}  alerted={alerted}")


if __name__ == "__main__":
    main()
