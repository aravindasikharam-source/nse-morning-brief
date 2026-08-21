# NSE Morning Brief — Runbook

Pre-market research pipeline for NSE F&O stocks. Intraday and 2–10 day swing.

**This is a screener, not a signal service.** It surfaces setups with evidence and
an invalidation level. Position sizing and the decision to trade are yours.

## Daily sequence (IST)

| Time | Step | Actor |
|---|---|---|
| 07:00 | `python fetch.py` writes `data/premarket_YYYY-MM-DD.json` | cron |
| 07:05 | Agents A, B, C run in parallel on that JSON | orchestrator |
| 07:25 | Synthesis and narrowing | orchestrator |
| 07:30 | Brief delivered | orchestrator |
| 09:00 | Optional refresh — actual gap vs plan | on demand |
| 09:15 | Market opens | you |

## Phase 0 — fetch

```bash
python fetch.py            # normal run
python fetch.py --selftest # indicator math only, no network
```

Universe: ~205 F&O-eligible stocks from NSE's lot-size file. Every F&O name sits
inside Nifty 500, so this doubles as the liquidity filter. Names below
₹25 cr daily turnover are dropped — the edge dies in the spread.

Setups tagged: `breakout`, `pullback`, `momentum`, `breakdown`, `volume_spike`.

**Known data limits** — the agents must respect these:
- Any index carrying `"stale": true` is a multi-week span, *not* a daily move.
  Only nifty50, banknifty, indiavix, nifty_it and nifty_pharma are true daily.
- `flows` is the *prior* session's FII/DII, dated in `flows.session_date`. There
  is no same-morning number.
- Bars are end-of-day. Nothing here reflects tonight's US session or today's
  pre-open. GIFT Nifty must be checked live at brief time.
- **There is no macro event calendar in the file.** RBI policy, CPI, Fed
  speakers and F&O expiry are Agent A's job to source live via WebSearch. Absent
  is not the same as none — never report "no events today" from this file.
- `news.dead_sources` lists feeds that returned nothing fresh. A non-empty list
  means today's catalyst coverage is thinner than usual; say so in the brief.
- `events` splits into `results` (actual earnings) and `other_board_meetings`
  (fund-raising, AGM housekeeping). Only the first is an earnings event.
- `large_deals` is the last *published* session, which can lag the flows date —
  NSE publishes bulk and block deals on different schedules. Each carries its
  own `session_date`; never assume they match.

## Phase 1 — three agents, parallel

All three are `general-purpose`. Each reads **only its own slice** and never
re-fetches prices — numbers come from the file, not from memory or the web.

| Agent | File | Size |
|---|---|---|
| A — Macro | `data/slice_macro_<DATE>.json` | ~4 KB |
| B — Flows | `data/slice_flows_<DATE>.json` | ~32 KB |
| C — Technical | `data/slice_technical_<DATE>.json` | ~24 KB |

The full `premarket_<DATE>.json` stays on disk for synthesis and the archive; no
agent reads it. This is not only about cost. Phase 2 scores a name on technical,
catalyst and sector *agreeing*, which is only evidence if the three were reached
independently. In the first dry run — one shared file — the technical agent
reasoned off a news headline and the macro agent audited the news block. Neither
disobeyed; the data was simply there. Separation lives in the filesystem now.

### Agent A — Macro

> Read `data/slice_macro_<DATE>.json`, blocks `global` and `india`.
> Report, in under 200 words:
> 1. What moved overnight — US close, Asia, DXY, US 10Y, Brent, USDINR — and why.
> 2. Transmission to Indian sectors. Crude up hurts OMCs, paints, aviation.
>    USDINR up helps IT and pharma. US 10Y up pressures rate-sensitives.
> 3. India VIX level and what it implies for position sizing.
> 4. Event risk today — RBI, CPI, Fed speakers, expiry.
> Any index flagged `stale` is a multi-week trend, not a daily move — label it
> that way or leave it out. Use WebSearch only for GIFT Nifty and breaking
> overnight news. Give a sector bias table: sector, direction, one-line reason.

### Agent B — Flows and catalysts

