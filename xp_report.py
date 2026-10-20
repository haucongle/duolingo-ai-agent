"""
XP report for the practice farm, read from Duolingo's own XP history.

  python xp_report.py snapshot FILE   save the current total XP (before a farm run)
  python xp_report.py run FILE        XP gained since the snapshot + the last 7 days
  python xp_report.py daily           yesterday's XP + the last 7 days (for the daily email)

Markdown goes to stdout, and is also appended to $GITHUB_STEP_SUMMARY when it is set.
Days are the account's Duolingo days (the account timezone is UTC, i.e. 07:00 Vietnam time).
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

SESSION_FILE = "vi_duo_session.json"
API = "https://www.duolingo.com/2017-06-30/users"
# The users endpoint answers 403 to non-browser user agents.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HISTORY_DAYS = 7


def _jwt():
    token = os.getenv("VI_DUO_JWT") or os.getenv("DUO_JWT")
    if token:
        return token
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, encoding="utf-8") as f:
            for cookie in json.load(f).get("cookies", []):
                if cookie["name"] == "jwt_token":
                    return cookie["value"]
    sys.exit("No Duolingo JWT: set VI_DUO_JWT or keep a logged-in vi_duo_session.json.")


JWT = _jwt()
_payload = JWT.split(".")[1]
USER_ID = json.loads(base64.urlsafe_b64decode(_payload + "=" * (-len(_payload) % 4)))["sub"]


def _get(path):
    request = urllib.request.Request(
        f"{API}/{USER_ID}{path}",
        headers={"Authorization": f"Bearer {JWT}", "User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def total_xp():
    return _get("?fields=totalXp")["totalXp"]


def daily_history(days=HISTORY_DAYS):
    """[(date, xp, sessions, minutes)] for the last `days` days, newest first."""
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=days - 1)
    summaries = _get(f"/xp_summaries?startDate={start}&endDate={end}")["summaries"]
    rows = []
    for s in summaries:
        day = dt.datetime.fromtimestamp(s["date"], dt.timezone.utc).date()
        rows.append((day, s.get("gainedXp") or 0, s.get("numSessions") or 0,
                     round((s.get("totalSessionTime") or 0) / 60)))
    return sorted(rows, reverse=True)


def history_table(rows):
    lines = ["| Ngày | XP | Sessions | Thời gian |", "|---|--:|--:|--:|"]
    for day, xp, sessions, minutes in rows:
        lines.append(f"| {day:%a %d/%m} | {xp} | {sessions} | {minutes // 60}h{minutes % 60:02d} |")
    total = sum(r[1] for r in rows)
    lines.append(f"| **{len(rows)} ngày** | **{total}** | | |")
    return "\n".join(lines)


def emit(markdown):
    print(markdown)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "snapshot" and len(sys.argv) == 3:
        xp = total_xp()
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(str(xp))
        print(f"Total XP before the run: {xp}")
    elif mode == "run" and len(sys.argv) == 3:
        rows = daily_history()
        try:
            with open(sys.argv[2], encoding="utf-8") as f:
                gained = f"+{total_xp() - int(f.read())} XP"
        except (OSError, ValueError):
            gained = "không rõ (thiếu snapshot trước khi farm)"
        today = rows[0] if rows else None
        emit("\n".join([
            "### 🦉 XP report",
            f"- **Lượt chạy này:** {gained}",
            f"- **Hôm nay:** {today[1]} XP / {today[2]} sessions" if today else "- **Hôm nay:** 0 XP",
            "",
            history_table(rows),
        ]))
    elif mode == "daily":
        today = dt.datetime.now(dt.timezone.utc).date()
        # Drop today: the report runs just after midnight, so today has barely started.
        rows = [r for r in daily_history(HISTORY_DAYS + 1) if r[0] < today]
        yesterday = rows[0] if rows else None
        emit("\n".join([
            f"### 🦉 XP ngày {yesterday[0]:%d/%m}: **{yesterday[1]} XP** ({yesterday[2]} sessions)"
            if yesterday else "### 🦉 Hôm qua không có XP nào",
            "",
            history_table(rows),
        ]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
