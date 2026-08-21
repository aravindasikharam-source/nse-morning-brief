"""Pre-market data fetch for the NSE morning brief.

Runs ~07:00 IST on weekdays, writes one JSON to data/. No LLM in this step --
scraping is deterministic, and agents reading numbers off a scrape hallucinate them.
The three analysis agents read the JSON this produces.

Usage:
    python fetch.py              # normal run
    python fetch.py --selftest   # verify indicator math, no network
"""

import argparse
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# NSE blocks the default urllib/requests user agent.
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"

GLOBAL_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "dxy": "DX-Y.NYB",
    "us10y": "^TNX",
    "brent": "BZ=F",
    "usdinr": "USDINR=X",
    "gold": "GC=F",
    "nikkei": "^N225",
    "hangseng": "^HSI",
}

# ponytail: Yahoo serves only nifty50/banknifty/vix/it/pharma as true daily bars;
# the rest arrive with multi-week holes and get flagged stale downstream. If the
# daily sector read starts mattering, compute it from the F&O constituents we
# already download (map symbol -> sector, average the 1d return) instead of
# trusting these feeds.
INDIA_TICKERS = {
    "nifty50": "^NSEI",
    "banknifty": "^NSEBANK",
    "indiavix": "^INDIAVIX",
    "nifty_it": "^CNXIT",
    "nifty_auto": "^CNXAUTO",
    "nifty_pharma": "^CNXPHARMA",
    "nifty_metal": "^CNXMETAL",
    "nifty_fmcg": "^CNXFMCG",
    "nifty_energy": "^CNXENERGY",
    "nifty_realty": "^CNXREALTY",
    "nifty_psubank": "^CNXPSUBANK",
    "nifty_infra": "^CNXINFRA",
    "nifty_consumption": "^CNXCONSUM",
    "nifty_media": "^CNXMEDIA",
}

# Moneycontrol and Business Standard were dropped after the first dry run:
# every Moneycontrol feed served a cached April-2024 payload, and Business
# Standard returned zero entries on every URL tried. The source_audit below
# exists so the next feed to die this way is visible the same morning.
NEWS_FEEDS = {
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "et_stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "mint_markets": "https://www.livemint.com/rss/markets",
    "businessline": "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    "ndtv_profit": "https://feeds.feedburner.com/ndtvprofit-latest",
}

# Feeds go stale silently -- Moneycontrol's marketreports.xml was observed serving
# April-2024 items alongside live ones. Anything older than this is dropped, not
# flagged: a two-year-old headline reads exactly like today's if it survives.
NEWS_MAX_AGE_HOURS = 48

# Minimum turnover to be tradeable without slipping the whole edge away.
MIN_TURNOVER_CR = 25.0


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. ewm(alpha=1/n) is Wilder smoothing, not a plain EMA."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ATR."""
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# --------------------------------------------------------------------------
# universe
# --------------------------------------------------------------------------

SKIP_SYMBOLS = {"SYMBOL", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
VALID_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&\-]*")


