"""Publish percent-change-only stats for the nightly Claude portfolio run.

Reads every "Portfolio Review" email from the AgentMail inbox, reduces each to
(portfolio value, cost basis), and writes agent-portfolio.json containing only
percentages and dates. Rupee amounts, symbols, and recommendations never leave
this process, and nothing from an email body is ever printed: this runs in a
public repo, so its logs are public.

Return method: time-weighted. A day's return strips out new money by treating
the change in cost basis as the cash flow, so buying more shares doesn't read
as a gain. Sells are approximated at cost, which is close enough for a
percent-change card and far better than raw P&L-on-cost.

Usage:
  AGENTMAIL_API_KEY=... python agent_portfolio.py agent-portfolio.json
  python agent_portfolio.py out.json --fixture emails.json   # [{timestamp, html|text}]
"""

from __future__ import annotations

import html as htmlmod
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.agentmail.to/v0"
INBOX = "h-4204@agentmail.to"
SUBJECT = "Portfolio Review"
IST = timezone(timedelta(hours=5, minutes=30))
# A single day moving more than this is a parse error, not a market move.
MAX_DAILY_MOVE = 0.15

STATE_RE = re.compile(r"PORTFOLIO_STATE\s*(\{.*?\})\s*PORTFOLIO_STATE", re.S)
AMOUNT = r"([\d,]+(?:\.\d+)?)"
VALUE_RE = re.compile(r"Portfolio[^₹\d]{0,40}₹\s?" + AMOUNT)
PNL_RE = re.compile(r"P&L[^₹]{0,40}?([+\-−–])\s?₹\s?" + AMOUNT)


def _get(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def fetch_emails(key: str) -> list[dict]:
    auth = {"Authorization": f"Bearer {key}"}
    inbox = urllib.parse.quote(INBOX, safe="")
    ids, token = [], None
    while True:
        params = {"limit": 100, "subject": SUBJECT}
        if token:
            params["page_token"] = token
        page = _get(f"{API}/inboxes/{inbox}/messages?{urllib.parse.urlencode(params)}", auth)
        ids += [m["message_id"] for m in page.get("messages", [])]
        token = page.get("next_page_token")
        if not token:
            break
    emails = []
    for mid in ids:
        m = _get(f"{API}/inboxes/{inbox}/messages/{urllib.parse.quote(mid, safe='')}", auth)
        emails.append({"timestamp": m["timestamp"], "html": m.get("html") or "", "text": m.get("text") or ""})
    return emails


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def value_and_cost(email: dict) -> tuple[float, float] | None:
    """(value, cost) from the STATE block when present, else from the headline."""
    for body in (email.get("text", ""), email.get("html", "")):
        m = STATE_RE.search(body)
        if not m:
            continue
        try:
            holdings = json.loads(htmlmod.unescape(m.group(1)))["holdings"]
            value = sum(h["qty"] * h["ltp"] for h in holdings)
            cost = sum(h["qty"] * h["avg"] for h in holdings)
            if value > 0 and cost > 0:
                return value, cost
        except (ValueError, KeyError, TypeError):
            pass

    plain = htmlmod.unescape(re.sub(r"<[^>]+>", " ", email.get("html") or email.get("text", "")))
    plain = re.sub(r"\s+", " ", plain)
    v = VALUE_RE.search(plain)
    if not v:
        return None
    p = PNL_RE.search(plain, v.end(), v.end() + 200)
    if not p:
        return None
    value = _num(v.group(1))
    pnl = _num(p.group(2)) * (1 if p.group(1) == "+" else -1)
    cost = value - pnl
    return (value, cost) if value > 0 and cost > 0 else None


def price_date(ts: str) -> date:
    """The run fires before the NSE opens, so it prices the last weekday
    strictly before its IST calendar date (a Sunday or Monday run prices Friday)."""
    d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def nifty_closes(start: date) -> dict[date, float]:
    p1 = int(datetime.combine(start - timedelta(days=10), datetime.min.time(), timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?period1={p1}&period2={p2}&interval=1d"
    r = _get(url)["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    return {
        datetime.fromtimestamp(t, IST).date(): c
        for t, c in zip(r["timestamp"], closes)
        if c is not None
    }


def close_on(closes: dict[date, float], d: date) -> float | None:
    eligible = [k for k in closes if k <= d]
    return closes[max(eligible)] if eligible else None


def build(emails: list[dict]) -> dict:
    # Latest email per price date wins: re-runs and weekend runs carry corrections.
    by_day: dict[date, tuple[str, float, float]] = {}
    skipped = 0
    for e in sorted(emails, key=lambda e: e["timestamp"]):
        vc = value_and_cost(e)
        if vc is None:
            skipped += 1
            continue
        by_day[price_date(e["timestamp"])] = (e["timestamp"], *vc)
    days = sorted(by_day)
    if len(days) < 2:
        raise SystemExit(f"Not enough parsable reviews ({len(days)} usable, {skipped} skipped).")

    index, rets, cums = 1.0, [0.0], [0.0]
    outliers = 0
    for prev, cur in zip(days, days[1:]):
        _, v0, c0 = by_day[prev]
        _, v1, c1 = by_day[cur]
        r = (v1 - (c1 - c0)) / v0 - 1
        if abs(r) > MAX_DAILY_MOVE:
            outliers += 1
            r = 0.0
        index *= 1 + r
        rets.append(r)
        cums.append(index - 1)

    try:
        closes = nifty_closes(days[0])
        base = close_on(closes, days[0])
        bench = [(close_on(closes, d) / base - 1) if base else None for d in days]
    except Exception as exc:  # benchmark is optional; never fail the run over it
        print(f"benchmark unavailable ({type(exc).__name__})")
        bench = [None] * len(days)

    series = []
    for d, c, b in zip(days, cums, bench):
        point = {"d": d.isoformat(), "cum": round(c * 100, 2)}
        if b is not None:
            point["bench"] = round(b * 100, 2)
        series.append(point)

    out = {
        "updated": by_day[days[-1]][0],
        "as_of": days[-1].isoformat(),
        "inception": days[0].strftime("%b %Y"),
        "cumulative_pct": series[-1]["cum"],
        "today_pct": round(rets[-1] * 100, 2),
        "benchmark_label": "Nifty 50",
        "series": series,
    }
    if bench[-1] is not None:
        out["benchmark_cumulative_pct"] = series[-1]["bench"]
    print(
        f"reviews={len(emails)} days={len(days)} skipped={skipped} outliers={outliers} "
        f"cum={out['cumulative_pct']:+.2f}% today={out['today_pct']:+.2f}% "
        f"bench={out.get('benchmark_cumulative_pct', 'n/a')}"
    )
    return out


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    dest = args[0]
    if "--fixture" in args:
        with open(args[args.index("--fixture") + 1]) as f:
            emails = json.load(f)
    else:
        key = os.environ.get("AGENTMAIL_API_KEY")
        if not key:
            raise SystemExit("AGENTMAIL_API_KEY is not set.")
        emails = fetch_emails(key)
    data = build(emails)
    with open(dest, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
