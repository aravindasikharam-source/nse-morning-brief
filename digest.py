"""Format fetch.py's output into a plain-text Telegram digest. No LLM involved.

This is the mechanical-only path: it applies the same numeric filters the
RUNBOOK asks Agent C to apply (reject wide ATR, extended RSI, unconfirmed
volume) and ranks what survives, but it cannot do what the agents did in the
dry run -- read transmission (crude -> which sector), verify an ambiguous
ticker tag, or judge whether a headline is really a catalyst. Label output
accordingly: this is a screen, not analysis.

Usage:
    python digest.py <path/to/premarket_YYYY-MM-DD.json>
"""

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

MAX_CANDIDATES = 8
MAX_HEADLINES = 6
TELEGRAM_LIMIT = 3500


def macro_lines(payload: dict) -> list[str]:
    india, glob = payload["india"], payload["global"]

    def fmt(name, block, label):
        rec = block.get(name, {})
        if "error" in rec:
            return None
        tag = " (stale, not 1d)" if rec.get("stale") else ""
        return f"{label} {rec['last']} ({rec['pct']:+.2f}%{tag})"

    parts = [
        fmt("nifty50", india, "Nifty"),
        fmt("banknifty", india, "BankNifty"),
        fmt("indiavix", india, "VIX"),
        fmt("sp500", glob, "S&P"),
        fmt("brent", glob, "Brent"),
        fmt("usdinr", glob, "USDINR"),
    ]
    return [p for p in parts if p]


def rank_candidates(payload: dict) -> list[dict]:
    """Same numeric filters RUNBOOK.md asks Agent C to apply -- arithmetic only,
    no read on whether a setup is actually good, just whether it's not obviously bad."""
    out = []
    for c in payload["candidates"]:
        if c["atr_pct"] > 6:
            continue
        if c["rsi14"] > 75:
            continue
        if c["vol_ratio"] < 1.0:
            continue
        if c["risk_atr_mult"] <= 1.01:
            # ATR floor fired -- no real structural stop of its own.
            continue
        out.append(c)
    out.sort(key=lambda c: -c["vol_ratio"])
    return out[:MAX_CANDIDATES]


def top_headlines(payload: dict) -> list[dict]:
    tagged = [i for i in payload["news"]["items"] if i.get("fo") and not i.get("is_price_list")]
    tagged.sort(key=lambda i: i["age_hours"])
    return tagged[:MAX_HEADLINES]


def build_message(payload: dict, date_str: str) -> str:
    lines = [f"NSE Morning Digest -- {date_str} (mechanical filter, no agent read)", ""]

    lines.append("MACRO")
    lines.extend(macro_lines(payload))
    lines.append("")

    cands = rank_candidates(payload)
    lines.append(f"SCREEN -- {len(cands)} passed ATR/RSI/volume filters (raw, unranked by story)")
    if not cands:
        lines.append("None today. A quiet session is a real finding, not a failure.")
    for c in cands:
        lines.append(
            f"{c['symbol']}: {'/'.join(c['setups'])} | trigger {c['trigger']} "
            f"invalidate {c['invalidation']} risk {c['risk_pct']}% | "
            f"vol {c['vol_ratio']}x rsi {c['rsi14']}"
        )
    lines.append("")

    heads = top_headlines(payload)
    if heads:
        lines.append("TAGGED HEADLINES (unverified -- '?' = single-word name match, check it)")
        for h in heads:
            syms = ",".join(h["fo"])
            lines.append(f"[{syms}] {h['title'][:90]}")
        lines.append("")

    stale = payload["news"].get("dead_sources") or []
    if stale:
        lines.append(f"News sources returning nothing today: {', '.join(stale)}")
        lines.append("")

    lines.append("No confluence scoring, no sector-transmission read, no ambiguous-ticker check --")
    lines.append("this digest is arithmetic only. Screener output, not advice.")

    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[:TELEGRAM_LIMIT - 20] + "\n...(truncated)"
    return msg


def send_telegram(text: str, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram send failed: {body}")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python digest.py <premarket_YYYY-MM-DD.json>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    payload = json.loads(open(path, encoding="utf-8").read())
    date_str = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    message = build_message(payload, date_str)
    print(message)
    print(f"\n--- {len(message)} chars ---")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- printed only, not sent.", file=sys.stderr)
        return
    send_telegram(message, token, chat_id)
    print("sent to telegram")


if __name__ == "__main__":
    main()
