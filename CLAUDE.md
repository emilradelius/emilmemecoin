# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Two independent trading bots for one non-technical owner (Swedish retail, Stockholm timezone). Neither has ever been run against real credentials.

- **`memebot/`** — Telegram bot. Watches Solana meme coin traders across Pump.fun (on-chain), X/Twitter and AlphaLedger, and alerts when several independent good traders converge on the same token. Alerts-only / paper / live switchable.
- **`brokerbot/`** — Broker-connected auto trading for listed instruments (stocks, ETFs) via Saxo / IBKR / eToro, plus a news-driven strategy that uses Claude to assess headlines.

They are deliberately separate packages. Different venues, data, cost structure and realistic edge; merging them would produce a system wrong about both.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

pytest                                    # 295 tests, both projects
pytest tests/brokerbot/test_pine.py        # one file
pytest -k "test_fills_happen_at_next"      # one test
pytest tests/brokerbot -q                  # one project
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator.

### brokerbot CLI

```bash
python -m brokerbot.cli costs              # round-trip cost by broker profile
python -m brokerbot.cli noise --strategy price_vs_sma --runs 100
python -m brokerbot.cli backtest --csv data/x.csv --symbol X --strategy momentum
python -m brokerbot.cli walkforward --csv data/x.csv --strategy sma_crossover
python -m brokerbot.cli pine --traps       # TradingView Strategy Tester pitfalls
python -m brokerbot.cli preflight --broker paper --symbols VOLV-B.ST
python -m brokerbot.cli trial --broker paper --symbols VOLV-B.ST --days 7
python -m brokerbot.cli trial --report
```

Any command accepts `--synthetic` instead of `--csv` to run on generated data.

### memebot

```bash
python run.py --check                      # validate config and credentials
python run.py
python -m memebot.tools.whoami             # find your Telegram chat id
python -m memebot.tools.cost               # price X polling settings
python -m memebot.tools.readiness          # are paper results good enough to go live?
python -m memebot.tools.probe_schemas --all # verify live API shapes
```

All memebot behaviour is tuned in `config.yaml` (heavily commented, every threshold explained).

## Architecture

### memebot pipeline

```
sources → signal queue → consensus → safety gate → flow gate → alert gate → Telegram
                              ▲                                      │
                         clustering                             execution
                         trader scores                          (paper/live)
                                                                     │
                                                              exit monitor
```

One asyncio process, SQLite, no broker or external database. Wiring lives in `memebot/app.py`.

Two pieces carry most of the value and are easy to break:

- **`scoring/clustering.py` — Sybil collapse.** Wallets are collapsed into clusters (direct funding, shared funder, repeated co-occurrence) *before* being counted. Without it, one person with five wallets looks like five traders agreeing, which makes the entire premise trivially fakeable.
- **`scoring/safety.py` — the hard gate.** Fails **closed**: if a lookup fails and mint-authority revocation cannot be confirmed, the token is rejected. Unknown is unsafe.

`enrich/flow.py` (DexScreener) is a *confirmation* layer, not a consensus source, and can only reduce conviction or veto — never promote. DexScreener has no notion of *who* is trading, and counting anonymous volume as a trader's opinion would inflate conviction on wash-traded tokens.

### brokerbot

Most of the package is measurement, not execution. Sending orders is the easy part.

Three invariants keep backtests honest. **Do not regress these** — each has a test named after it:

1. **Fills occur at the next bar's open**, never the signal bar's close (`backtest/engine.py`). Strategies receive only history-to-date, so look-ahead is structurally unavailable rather than merely discouraged.
2. **Every backtest reports a buy-and-hold benchmark** run through the identical cost path, and beating it requires better *risk-adjusted* return, not just higher return (`backtest/metrics.py`).
3. **`costs.py` is not optional.** The same mean-reversion strategy returns +18.9% frictionless and −16.6% under eToro costs, with buy-and-hold beating both. Omitting costs does not overstate returns, it inverts the conclusion.

