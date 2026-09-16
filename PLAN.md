# Stock Bot — Copy-Trader Build Plan

**Audience:** implementation brief for Claude Fable 5.1 (or whichever agent picks up the work). Describes what exists, what to build, in what order, and the guardrails that must never be relaxed.

**Scope (locked 2026-09-16):** this repo is a **copy-trader**. It mirrors one Discord signal source's option trades — entries, partial exits ("sold majority, leaving runners"), profit-taking ("took 50% off at +80%"), stops, and full exits — first on a paper account, later on a Robinhood agentic account. **No decision engine, no learning, no autonomous trading.** The only "judgement" in the system is (a) a small set of hard-coded safety rules and (b) turning the source's free-form exit messages into structured actions.

**Last updated:** 2026-09-16

---

## 0. Current state (verified from the repo + DB)

| Piece | File | Status |
|---|---|---|
| Discord ingestion (bot or self-bot mode, optional channel filter) | `main.py` | Working |
| SQLite schema: `messages`, `option_signals`, `paper_trades` | `database.py` | Working; `paper_trades` never written to |
| Regex parser for `$TICKER / $STRIKE CALLS / EXPIRATION m/d/y / $x.xx Entry` (`mike_v1`) | `parser.py` | Entries only |
| Data on disk | `stock_msg.db` | 185 messages, 4 entry signals (all SPY calls), 181 ignored |

**Gaps that block copy-trading:**

1. **Exits are not parsed at all.** `action` is only ever `buy`/`unknown`. Without exits the bot can open positions and never close them.
2. **No link from a follow-up message to its signal.** `reply_to_id` is stored but unused.
3. **No expiration fallback** when the `EXPIRATION` line is missing (these look like 0DTE plays).
4. **No contract resolution** — `(ticker, type, strike, expiration)` must become a broker contract symbol before an order can be placed.
5. `init_db()` drops a table on every start; no migrations; no tests.

---

## 1. Phases

```
Phase 1  Ingest + parse ENTRY signals              DONE
Phase 2  Parse EXIT / update signals + position model   <- the real work
Phase 3  Paper execution on Alpaca (free paper account)
Phase 4  Live execution on the Robinhood agentic account
```

Each phase has an exit criterion. Don't start the next until it's met.

---

## 2. Phase 2 — Exit-signal parsing + position model

**Goal:** every message from the source that changes a position becomes a structured `signal_update` row attached to the right open signal.

### 2.1 Action vocabulary

The source's exits are free-form ("sometimes / inconsistent wording" — your answer). Normalise everything to this small set:

| `action` | Fields | Example source phrasing (real, from the channel) |
|---|---|---|
| `entry` | ticker, type, strike, expiration, limit_price, `underlying_pt` | `$SPY $754 PUTS EXPIRATION 9/16/2026 $.68 Entry … seeking for these to go ITM with a $755 PT` |
| `price_update` | `price`, `pct_gain` | `$.99 HERE ON SPY PUTS / UP +48%`, `$1.25 HERE ON SPY PUTS / UP +85%` — **informational only, never an order** |
| `trim` | `fraction` (0–1) **or** `contracts`, optional `price` | `SOLD MAJORITY 🚨🚨🚨` (→ fraction 0.75 default, Q1), "sold half", "took 1/3 off at .60" |
| `close` | optional `price` | "out", "all out", "closed the rest", "runners out" |
| `stop` | `stop_price` **or** `stop_pct` | "stop at .25", "cut if it loses .30", "SL 20%" |
| `add` | contracts or fraction, price | "added at .28", "DCA'd" |
| `note` | — | `NAILED THAT SELLOFF!!!`, `$755/$754 PT RANGE HITTING`, image-only posts — stored, no action |

Observed lifecycle for one signal (2026-09-16, SPY 754P 0DTE):

```
14:2x  entry            $.68, PT $755 on SPY
14:35  price_update     $.99  +48%      (reply to entry)
14:35  trim             SOLD MAJORITY   (NOT a reply — standalone message, same author, 0-60 s later)
14:37  price_update     $1.25 +85%      (reply to entry)  <- runners still on
14:38  note             NAILED THAT SELLOFF + image
14:54  note             PT range hitting
14:56  price_update     $1.36 +100%     (reply to entry)
  …    no explicit close seen -> runners ride to expiry; our time-stop closes them
```

Rules that fall out of this:

- **"UP +X%" is a status, not a profit-take.** Never map `pct_gain` to a sell.
- A `trim` with no fraction word beyond "majority" / "some" / "most" uses the default fraction; "half", "1/3", "2/3", "all" are exact.
- A `trim` is only valid on an open position with `remaining_qty > 1`; otherwise it's recorded and ignored (can't sell "majority" of 1 contract).
- No explicit `close` before expiry is normal for this source → the **time-stop** (section 5) is the real exit for runners, and must run on every open position.
- The source posts a `price_update` every ~2 min while active; > 20 min of silence on an open position is a useful "probably done" hint for the notification channel (not an order trigger).