def fo_table() -> dict[str, str]:
    """F&O-eligible stocks as {symbol: underlying company name}.

    Every F&O name sits inside Nifty 500, so this doubles as the liquidity filter.
    The company name feeds the news tagger.
    """
    resp = requests.get(FO_LOTS_URL, headers=NSE_HEADERS, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = [c.strip().upper() for c in df.columns]
    sym_col = next(c for c in df.columns if "SYMBOL" in c)
    name_col = next((c for c in df.columns if "UNDERLYING" in c), None)

    out = {}
    for _, row in df.iterrows():
        sym = str(row[sym_col]).strip().upper()
        # The lot-size file repeats its header mid-file and carries index
        # derivatives; we screen single stocks only. '&' and '-' are legal in
        # NSE symbols -- an isalnum() check here silently dropped M&M and
        # BAJAJ-AUTO, two of the most liquid names in the universe.
        if sym in SKIP_SYMBOLS or not VALID_SYMBOL.fullmatch(sym):
            continue
        out[sym] = str(row[name_col]).strip() if name_col else sym
    return dict(sorted(out.items()))


def fo_universe() -> list[str]:
    return list(fo_table().keys())


# --------------------------------------------------------------------------
# news tagging
# --------------------------------------------------------------------------

# Single words that are a whole company name for someone but appear constantly in
# ordinary market prose. "Bank of India" reduced to "Bank" matched every headline
# containing the word -- these are never allowed to match on their own.
GENERIC_TOKENS = {
    "bank", "finance", "financial", "motors", "motor", "steel", "power", "cement",
    "energy", "auto", "industries", "enterprises", "india", "indian", "national",
    "state", "global", "capital", "technologies", "systems", "services", "corporation",
    "chemicals", "pharma", "pharmaceuticals", "labs", "laboratories", "life",
    "insurance", "housing", "infra", "infrastructure", "projects", "exchange",
    "container", "petroleum", "gas", "oil", "electric", "electricals", "cements",
    "railway", "railways", "port", "ports", "mills", "paper", "sugar", "tyres",
    # NSE abbreviates the underlying name, so these show up truncated.
    "techno", "tech", "inds", "indus", "fin", "invest", "investments", "agro",
    "engg", "engineering", "products", "resources", "ventures", "holdings",
}
STOPWORDS = {"of", "and", "the", "for", "in"}
CORP_SUFFIX = re.compile(
    r"\s+(limited|ltd\.?|corporation|corp\.?|co\.?|inc\.?|plc)\s*$", re.I
)
PRICE_LIST = re.compile(r"top (gainers|losers)|gainers\s*&\s*losers|movers", re.I)


def _aliases(symbol: str, company: str) -> list[tuple[str, str, str]]:
    """Match strings for one stock, as (alias, confidence, case_rule).

    Multi-word names are safe case-insensitively. A single-word name has to earn
    it: not generic, and capitalised in the headline the way a proper noun is.

    The symbol itself is always included, because NSE's underlying name is an
    internal abbreviation the press never uses -- Dixon trades under the name
    "DIXON TECHNO (INDIA) LTD", while every headline says "Dixon Technologies".
    Short symbols must appear in full caps to count, so BEL matches BEL and not
    the word "Bel".
    """
    name = CORP_SUFFIX.sub("", company).strip()
    name = re.sub(r"[^A-Za-z0-9& ]", " ", name)
    name = " ".join(name.split())
    if not name:
        return []

    # Peel generic words off the RIGHT only. "Dixon Technologies (India) Limited"
    # has to still match a headline saying "Dixon Technologies", but peeling from
    # anywhere would turn "Bank of India" into "Bank of" and match Bank of Baroda.
    forms, tokens = [], name.split()
    while tokens:
        forms.append(" ".join(tokens))
        if tokens[-1].lower() in GENERIC_TOKENS | STOPWORDS:
            tokens = tokens[:-1]
        else:
            break

    out, seen = [], set()
    for form in forms:
        key = form.lower()
        # A form ending in a stopword is a fragment, not a name: peeling "India"
        # off "Bank of India" leaves "Bank of", which would match Bank of Baroda.
        if form.split()[-1].lower() in STOPWORDS:
            continue
        if key in seen:
            continue
        seen.add(key)
        if len(form.split()) >= 2:
            out.append((form, "high", "any"))
        elif key not in GENERIC_TOKENS and (form.upper() == symbol or len(form) >= 5):
            out.append((form, "medium", "cap"))

    if symbol.lower() not in seen and symbol.lower() not in GENERIC_TOKENS:
        out.append((symbol, "medium", "upper" if len(symbol) <= 4 else "cap"))
    return out


def tag_news(items: list[dict], fo_map: dict[str, str]) -> list[dict]:
    """Attach F&O symbols to headlines by literal name match.

    Recall is the machine's job -- it reads all 192 names against every headline
    and never gets bored. Precision stays the agent's job: matches are labelled
    with confidence and the agent adjudicates, rather than trusting this blindly.
    """
    # Peeling generic words can strip a name down to a shared group name --
    # "Bajaj Finance" becomes "Bajaj", which four listed Bajaj entities answer to,
    # and it duly tagged BAJFINANCE onto a Bajaj Auto headline. Any alias more
    # than one symbol claims names a family, not a company, so it is dropped.
    claims: dict[str, set[str]] = {}
    built = []
    for sym, company in fo_map.items():
        for alias, conf, rule in _aliases(sym, company):
            claims.setdefault(alias.lower(), set()).add(sym)
            built.append((sym, alias, conf, rule))

    table, ambiguous = [], sorted(a for a, s in claims.items() if len(s) > 1)
    for sym, alias, conf, rule in built:
        if len(claims[alias.lower()]) > 1:
            continue
        table.append((sym, alias, conf, rule,
                      re.compile(r"\b" + re.escape(alias) + r"\b", re.I)))

    for item in items:
        title = item["title"]
        hits = {}
        for sym, alias, conf, rule, rx in table:
            m = rx.search(title)
            if not m:
                continue
            hit = m.group(0)
            # A bare word must look like a proper noun; a short ticker must be
            # in full caps, so "BEL" counts and the word "Bel" does not.
            if rule == "cap" and not hit[0].isupper():
                continue
            if rule == "upper" and hit != alias.upper():
                continue
            if sym not in hits or conf == "high":
                hits[sym] = {"symbol": sym, "matched": alias, "confidence": conf}
        # Compact on purpose: "INFY" is a confident match, "INFY?" came from a
        # single-word name and wants checking. A dict per hit tripled the slice.
        if hits:
            item["fo"] = sorted(
                h["symbol"] + ("" if h["confidence"] == "high" else "?")
                for h in hits.values()
            )
        # A gainers/losers roundup names stocks without reporting an event.
        if PRICE_LIST.search(title):
            item["is_price_list"] = True
    return items


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def quote_block(tickers: dict[str, str]) -> dict:
    """Last close + prior close + pct change for a named set of tickers.

    A full year, not a week: Yahoo serves most ^CNX* sector indices only on the
    long window -- a 1mo request returns a single bar, too few for a change calc.
    """
    import yfinance as yf

    out = {}
    raw = yf.download(
        list(tickers.values()), period="1y", interval="1d",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )
    for name, tkr in tickers.items():
        try:
            close = raw[tkr]["Close"].dropna()
            if len(close) < 2:
                out[name] = {"error": "insufficient data"}
                continue
            last, prev = float(close.iloc[-1]), float(close.iloc[-2])
            d_last, d_prev = close.index[-1].date(), close.index[-2].date()
            span = (d_last - d_prev).days
            rec = {
                "last": round(last, 2),
                "prev": round(prev, 2),
                "pct": round((last / prev - 1) * 100, 2),
                "as_of": str(d_last),
                "prev_as_of": str(d_prev),
            }
            # Several ^CNX* indices publish with holes. When the prior bar is not
            # the previous session, that change spans days -- labelling it as a
            # 1-day move invents a story out of a gap. Flag it instead.
            if span > 4:
                rec["stale"] = True
                rec["span_days"] = span
                rec["note"] = f"change spans {span} days, not a single session"
            out[name] = rec
        except Exception as exc:
            out[name] = {"error": str(exc)[:120]}
    return out


def invalidation(close: float, sma20: float, low20: float, atr_val: float) -> float:
    """Nearest structural level below price, floored at 1 ATR of distance.

    Two failure modes this exists to kill, both found in the first dry run:
    every `pullback` candidate sits on its own 20-DMA, so "nearest level" yields
    a 0.03% stop that a normal session takes out by accident; and a name trading
    *under* its 20-DMA yields a level above price, which is not a stop at all.
    """
    below = [x for x in (sma20, low20) if x < close]
    level = max(below) if below else close - atr_val
    if close - level < atr_val:
        level = close - atr_val
    return level


def screen(symbols: list[str], chunk: int = 50) -> list[dict]:
    """Daily-bar technical screen over the F&O universe.

    Returns one row per liquid symbol with the levels an agent needs to reason
    about entries. Setup tagging happens here so the agent judges, not computes.
    """
    import yfinance as yf

    rows = []
    tickers = [f"{s}.NS" for s in symbols]
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        try:
            raw = yf.download(
                batch, period="1y", interval="1d",
                group_by="ticker", auto_adjust=False, progress=False, threads=True,
            )
        except Exception as exc:
            print(f"  batch {i//chunk} failed: {exc}", file=sys.stderr)
            continue

        for tkr in batch:
            try:
                d = raw[tkr].dropna()
            except (KeyError, TypeError):
                continue
            if len(d) < 60:
                continue

            c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
            close = float(c.iloc[-1])
            turnover_cr = close * float(v.iloc[-1]) / 1e7
            if turnover_cr < MIN_TURNOVER_CR:
                continue

            sma20, sma50 = float(c.rolling(20).mean().iloc[-1]), float(c.rolling(50).mean().iloc[-1])
            sma200 = float(c.rolling(200).mean().iloc[-1]) if len(d) >= 200 else None
            r = float(rsi(c).iloc[-1])
            a = float(atr(h, l, c).iloc[-1])
            volratio = float(v.iloc[-1] / v.rolling(20).mean().iloc[-1])
            hi20, lo20 = float(h.rolling(20).max().iloc[-1]), float(l.rolling(20).min().iloc[-1])
            hi52, lo52 = float(h.max()), float(l.min())
            pct1d = float(c.iloc[-1] / c.iloc[-2] - 1) * 100
            pct5d = float(c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(d) > 6 else None

            uptrend = close > sma20 > sma50
            setups = []
            if close >= hi20 * 0.99 and volratio > 1.5 and close > sma50:
                setups.append("breakout")
            if uptrend and 40 <= r <= 55 and abs(close / sma20 - 1) < 0.03:
                setups.append("pullback")
            if sma200 and close > sma20 > sma50 > sma200 and r > 60:
                setups.append("momentum")
            if close <= lo20 * 1.01 and volratio > 1.5 and close < sma50:
                setups.append("breakdown")
            if volratio > 3.0:
                setups.append("volume_spike")
            if not setups:
                continue

            stop = invalidation(close, sma20, lo20, a)
            trigger = hi20 if {"breakout", "momentum"} & set(setups) else close
            rows.append({
                "symbol": tkr.replace(".NS", ""),
                "close": round(close, 2),
                "open": round(float(d["Open"].iloc[-1]), 2),
                "high": round(float(h.iloc[-1]), 2),
                "low": round(float(l.iloc[-1]), 2),
                "prev_close": round(float(c.iloc[-2]), 2),
                "trigger": round(trigger, 2),
                "invalidation": round(stop, 2),
                "risk_pct": round((trigger - stop) / trigger * 100, 2),
                "risk_atr_mult": round((trigger - stop) / a, 2),
                "pct_1d": round(pct1d, 2),
                "pct_5d": round(pct5d, 2) if pct5d is not None else None,
                "rsi14": round(r, 1),
                "atr14": round(a, 2),
                "atr_pct": round(a / close * 100, 2),
                "vol_ratio": round(volratio, 2),
                "turnover_cr": round(turnover_cr, 1),
                "sma20": round(sma20, 2),
                "sma50": round(sma50, 2),
                "sma200": round(sma200, 2) if sma200 else None,
                "high_20d": round(hi20, 2),
                "low_20d": round(lo20, 2),
                "pct_from_52w_high": round((close / hi52 - 1) * 100, 1),
                "pct_from_52w_low": round((close / lo52 - 1) * 100, 1),
                "setups": setups,
                "as_of": str(d.index[-1].date()),
            })
    return sorted(rows, key=lambda x: -x["vol_ratio"])


def _records(obj, limit: int = 200):
    """Normalise whatever nsepython hands back into JSON-friendly records.

    It returns a DataFrame for some calls and a dict/list for others, and a
    stringified DataFrame reaches the agent truncated with '...' -- unreadable.
    """
    if isinstance(obj, pd.DataFrame):
        return obj.head(limit).to_dict(orient="records")
    if isinstance(obj, list):
        return obj[:limit]
    return obj


def flows() -> dict:
    """FII/DII cash activity. Always a PRIOR session -- there is no live morning number.

    The session date is lifted to the top of the block; buried inside the records
    (in a different date format from every other field) it reads as this morning's.
    """
    try:
        from nsepython import nse_fiidii
        recs = _records(nse_fiidii())
        session = recs[0].get("date") if isinstance(recs, list) and recs else None
        return {
            "session_date": session,
            "is_prior_session": True,
            "unit": "INR crore",
            "note": "prior session's activity, not today's",
            "fii_dii": recs,
        }
    except Exception as exc:
        return {"error": f"nsepython fiidii failed: {str(exc)[:150]}"}


def _agg_deals(df, fo_symbols: set[str]) -> dict:
    """Net one session's deal rows down to one record per symbol."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {"deals": [], "note": "empty response"}

    df = df.copy()
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["watp"] = pd.to_numeric(df["watp"], errors="coerce")
    df = df.dropna(subset=["qty", "watp"])

    total_rows = len(df)
    df = df[df["symbol"].isin(fo_symbols)]
    is_buy = df["buySell"].astype(str).str.upper().str.startswith("B")

    deals = []
    for sym, grp in df.groupby("symbol"):
        buy_side = is_buy.loc[grp.index]
        buy_qty = float(grp.loc[buy_side, "qty"].sum())
        sell_qty = float(grp.loc[~buy_side, "qty"].sum())
        net = buy_qty - sell_qty
        deals.append({
            "symbol": sym,
            "date": str(grp["date"].iloc[0]),
            "buy_qty": int(buy_qty),
            "sell_qty": int(sell_qty),
            "net_qty": int(net),
            "direction": "accumulation" if net > 0 else "distribution" if net < 0 else "crossed",
            "gross_value_cr": round(float((grp["qty"] * grp["watp"]).sum()) / 1e7, 2),
            "avg_price": round(float(grp["watp"].mean()), 2),
            "clients": sorted({str(c).strip() for c in grp["clientName"].head(8)}),
        })
    deals.sort(key=lambda d: -d["gross_value_cr"])
    return {
        "session_date": deals[0]["date"] if deals else None,
        "deals": deals,
        "rows_total": total_rows,
        "rows_outside_fo_universe": total_rows - len(df),
    }


def large_deals(fo_symbols: set[str]) -> dict:
    """Bulk and block deals for the last published session, F&O names only.

    Buys and sells are netted per symbol because a single block often prints as
    matched legs -- one seller, several buyers, at one price. Gross quantity
    makes that look like heavy accumulation; the net is what actually changed
    hands. Crossing trades net to roughly zero and should read that way.
    """
    from nsepython import nse_largedeals

    out = {}
    for kind in ("bulk_deals", "block_deals"):
        try:
            out[kind] = _agg_deals(nse_largedeals(kind), fo_symbols)
        except Exception as exc:
            out[kind] = {"error": f"{kind} failed: {str(exc)[:150]}"}

    out["note"] = (
        "Last published session, not today. F&O names only. 'crossed' means buys "
        "and sells netted out -- a matched block, not a directional bet."
    )
    return out


def events() -> dict:
    """Corporate calendar, split by what the board is actually meeting about.

    Most rows are fund-raising approvals and AGM housekeeping. Lumping them under
    'results_calendar' overstates the earnings count several-fold.
    """
    out = {}
    try:
        from nsepython import nse_events
        recs = _records(nse_events())
        if isinstance(recs, list):
            results = [r for r in recs if "financial result" in str(r.get("purpose", "")).lower()]
            out["results"] = results
            out["other_board_meetings"] = [r for r in recs if r not in results]
            out["counts"] = {"results": len(results), "other": len(recs) - len(results)}
        else:
            out["raw"] = recs
    except Exception as exc:
        out["error"] = str(exc)[:150]
    return out


def news(limit_per_feed: int = 25) -> dict:
    """Recent market headlines, hard-filtered by age.

    Returns the kept items plus a per-source audit so a feed that has quietly
    gone stale shows up as a zero instead of passing off old news as today's.
    """
    import calendar
    import html

    import feedparser

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=NEWS_MAX_AGE_HOURS)
    items, audit = [], {}

    for source, url in NEWS_FEEDS.items():
        kept = dropped_old = dropped_undated = 0
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit_per_feed]:
                stamp = entry.get("published_parsed") or entry.get("updated_parsed")
                if not stamp:
                    dropped_undated += 1
                    continue
                published = dt.datetime.fromtimestamp(calendar.timegm(stamp), dt.timezone.utc)
                if published < cutoff:
                    dropped_old += 1
                    continue
                items.append({
                    "source": source,
                    "title": html.unescape(entry.get("title", "")),
                    "link": entry.get("link", ""),
                    "age_hours": round((dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600, 1),
                    "_ts": published.isoformat(),
                })
                kept += 1
            audit[source] = {"kept": kept, "dropped_stale": dropped_old, "dropped_undated": dropped_undated}
        except Exception as exc:
            audit[source] = {"error": str(exc)[:120]}

    items.sort(key=lambda x: x["_ts"], reverse=True)

    # Feeds cross-post -- ET Markets and ET Stocks carry the same story. Left in,
    # one story counted three times looks exactly like a three-story sector
    # cluster, which is the thing Agent B is asked to detect.
    unique, seen_titles, duped = [], set(), 0
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen_titles:
            duped += 1
            continue
        seen_titles.add(key)
        del it["_ts"]  # sort key only; age_hours is what a reader needs
        unique.append(it)
    items = unique
    dead = [s for s, a in audit.items() if a.get("kept") == 0]
    return {
        "max_age_hours": NEWS_MAX_AGE_HOURS,
        "cross_posts_removed": duped,
        "items": items,
        "source_audit": audit,
        "dead_sources": dead,
    }


# --------------------------------------------------------------------------

def write_slices(payload: dict, stamp: str) -> dict[str, Path]:
    """One file per agent, carrying only that agent's inputs.

    Phase 2 scores a name on technical + catalyst + sector agreeing, which is
    only evidence if the three were reached independently. Handing all three
    agents the whole file breaks that: in the first dry run the technical agent
    reasoned off a news headline and the macro agent audited the news block,
    because the data was simply there. Separation belongs in the filesystem,
    not in a line of prompt asking nicely.
    """
    common = {"generated_ist": payload["generated_ist"], "as_of_note": payload["as_of_note"]}

    # Split by how the headlines get used. A tagged headline is a catalyst
    # candidate and needs its link for sourcing; an untagged one only feeds
    # theme clustering, where the title alone is enough and the URL is dead weight.
    news = payload["news"]
    tagged = [i for i in news["items"] if i.get("fo")]
    untagged = [{k: v for k, v in i.items() if k != "link"}
                for i in news["items"] if not i.get("fo")]

    slices = {
        "macro": {**common, "global": payload["global"], "india": payload["india"]},
        "flows": {**common,
                  "flows": payload["flows"],
                  "large_deals": payload["large_deals"],
                  "events": payload["events"],
                  "news_meta": {k: v for k, v in news.items() if k != "items"},
                  "tagged_headlines": tagged,
                  "other_headlines": untagged},
        "technical": {**common, "universe_size": payload["universe_size"],
                      "candidates": payload["candidates"]},
    }
    paths = {}
    for name, body in slices.items():
        p = DATA / f"slice_{name}_{stamp}.json"
        # indent=1: still readable when debugging, ~40% smaller than indent=2
        # across a few hundred news records.
        p.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
        paths[name] = p
    return paths


def run() -> Path:
    now = dt.datetime.now(IST)
    stamp = f"{now:%Y-%m-%d}"
    print(f"fetch start {now:%Y-%m-%d %H:%M} IST")

    print("  universe...")
    try:
        fo_map = fo_table()
    except Exception as exc:
        print(f"  F&O list failed ({exc}); aborting -- screen needs a universe", file=sys.stderr)
        raise

    universe = list(fo_map.keys())
    print(f"  {len(universe)} F&O symbols")
    print("  global quotes...")
    glob = quote_block(GLOBAL_TICKERS)
    print("  india indices...")
    india = quote_block(INDIA_TICKERS)
    print("  screening...")
    setups = screen(universe)
    print(f"  {len(setups)} candidates")
    print("  flows / events / news...")

    news_block = news()
    news_block["items"] = tag_news(news_block["items"], fo_map)
    tagged = sum(1 for i in news_block["items"] if i.get("fo"))
    news_block["tagged_count"] = tagged
    news_block["tagger_note"] = (
        "'fo' holds literal company-name matches, NOT verified catalysts. "
        "A trailing '?' means the match came from a single-word name and needs checking. "
        "'is_price_list' marks gainers/losers roundups, which name stocks without reporting an event. "
        "An untagged headline can still matter -- the tagger only knows F&O company names."
    )
    print(f"  {tagged}/{len(news_block['items'])} headlines tagged to F&O names")

    payload = {
        "generated_ist": now.isoformat(),
        "as_of_note": "bars are end-of-day; nothing here reflects tonight's US session or today's pre-open",
        "universe_size": len(universe),
        "global": glob,
        "india": india,
        "flows": flows(),
        "large_deals": large_deals(set(universe)),
        "events": events(),
        "candidates": setups,
        "news": news_block,
    }

    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"premarket_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    paths = write_slices(payload, stamp)
    print(f"wrote {path}")
    for name, p in paths.items():
        print(f"  slice {name}: {p.stat().st_size // 1024} KB")
    return path


def selftest() -> None:
    """Indicator math against hand-checkable series. No network."""
    # RSI of a monotonically rising series has no losses -> pins at 100.
    up = pd.Series(range(1, 40), dtype=float)
    assert abs(rsi(up).iloc[-1] - 100.0) < 1e-6, rsi(up).iloc[-1]

    # Monotonically falling -> pins at 0.
    down = pd.Series(range(40, 1, -1), dtype=float)
    assert abs(rsi(down).iloc[-1] - 0.0) < 1e-6, rsi(down).iloc[-1]

    # Flat series -> RSI undefined (0/0), must not raise.
    flat = pd.Series([100.0] * 30)
    assert pd.isna(rsi(flat).iloc[-1]) or 0 <= rsi(flat).iloc[-1] <= 100

    # ATR of constant-range bars equals that range.
    n = 30
    high = pd.Series([102.0] * n)
    low = pd.Series([98.0] * n)
    close = pd.Series([100.0] * n)
    assert abs(atr(high, low, close).iloc[-1] - 4.0) < 0.01, atr(high, low, close).iloc[-1]

    # Gapping bars: true range must use the prior close, not just high-low.
    high = pd.Series([102.0, 120.0])
    low = pd.Series([98.0, 118.0])
    close = pd.Series([100.0, 119.0])
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    assert abs(tr.iloc[-1] - 20.0) < 1e-6, tr.iloc[-1]

    # -- invalidation: both dry-run failure modes must be gone --
    # Normal case: sma20 sits a healthy distance below, so it is the stop.
    assert invalidation(close=100, sma20=95, low20=90, atr_val=2.0) == 95

    # Pullback degeneracy: price resting on its 20-DMA must not yield a ~0 stop.
    lvl = invalidation(close=100, sma20=99.97, low20=90, atr_val=2.0)
    assert 100 - lvl >= 2.0, lvl

    # Price below its 20-DMA must never return a "stop" above price.
    lvl = invalidation(close=100, sma20=104, low20=90, atr_val=2.0)
    assert lvl < 100, lvl

    # No structural level below price at all -> fall back to an ATR stop.
    lvl = invalidation(close=100, sma20=110, low20=105, atr_val=3.0)
    assert abs(lvl - 97.0) < 1e-9, lvl

    # -- news tagger: the probe's false positives must stay dead --
    fo = {
        "BANKINDIA": "Bank of India",
        "PERSISTENT": "Persistent Systems Limited",
        "MRF": "MRF Limited",
        "HDFCBANK": "HDFC Bank Limited",
        "DIXON": "Dixon Technologies (India) Limited",
    }

    def syms_of(title, table):
        item = tag_news([{"title": title}], table)[0]
        return {s.rstrip("?") for s in item.get("fo", [])}

    def syms(title):
        return syms_of(title, fo)

    # "Bank of India" must not reduce to "Bank" and match every banking headline.
    assert "BANKINDIA" not in syms("Bank stocks rally as RBI holds rates")
    assert "BANKINDIA" in syms("Bank of India posts higher Q1 profit")
    # Peeling the generic tail must not leave "Bank of" and catch a rival bank.
    assert "BANKINDIA" not in syms("Bank of Baroda posts higher Q1 profit")

    # NSE's abbreviated underlying never matches press usage, so the symbol
    # itself has to carry the match.
    assert "DIXON" in tag_news(
        [{"title": "Dixon Technologies gains on order win"}],
        {"DIXON": "DIXON TECHNO (INDIA) LTD"},
    )[0]["fo"][0]

    # A short symbol counts only in full caps.
    bel = {"BEL": "BHARAT ELECTRONICS LTD"}
    assert "BEL" in syms_of("BEL wins defence order", bel)
    assert "BEL" not in syms_of("Bel Air property prices climb", bel)

    # A group name shared by several listed entities must tag none of them.
    bajaj = {"BAJFINANCE": "BAJAJ FINANCE LTD", "BAJAJ-AUTO": "BAJAJ AUTO LTD",
             "BAJAJFINSV": "BAJAJ FINSERV LTD"}
    assert syms_of("Bajaj Auto vs TVS Motor: Citi picks a winner", bajaj) == {"BAJAJ-AUTO"}
    assert syms_of("Bajaj group stocks rally", bajaj) == set()

    # Symbols carrying '&' or '-' are legal and must survive the universe filter.
    assert VALID_SYMBOL.fullmatch("M&M") and VALID_SYMBOL.fullmatch("BAJAJ-AUTO")
    assert not VALID_SYMBOL.fullmatch("") and not VALID_SYMBOL.fullmatch("&M")

    # -- large deals: a matched block must not read as accumulation --
    cols = ["symbol", "date", "buySell", "qty", "watp", "clientName", "name", "remarks"]
    rows = [
        # One seller, two buyers, same price: net zero, not heavy buying.
        ("PAYTM", "18-Aug-2026", "SELL", 100, 1500.0, "PROMOTER ENTITY", "", ""),
        ("PAYTM", "18-Aug-2026", "BUY", 60, 1500.0, "FUND A", "", ""),
        ("PAYTM", "18-Aug-2026", "BUY", 40, 1500.0, "FUND B", "", ""),
        # A genuine one-sided sale.
        ("TITAN", "18-Aug-2026", "SELL", 500, 3000.0, "FUND C", "", ""),
        # Outside the F&O universe -- must be excluded but still counted.
        ("SMALLCO", "18-Aug-2026", "BUY", 10, 10.0, "FUND D", "", ""),
    ]
    got = _agg_deals(pd.DataFrame(rows, columns=cols), {"PAYTM", "TITAN"})
    by_sym = {d["symbol"]: d for d in got["deals"]}

    assert by_sym["PAYTM"]["net_qty"] == 0
    assert by_sym["PAYTM"]["direction"] == "crossed"
    # Gross value counts every leg: (100 + 60 + 40) * 1500 = 300,000.
    assert abs(by_sym["PAYTM"]["gross_value_cr"] - 300000 / 1e7) < 1e-6
    assert by_sym["TITAN"]["direction"] == "distribution"
    assert by_sym["TITAN"]["net_qty"] == -500
    assert "SMALLCO" not in by_sym
    assert got["rows_total"] == 5 and got["rows_outside_fo_universe"] == 1
    # Biggest gross first: TITAN's 500 x 3000 outweighs PAYTM's 200 x 1500.
    assert [d["symbol"] for d in got["deals"]] == ["TITAN", "PAYTM"]

    assert _agg_deals(pd.DataFrame(columns=cols), {"PAYTM"})["deals"] == []

    # A single-word name only counts when capitalised like a proper noun.
    assert "PERSISTENT" in syms("Persistent Systems wins large deal")
    assert "PERSISTENT" not in syms("Analysts flag persistent margin pressure")

    # Real multi-word matches still land, including a parenthesised suffix.
    assert "HDFCBANK" in syms("HDFC Bank shares rise 2% from 52-week low")
    assert syms("Dixon Technologies gains on order win") == {"DIXON"}

    # Roundups name stocks without reporting an event -- flagged, not dropped.
    assert tag_news([{"title": "Top Gainers & Losers on 20 Aug: MRF, Dixon Technologies"}],
                    fo)[0].get("is_price_list")
    assert not tag_news([{"title": "MRF slips on margin concerns"}], fo)[0].get("is_price_list")

    # Confidence must survive into the compact form: bare words carry "?".
    assert tag_news([{"title": "Persistent Systems wins large deal"}], fo)[0]["fo"] == ["PERSISTENT"]
    assert tag_news([{"title": "MRF slips on margin concerns"}], fo)[0]["fo"] == ["MRF?"]

    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    selftest() if args.selftest else run()