`cli.py noise` runs a strategy over zero-edge random walks to establish what apparent performance the method manufactures from nothing. It is the most useful tool here — `sma_crossover` beats buy-and-hold in 40 of 100 noise runs and `momentum` in 26, so a single asset beating it once is not evidence. The rate is strategy-specific: `price_vs_sma` manages only 8 of 100, so the same result means different things depending on which rule produced it. Re-measure rather than quoting these numbers; they move whenever the generator is recalibrated.

`data/synthetic.py` keeps `drift`, `volatility` and `trend_strength` orthogonal via two corrections documented in the file. Both were real bugs. Do not simplify that code without reading the comments. `ASSET_PRESETS` (`--preset crypto|crypto_noise|equity_index|single_equity`) carries calibrated settings per asset class — a noise baseline is only valid for the class it imitates, and equity settings badly understate crypto.

`PaperBroker` takes an optional `quotes` source (`data/yahoo.py`, an undocumented free endpoint) so the live path can be rehearsed without an account. Two limits are deliberate and should stay: `supports_live` remains `False`, because a free feed must never size a real order; and `last_price` caches what it fetched so the fill uses the same price the signal was computed from, rather than a quote that arrived in between.

### News (brokerbot/news/)

Pipeline order is chosen for cost as much as correctness:

```
ingest → deduplicate → resolve entity → classify (Claude) → signal
         ~free, most     ~free, most      only survivors pay
```

Running the model first costs roughly 50× more for identical output.

- **`models.py` — `first_seen_at`.** Publisher `published_at` timestamps get revised and backfilled. Both the strategy and backtester key on when *our* ingester saw the item, which is strictly later than real publication and so biases results against the strategy.
- **`classify.py`** uses `claude-opus-5` with a strict tool for schema-valid output, low effort, and the rubric prompt-cached. Claude scores materiality / novelty / direction / surprise / confidence — it is **never** asked whether to buy. Sizing, risk and timing stay in code. The spend cap is persisted to disk; an in-memory cap resets on every deploy and never binds.
- **`strategy/news_drift.py`** deliberately does not react to headlines. Institutional feeds arrive in ~25ms; retail cannot win that race. It waits for the market to confirm a story's direction before entering, then holds for a drift window in days. That confirmation step is what makes the system robust to the classifier being wrong — a misread headline produces no confirming move, so no trade follows.

## Current state

No credentials are configured anywhere. The paper path runs end-to-end with no account: `preflight --broker paper` is clean and `trial --broker paper` completes cycles, places dry-run orders and reconciles positions.

Known gaps, in order of how much they block progress:

1. **No broker adapter has been exercised against a live API.** Written against documented shapes; the sandbox had no credentials and blocked outbound calls. `preflight` reports which call fails. The paper broker does **not** reduce this risk — it never speaks a broker protocol.
2. **X account scores start empty** in memebot — they are learned from calls graded 24h later, so ~2 weeks of shadow mode are needed before they mean anything. `runtime.shadow_mode: true` is the default and should stay on.
3. **News entity universe is ~16 names** (`news/entities.py`). A story about a company not in that list is invisible.

## Working with the owner

Not a programmer. Explain in plain language, lead with the decision rather than the implementation, and say plainly when something cannot be done rather than hedging.

Two recurring themes worth holding onto:

- He is drawn to trading-bot content on TikTok and YouTube showing large returns. The measurement tools in this repo exist so claims like those can be checked rather than argued about. Run them rather than debating.
- Sequence before real money: shadow → paper (6–8 weeks, 30+ closed trades) → `readiness` → live at a fraction of paper size. Roughly two months. Say so when asked to shortcut it.

Docs written for him, not for Claude: `brokerbot/README.md`, `docs/SEVEN_DAY_TRIAL.md`, `docs/TUNING.md`, `docs/ALPHALEDGER.md`.

## Attribution

Git remote is `emilradelius/emilmemecoin`. The repo was empty at first push, so `main` and `claude/telegram-meme-coin-bot-4m9gqj` point at the same commit; push to both or the branches diverge.

`.gitignore` must keep the `data/` pattern anchored as **`/data/`**. Unanchored, it matches at any depth and silently excludes the `brokerbot/data/` source package — which is exactly what happened: the package was absent from a fresh clone and three test modules failed to import. Nothing warns you; `git status` stays clean.