A message that mentions a trade but can't be mapped confidently becomes `needs_label` (see 2.3) and is **never** executed.

### 2.2 Two-stage parser

1. **Regex first** (`parser.py`): extend `mike_v1` → `mike_v2` with patterns for the vocabulary above. Fast, deterministic, free; expected to catch the common phrasings. Starting patterns from the observed messages:
   - price_update: `^\$?(\d*\.\d+)\s+HERE ON\s+([A-Z]{1,5})\s+(CALLS|PUTS)` + `UP\s+\+?(\d+)%`
   - trim: `\bSOLD\s+(MAJORITY|MOST|SOME|HALF|1/3|2/3|A FEW)\b`, `\b(TRIMMED|TOOK)\s+(HALF|1/3|SOME|\d+%)?\s*(OFF)?`
   - close: `\b(SOLD|CLOSED)\s+(ALL|THE REST|RUNNERS?)\b`, `\b(ALL\s+)?OUT\b` (word-boundary, author-only, so "seeking" text doesn't trip it)
   - underlying PT on entry: `\$(\d+(?:\.\d+)?)\s*PT\b`
   - Strip emoji before matching; keep the raw text in `raw_excerpt`.
2. **LLM extraction fallback** for messages that (a) come from the signal author, (b) fail regex, and (c) look trade-related (mention a ticker, a price, or words like sold/out/trim/stop/runner). One call to `claude-haiku-4-5` (cheap, fast) with structured output (`output_config.format`) returning `{action, fraction, price, pct_gain, stop_price, confidence, target_signal_hint}`. Below a confidence threshold (0.8) → `needs_label`.

   This is *parsing*, not a decision engine: the model never chooses whether to trade, only what the human said. Cost: fractions of a cent per message.

3. **Signal attachment**: an update is linked to a signal by, in order:
   1. `reply_to_id` → the entry message it replies to (the source replies to the entry for price updates — primary path).
   2. Ticker + type named in the message (`… ON SPY PUTS`) → the open position matching it.
   3. Standalone message from the signal author (e.g. `SOLD MAJORITY`) within 10 min of that author's last update → the position that update belonged to. This is how the non-reply trim in the observed lifecycle gets attached.
   4. Exactly one open position for that author → that one.
   5. Otherwise → `needs_label`. Multiple open positions + ambiguous message → `needs_label`, never a guess.

   Discord's "grouped" rendering (no header on consecutive messages) is cosmetic — each is still a separate message with its own id and `reply_to_id` (`None` for the grouped ones), so `main.py` already captures what's needed. Also store `attachment_urls` on `messages`: image-only posts currently arrive with empty `content`.

### 2.3 `needs_label` queue

A table + a tiny CLI (`python -m tools.label`) that shows unlabeled messages and lets you assign `{signal_id, action, fraction, price}` in a couple of keystrokes. Every label is also saved as a regex/LLM test fixture so the parser stops needing it next time.

### 2.4 Position model

The source's position and ours are different sizes, so fractions must be translated:

- We hold `qty` contracts per signal. `trim fraction=f` → sell `round(qty * f)`, but **never** sell the last contract on a `trim` (that's what "runners" means); only `close` sells the last one.
- With `qty = 1` a `trim` becomes a no-op *note* (can't sell half a contract) — the position is closed on the next `close`. This is why **sizing must be ≥ 2 contracts** for partial-exit copying to mean anything (Q2).
- `take_profit pct_gain=X` with no fraction: treat as `trim` with the default fraction (Q1).
- `stop stop_pct=X`: converted to an absolute price from *our* fill, not the source's entry.
- Every position keeps a running `remaining_qty`, `avg_cost`, `realized_pnl`.

### 2.5 Schema

```sql
signal_updates(id, signal_id, message_id, action, fraction, contracts, price,
               pct_gain, stop_price, confidence, source  -- 'regex' | 'llm' | 'manual'
               parser_version, created_at)

positions(id, signal_id, broker, contract_symbol, qty_opened, remaining_qty,
          avg_cost, realized_pnl, status  -- 'open' | 'closed' | 'error'
          opened_at, closed_at)

orders(id, position_id, update_id, broker, broker_order_id, side, qty,
       order_type, limit_price, status, fill_price, error, submitted_at, filled_at)
       -- replaces paper_trades

needs_label(id, message_id, reason, created_at, resolved_at)
schema_version(version, applied_at)
```

Plus Phase-1 fixes: remove the `DROP TABLE`, add a migration runner, `expiration_inferred` flag on `option_signals`.

### 2.6 Tooling

- `python -m tools.replay_parser` — re-run the parser over every stored message; prints a diff against the previous parser version. This is how parser changes get validated against the 185 messages already captured (and any backfill, Q5).
- `tests/test_parser.py` — real message shapes from the DB: entries, each exit phrasing, and noise. Minimum fixture set from the observed lifecycle above: the entry with PT, three `price_update`s (must NOT produce orders), `SOLD MAJORITY` (trim 0.75, attached via rule 3), the two notes, and an image-only message.

**Exit criterion:** replaying the stored history yields the correct `signal_updates` for every entry that had follow-ups, zero false positives on noise, and the `needs_label` queue is empty after one manual pass.

### 2.7 Status (2026-09-16) — built, awaiting real channel data

| Item | File | State |
|---|---|---|
| Migration runner + v2 schema (`signal_updates`, `needs_label`, `schema_version`, new signal columns, `attachment_urls`) | `database.py` | done, applied to `stock_msg.db` |
| Parser v2: entries (+AVG, +PT, 0DTE fallback), `price_update`/`trim`/`close`/`stop`/`add` | `parser.py` | done |
| Attachment rules 1–5 | `attach.py` | done |
| Pipeline (entry → update → attach → store / needs_label / ignore) | `pipeline.py` | done; `main.py` routes through it and stores attachment URLs |
| Haiku extraction fallback | `llm_extract.py` | written; **not exercised live** (no `ANTHROPIC_API_KEY` yet). Degrades to `needs_label` without a key. |
| Position model (fraction → contracts, runner rule, stop tighten-only) | `positions.py` | done, pure functions |
| Replay + label CLIs | `tools/replay_parser.py`, `tools/label.py` | done |
| Tests | `tests/` | 85 passing; fixtures are the observed 9/16 lifecycle + noise |

Replay over the 185 stored messages: 4 entries, 181 ignored, 0 false positives — identical to v1, as expected since no real follow-ups have been captured yet. **The exit criterion cannot be met until the bot has run on the live channel** and captured Mike's actual follow-ups; expect the first real day to put a few messages into `needs_label`, which then become new regex cases + test fixtures.

---

## 3. Phase 3 — Paper execution on Alpaca

**Goal:** the structured signals from Phase 2 place real paper orders, end to end, unattended during market hours.

**Broker:** Alpaca free paper account, options enabled. Since there is no LLM in the trade path, use the official **`alpaca-py`** Python SDK directly from the service — no MCP, no agent. (The Alpaca MCP server exists and is fine for poking at the account by hand from Claude, but it's the wrong tool for a headless copy-trader.)

### 3.1 Execution layer

```
stockbot/
  ingest/      main.py, parser.py, llm_extract.py      (Phase 1-2)
  positions/   model.py, sizing.py                    (Phase 2.4)
  execution/
    base.py       ExecutionBroker: resolve_contract, place, cancel, get_order, positions
    alpaca.py     AlpacaPaperBroker (alpaca-py, paper=True)
    robinhood.py  RobinhoodBroker (Phase 4)
  rules.py       hard-coded safety rules (section 5)
  runner.py      the loop: new signal_update -> rules -> broker -> orders/positions rows
  tools/         replay_parser.py, label.py
  db/            schema.sql, migrations/
```

### 3.2 Order mapping

| Signal | Order |
|---|---|
| `entry` with limit | buy-to-open, limit = source's entry price (+ configurable tolerance, e.g. +10 %); GFD; if unfilled after N minutes → cancel and mark `missed` |
| `entry` without limit | buy-to-open, limit = current ask |
| `trim` / `take_profit` | sell-to-close `round(qty*f)`, limit = current bid (or marketable limit) |
| `close` | sell-to-close `remaining_qty`, marketable limit |
| `stop` | store on the position; `runner.py` polls the quote and sends a sell-to-close when breached (Alpaca options do not support native stop orders on every contract — check per account) |
| `add` | buy-to-open, same sizing rule capped by max position size |

Contract resolution: `(ticker, type, strike, expiration)` → OCC symbol (`SPY260918C00742000`) via `alpaca-py`'s option-contracts endpoint; cache in `option_contracts`.

Optional, cheap, and useful for P&L review: snapshot the contract's quote + Greeks at entry and at each exit (`alpaca-py` option snapshot). Record-only — nothing reads it in this repo.

### 3.3 Runtime

- `runner.py` is a single long-running process on your machine (Q4) that: keeps the Discord client alive, drains new `signal_updates`, applies rules, talks to the broker, polls open positions every ~15 s for stops and the time-stop.
- Discord notifications back to you (a private channel or DM) for every order placed/filled/rejected and every `needs_label` — this is the human-in-the-loop channel.

**Exit criterion:** 4+ weeks of paper with every source entry mirrored (or explicitly `missed` with a reason), every source exit mirrored within 60 s, no position ever reaching expiry unmanaged, `needs_label` handled same-day.

---

## 4. Phase 4 — Live on the Robinhood agentic account

Same `runner.py`, `RobinhoodBroker` swapped in. **Open problem:** Robinhood has no official Python SDK; the agentic account is reachable only through the Robinhood MCP connector, which is a Claude tool surface. Options, in order of preference:

1. A minimal executor built on the **Claude Agent SDK** with the Robinhood MCP attached, whose *only* job is "place this exact order I hand you, review first, report the result." It makes no trading decisions — it's a typed bridge. Adds an LLM call per order (~cents) and some latency (seconds).
2. Wait for / check whether Robinhood publishes a direct API for agentic accounts.
3. Unofficial `robin_stocks` — not recommended (ToS, MFA, breakage).

Non-negotiables, all phases but enforced hardest here:

- First N live trades at **1 contract**, hard-coded (which means no partial exits are mirrored — accepted).
- **Entry approval** for the first two weeks: bot posts the proposed order to Discord, you react ✅ within the fill window, then it executes. Exits stay automatic.
- **Kill switch** (file flag / Discord command) that cancels open orders, flattens, and stops the loop.
- **Daily loss cap** trips the kill switch.
- `review_option_order` before every `place_option_order`; idempotent `ref_id` per logical order; every attempt/reject/fill written to `orders`.

**Exit criterion:** your call, from the P&L and incident log.

---

## 5. Safety rules (`rules.py`) — mechanical, never overridden

| Rule | Default |
|---|---|
| Max contracts per position | from Q2 |
| Max concurrent positions | 3 |
| Max daily loss → kill switch | from Q2 |
| Hard stop if the source never posts one | −50 % of our fill |
| Runner stop (after any `trim`) | sell remaining at break-even (our avg cost) if price falls back to it; unknown how the source manages runners, so we don't let them go red |
| Time stop | close 15 min before expiry cutoff (Robinhood sellout / Alpaca 3:30 ET on 0DTE) |
| Entry tolerance | skip if ask > source entry × 1.10 (don't chase) |
| Stale signal | skip entries older than 3 min by the time they're parsed |
| Illiquid contract | skip if bid/ask spread > 25 % of mark or OI < 100 |
| Parser confidence | `< 0.8` → `needs_label`, never trade |
| Duplicate protection | one open position per `signal_id`; `INSERT OR IGNORE` on `discord_id` already handles re-delivered messages |

---

## 6. Decisions made

| Question | Decision |
|---|---|
| Scope | Copy-trader only. No decision engine, no learning, no autonomous signals. (Revised 2026-09-16; earlier draft had an LLM decision engine — removed.) |
| Paper broker | Alpaca free paper account via `alpaca-py` (not the MCP — there's no agent in the loop now). |
| Exit phrasing | Inconsistent → regex first, Haiku extraction fallback, `needs_label` queue for the rest. |
| Live broker | Robinhood agentic account; access path is the open item in section 4. |

## 7. Open questions

**Q1. Runner handling.** `SOLD MAJORITY` = 0.75 — **decided**. How the source manages runners is **unknown** (no explicit close seen yet). Until the data says otherwise, runners are managed by us with three mechanical rules, in priority order: (1) an explicit `close` from the source, (2) a **runner stop** at break-even (our avg cost) once a trim has happened — a runner should never turn a winner into a loser, (3) the time-stop. Log every runner exit with its reason so after a few weeks of paper you can see whether the source posts closes and adjust. Add `runner_exit_reason` to `positions`.

**Q2. Sizing & risk.** Contracts per entry (needs ≥ 2 for partial exits to be mirrored; 3–4 lets "1/3 off" work), max $ per position, max concurrent positions, daily loss cap. Paper first, but pick the numbers you'd actually run live so the paper results mean something.

**Q3. Entry fill policy.** If our limit at the source's entry price doesn't fill, chase up to +10 % or skip? How long to leave the order working?

**Q4. Hosting.** Which machine runs `runner.py` 6:30–13:00 PT on trading days? If it's your PC, it needs to be on and awake.

**Q5. Backfill.** Pull the channel's history so the parser can be validated on more than 185 messages before paper starts?

**Q6. Expiration fallback.** No `EXPIRATION` line → nearest expiry (0DTE)?

**Q7. Discord mode.** Self-bot mode (`DISCORD_MODE=user`) is against Discord's ToS. Can a real bot be added to the signal server?

**Q8. Multiple callers?** Only `mike_v1` today. Other authors in the channel → per-author parser versions and an author allow-list.

## 8. Assumptions

- Long-only single-leg options (buy calls / buy puts). Never sell to open.
- US equity/index options, regular hours only.
- One signal source, one paper account, one Robinhood account.
- SQLite is fine at this volume.
- Python 3.11. New deps: `alpaca-py`, `anthropic` (Haiku extraction only).
