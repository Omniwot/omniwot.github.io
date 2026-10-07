"""Publish percent-change-only stats for the nightly Claude portfolio run.

Reads every "Portfolio Review" email from the AgentMail inbox and writes
agent-portfolio.json containing only percentages and dates. Rupee amounts,
symbols, and recommendations never leave this process, and nothing from an
email body is ever printed: this runs in a public repo, so its logs are public.

Return method: each day's return is yesterday's holdings priced at today's
prices — sum(q_prev * (p_today - p_prev)) / sum(q_prev * p_prev) over symbols
held on both days. Buys, sells and new money therefore can't masquerade as
performance (an earlier cost-basis method booked every profitable sale as a
loss). Holdings come from the STATE block when present, else from the
email's holdings table. If neither day parses, a day with an unchanged cost
basis falls back to the headline value change (exact when nothing traded);
otherwise the day is left flat and counted as unresolved.

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
# Parsed holdings must sum to within this of the headline value to be trusted.
RECONCILE_TOL = 0.03

STATE_RE = re.compile(r"PORTFOLIO_STATE\s*(\{.*?\})\s*PORTFOLIO_STATE", re.S)
AMOUNT = r"([\d,]+(?:\.\d+)?)"
VALUE_RE = re.compile(r"Portfolio[^₹\d]{0,40}₹\s?" + AMOUNT)
PNL_RE = re.compile(r"P&L[^₹]{0,40}?([+\-−–])\s?₹\s?" + AMOUNT)
RUPEE_RE = re.compile(r"₹\s?" + AMOUNT)
QTY_RE = re.compile(r"\b(\d+)\s*(?:sh\b|@)")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9&\-]{1,19}$")


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


def _plain(fragment: str) -> str:
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text).strip()


def headline(email: dict) -> tuple[float, float] | None:
    """(value, cost) from the headline sentence."""
    plain = _plain(email.get("html") or email.get("text", ""))
    v = VALUE_RE.search(plain)
    if not v:
        return None
    p = PNL_RE.search(plain, v.end(), v.end() + 200)
    if not p:
        return None
    value = _num(v.group(1))
    cost = value - _num(p.group(2)) * (1 if p.group(1) == "+" else -1)
    return (value, cost) if value > 0 and cost > 0 else None


def holdings_from_state(email: dict) -> dict[str, tuple[float, float]] | None:
    for body in (email.get("text", ""), email.get("html", "")):
        m = STATE_RE.search(body)
        if not m:
            continue
        try:
            rows = json.loads(htmlmod.unescape(m.group(1)))["holdings"]
            out = {h["symbol"]: (float(h["qty"]), float(h["ltp"])) for h in rows}
            if out and all(q > 0 and p > 0 for q, p in out.values()):
                return out
        except (ValueError, KeyError, TypeError):
            pass
    return None


def holdings_from_table(html: str) -> dict[str, tuple[float, float]] | None:
    """Symbol -> (qty, price) from the first table with a Symbol and a Value column.

    Formats seen: "27 sh · avg ₹959.58 · LTP ₹1,303.10", "86 @ ₹416.45 · ₹383.10",
    and a bare "₹404.35" — the price is always the last rupee figure in the symbol
    cell; quantity is explicit or recovered as value / price.
    """
    for table in re.findall(r"<table.*?</table>", html, re.S | re.I):
        rows = re.findall(r"<tr.*?</tr>", table, re.S | re.I)
        if not rows:
            continue
        header = [_plain(c).lower() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], re.S | re.I)]
        if not header or "symbol" not in header[0]:
            continue
        vcol = next((i for i, h in enumerate(header) if h.startswith("value")), None)
        pcol = next((i for i, h in enumerate(header) if "ltp" in h or "price" in h), None)
        if vcol is None:
            continue
        out = {}
        for row in rows[1:]:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            if len(cells) <= vcol:
                continue
            sym_m = re.search(r"<(?:b|strong)>(.*?)</(?:b|strong)>", cells[0], re.S | re.I)
            symbol = _plain(sym_m.group(1)) if sym_m else _plain(cells[0]).split(" ")[0]
            if not SYMBOL_RE.match(symbol):
                continue
            first = _plain(cells[0])
            prices = RUPEE_RE.findall(cells[pcol] if pcol is not None and pcol < len(cells) else first)
            vals = RUPEE_RE.findall(_plain(cells[vcol])) or re.findall(AMOUNT, _plain(cells[vcol]))
            if not prices or not vals:
                continue
            price, value = _num(prices[-1]), _num(vals[0])
            if price <= 0 or value <= 0:
                continue
            q = QTY_RE.search(first)
            qty = float(q.group(1)) if q else round(value / price)
            if qty > 0:
                out[symbol] = (qty, price)
        if out:
            return out
    return None


def parse(email: dict) -> dict:
    head = headline(email)
    hold = holdings_from_state(email) or holdings_from_table(email.get("html", ""))
    # Diagnostic, safe for public logs: row count and a ratio, never amounts or symbols.
    why = "ok" if hold else "no-table"
    if hold and head:
        ratio = sum(q * p for q, p in hold.values()) / head[0]
        if abs(ratio - 1) > RECONCILE_TOL:
            why = f"reconcile ratio={ratio:.3f} rows={len(hold)}"
            hold = None  # table didn't reconcile with the headline; don't trust it
    return {"head": head, "hold": hold, "why": why if head else why + " no-headline"}


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


def day_return(prev: dict, cur: dict) -> tuple[float | None, str]:
    a, b = prev["hold"], cur["hold"]
    if a and b:
        common = [s for s in a if s in b]
        base = sum(a[s][0] * a[s][1] for s in common)
        if base > 0:
            return sum(a[s][0] * (b[s][1] - a[s][1]) for s in common) / base, "holdings"
    pa, pb = prev["head"], cur["head"]
    if pa and pb and abs(pb[1] - pa[1]) < 1:  # no trades: value change is the return
        return pb[0] / pa[0] - 1, "headline"
    return None, "unresolved"


def build(emails: list[dict]) -> dict:
    # Latest email per price date wins: re-runs and weekend runs carry corrections.
    by_day: dict[date, dict] = {}
    unparsed = 0
    for e in sorted(emails, key=lambda e: e["timestamp"]):
        p = parse(e)
        if not p["hold"] and not p["head"]:
            unparsed += 1
            continue
        by_day[price_date(e["timestamp"])] = {**p, "ts": e["timestamp"]}
    days = sorted(by_day)
    if len(days) < 2:
        raise SystemExit(f"Not enough parsable reviews ({len(days)} usable, {unparsed} unparsed).")

    index, rets, cums = 1.0, [0.0], [0.0]
    stats = {"holdings": 0, "headline": 0, "unresolved": 0, "outliers": 0}
    for prev, cur in zip(days, days[1:]):
        r, how = day_return(by_day[prev], by_day[cur])
        stats[how] += 1
        if how == "unresolved":
            print(f"unresolved {cur}: prev[{by_day[prev]['why']}] cur[{by_day[cur]['why']}]")
        if r is None:
            r = 0.0
        elif abs(r) > MAX_DAILY_MOVE:
            stats["outliers"] += 1
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
        "updated": by_day[days[-1]]["ts"],
        "as_of": days[-1].isoformat(),
        "inception": days[0].strftime("%b %Y"),
        "cumulative_pct": series[-1]["cum"],
        "today_pct": round(rets[-1] * 100, 2),
        "benchmark_label": "Nifty 50",
        "series": series,
    }
    if bench[-1] is not None:
        out["benchmark_cumulative_pct"] = series[-1]["bench"]
    with_holdings = sum(1 for d in days if by_day[d]["hold"])
    print(
        f"reviews={len(emails)} days={len(days)} unparsed={unparsed} days_with_holdings={with_holdings} "
        f"returns: holdings={stats['holdings']} headline={stats['headline']} unresolved={stats['unresolved']} "
        f"outliers={stats['outliers']} cum={out['cumulative_pct']:+.2f}% today={out['today_pct']:+.2f}% "
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
