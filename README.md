# Stock Bot — Discord options copy-trader

Mirrors one Discord signal source's option trades onto a brokerage account:
entries, partial exits ("SOLD MAJORITY", leaving runners), stops and full
exits. Paper trading on Alpaca first; Robinhood later.

No decision engine, no learning. The only judgement in the system is a set of
hard-coded safety rules and turning the source's free-form messages into
structured actions. See [PLAN.md](PLAN.md) for the phased plan, decisions and
open questions.

## How it works

```
Discord channel ──> main.py (ingest) ──> stock_msg.db (messages)
                                            │
                                            ▼
                     pipeline.py: parser.py + attach.py [+ llm_extract.py]
                                            │
                        option_signals (entries) + signal_updates (follow-ups)
                                            │
                                            ▼
                     runner.py: rules.py ──> execution/alpaca.py ──> positions / orders
```

1. **Ingest** — every message in `SIGNAL_CHANNEL_ID` is stored with its
   reply-to id and attachments.
2. **Parse** — an entry (`$SPY / $754 PUTS / EXPIRATION 9/16/2026 / $.68 Entry`)
   becomes an `option_signals` row. Follow-ups become `signal_updates`:

   | action | example | effect |
   |---|---|---|
   | `price_update` | `$.99 HERE ON SPY PUTS / UP +48%` | none — status only |
   | `trim` | `SOLD MAJORITY ` | sell 75 % (rounded), keep the runner |
   | `close` | `ALL OUT`, `runners out` | sell everything left |
   | `stop` | `stop at .25` | tighten our stop (never loosen) |
   | `add` | `added at .28` | recorded, not executed yet |
   | `note` | `NAILED THAT SELLOFF` | none |

   Follow-ups are attached to their entry by reply → ticker/type in the text →
   same author within 10 min → only open signal. Anything ambiguous goes to a
   `needs_label` queue and is never traded.
3. **Execute** — the runner buys new entries at `min(ask, source × 1.10)`,
   applies trims/closes/stops, and enforces the safety rules below.

## Setup

Python 3.11. From the project root:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:

| variable | what |
|---|---|
| `DISCORD_MODE` | `bot` (a bot you invited) or `user` (your own account; against Discord ToS) |
| `DISCORD_BOT_TOKEN` / `DISCORD_USER_TOKEN` | the matching token |
| `SIGNAL_CHANNEL_ID` | the channel to listen on (Developer Mode → right-click channel → Copy ID) |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | **paper** keys from the Alpaca dashboard (Paper toggle on) |
| `ALPACA_PAPER_TRADE` | must be `true`; the broker class refuses otherwise |
| `RUNNER_ENABLED` | `false` = ingest + parse only (default). `true` = also trade |
| `NOTIFY_CHANNEL_ID` | optional Discord channel for fills / misses / halts |
| `ANTHROPIC_API_KEY` | optional; enables the LLM fallback for unparseable follow-ups |

Check the Alpaca account (options level must be ≥ 2):

```bash
.venv/Scripts/python -m tools.alpaca_check SPY 754 put 2026-09-17
```

## Running

```bash
.venv/Scripts/python main.py
```

One process: Discord listener + parser, and — if `RUNNER_ENABLED=true` — the
trading loop every `RUNNER_INTERVAL_SECONDS` (15). Migrations are applied on
start. Logs go to `stockbot.log`.

Recommended first day: run with the runner **off**, then review what was
captured:

```bash
.venv/Scripts/python -m tools.replay_parser --no-llm -v
```

## Tools

| command | purpose |
|---|---|
| `python -m tools.replay_parser [-v] [--apply] [--no-llm]` | re-run the parser over every stored message; dry-run by default |
| `python -m tools.label` | resolve the `needs_label` queue by hand |
| `python -m tools.alpaca_check [TICKER STRIKE call\|put YYYY-MM-DD]` | account, options level, contract lookup, quote + Greeks |
| `python -m tools.paper_trade open\|trim\|close\|status\|cancel\|positions …` | drive the paper broker manually |
| `python -m runner --once` | one runner tick from the command line |
| `python -m pytest` | tests (parser, pipeline, positions, runner with a fake broker) |

## Safety rules

All in `rules.py`, overridable via `.env`:

| rule | default |
|---|---|
| contracts per entry | 3 (so "sold majority" sells 2, keeps 1) |
| max open positions | 3 |
| entry chase | skip if ask > source entry + 10 % |
| entry order TTL | cancel after 5 min → `missed` |
| stale signal | skip entries first seen > 3 min old |
| liquidity | skip if spread > 25 % of mark or OI < 100 |
| hard stop | −50 % of our fill if the source never posts one |
| runner stop | after any trim, stop moves to break-even |
| time stop | flatten anything expiring today at 15:45 ET |
| daily loss | −$500 → halt, flatten, write `KILL` |
| kill switch | create a file named `KILL` in the project root; delete it to resume |

If the source exits before our entry fills, the entry is cancelled and the
trade marked `missed` — we don't chase a trade that's already over.

## Layout

```
main.py            Discord client; ingest + runner loop
parser.py          text -> ParsedSignal / ParsedUpdate (no DB)
attach.py          which open signal a follow-up belongs to
pipeline.py        parse -> attach -> store / needs_label / ignore
llm_extract.py     optional Haiku fallback for unmapped follow-ups
positions.py       fraction -> contracts, runner rule, stop tightening
rules.py           safety rules + sizing (env-configurable)
runner.py          the trading loop
execution/         ExecutionBroker interface, AlpacaPaperBroker
database.py        SQLite schema, migrations, queries
tools/             replay_parser, label, alpaca_check, paper_trade
tests/             pytest suite (FakeBroker in tests/fake_broker.py)
PLAN.md            phased plan, decisions, open questions
```

## Data

`stock_msg.db` (SQLite, gitignored): `messages`, `option_signals`,
`signal_updates`, `needs_label`, `positions`, `orders`, `schema_version`.