> Read `data/slice_flows_<DATE>.json`.
> `flows` and `events` are already structured — report them, do not analyse them.
> `tagged_headlines` carry an `fo` list of pre-matched NSE symbols. A trailing
> `?` means the match came from a single word and needs your check. Group names
> shared by several listed entities are already suppressed, so a Bajaj headline
> will not arrive pre-tagged to a specific Bajaj company.
> Report:
> 1. FII/DII net direction. State `flows.session_date` explicitly — it is the
>    prior session's, not today's.
> 2. Companies reporting today, from `events.results` only. `other_board_meetings`
>    is fund-raising and AGM housekeeping, not earnings.
> 2b. `large_deals` — bulk and block deals, already netted per symbol and filtered
>    to F&O names. Read `direction`, not `gross_value_cr`: `crossed` means the
>    buys and sells matched, so a ₹5,898 cr print moved no net stock and is not a
>    directional signal. What matters there is *who* — a promoter entity exiting
>    into institutional hands is a supply story even at net zero. An empty
>    `deals` list with a high `rows_outside_fo_universe` means the session's
>    deals were all in non-F&O smallcaps, which is normal for bulk deals.
> 3. Catalysts. Start from `tagged_headlines`: confirm or reject each `?` match,
>    and discard anything flagged `is_price_list` — a gainers roundup names a
>    stock without reporting an event. Then scan `other_headlines` for catalysts
>    on names the tagger could not match; it only knows F&O company names.
> 4. Sector news clusters — three or more stories on one theme. Cross-posted
>    duplicates are already removed, so a count of three is three real stories.
> Only list a catalyst you can point at a headline for. No inference. Return a
> table: symbol, catalyst, source, direction.

The tagger does recall, you do precision. It reads all 209 company names against
every headline and never gets bored; it also cannot tell a mention from an event.

### Agent C — Technical

> Read `data/slice_technical_<DATE>.json`, block `candidates`. Already screened
> and liquidity-filtered — do not re-fetch prices.
> Each candidate arrives with `trigger`, `invalidation`, `risk_pct` and
> `risk_atr_mult` already computed — do not re-derive them. The stop is the
> nearest structural level below price, floored at 1 ATR so it cannot land
> inside a single session's noise.
> For each candidate:
> 1. Reject noise: `atr_pct` above 6 is too wild for a 2–10 day swing; RSI above
>    75 is extended, not entering; `vol_ratio` below 1.0 has no confirmation.
> 2. Rank what survives by setup quality — volume confirmation, trend alignment
>    (close > sma20 > sma50 > sma200), proximity to the 20d high.
> 3. Flag any name whose `risk_atr_mult` is exactly 1.0 — that is the ATR floor
>    firing, meaning it had no usable structural level of its own.
> Use `open`/`high`/`low`/`prev_close` to judge gap potential.
> Separate intraday candidates (`volume_spike`, wide ATR, gap potential) from
> swing candidates (`momentum`, `pullback`, clean `breakout`). Top 10 max.

## Phase 2 — synthesis (orchestrator, not an agent)

A name reaches the focused list only on confluence:

```
technical setup (C) + catalyst today (B) + sector tailwind (A)
```

- 3 of 3 → focused list, maximum 5 names
- 2 of 3 → watchlist
- 1 of 3 → dropped

Every focused name ships with: setup, catalyst, trigger, invalidation, risk %.
A name with no invalidation level does not go on the list.

## Phase 3 — output

1. `briefs/brief_YYYY-MM-DD.md`
2. Push notification — top 5 plus a one-line macro read
3. NotebookLM: append the brief to the running "Trading Journal" notebook

## NotebookLM's role

Deliberately off the daily critical path — source upload and generation are too
slow for a 30-minute pre-market window.

| Use | Cadence |
|---|---|
| Archive each brief as a source, building a searchable record of past calls | daily |
| Audio Overview of the brief, for the commute | daily, optional |
| Deep-dive notebook per focused name — annual report, concall transcripts | weekly |
| Weekend review: last five briefs, what worked and what did not | weekly |

The weekend review is the highest-value one. Reviewing your own calls builds the
edge; the daily churn does not.

## Stack

Zero API keys. Everything below is free and unauthenticated.

| Layer | Tool |
|---|---|
| Prices, indices, FX, commodities | `yfinance` |
| F&O universe, FII/DII, results calendar | NSE lot-size CSV, `nsepython` |
| Technical screen | `pandas` — Wilder RSI and ATR, computed locally |
| News | Moneycontrol, ET Markets, Mint, Business Standard RSS |
| Scheduling | `scheduled-tasks` MCP |
| Deep research and archive | `notebooklm` skill |
| Filings and concall PDFs | `markitdown` MCP |
| Delivery | push notification, Artifact dashboard |

Chartink was evaluated and dropped — CSRF session scraping for a screen that
pandas does locally in fifteen lines. FRED was dropped; yfinance carries the US
macro tickers without a key.
