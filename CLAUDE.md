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
python -m brokerbot.cli history --symbols VOLV-B.ST --years 10  # export CSVs
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

### What the strategies actually score (measured 2026-09-16)

Ten years of split- and dividend-adjusted daily bars, six Stockholm large caps
plus OMXS30, under `nordic_equities` costs. Files in `data/history/`, exported
with `cli history`.

**Nothing in the library beats buy-and-hold. 0 of 28 symbol/strategy pairs.**
Buy-and-hold returned +480% on Volvo and +545% on Investor; the best strategy
result on either was +254%. Cost drag runs 30-70% of starting capital for
`price_vs_sma` and `mean_reversion`, which trade often enough that the broker
is the main beneficiary.

The steelman was tested and also fails. Trend rules claim to cut drawdowns
rather than to beat bull markets, and in the 2021-11 to 2023-01 bear window
`momentum` does beat buy-and-hold on 6 of 7 symbols. That result does not
survive contact with the repo's own tools:

- It is one window, chosen after seeing the data, in one market where all
  seven names fell together - closer to one observation than to seven.
- `cli noise` manufactures a buy-and-hold beat from pure randomness in 26 of
  100 runs for `momentum` and 40 of 100 for `sma_crossover`.
- `cli walkforward` on real OMXS30 data rates both `momentum` and
  `mean_reversion` **NOT robust**: 110% and 86% of in-sample return vanishes
  out-of-sample, profitable in only 40% of windows.

Re-run these before building on them; do not treat the numbers as settled.
The conclusion to carry forward is not "these particular parameters are wrong"
but that no edge has been demonstrated yet, so a paper phase run today would
be rehearsing the machinery rather than testing a strategy. That is still
worth doing - it is just not the same thing.

`PaperBroker` takes an optional `quotes` source (`data/yahoo.py`, an undocumented free endpoint) so the live path can be rehearsed without an account. Two limits are deliberate and should stay: `supports_live` remains `False`, because a free feed must never size a real order; and `last_price` caches what it fetched so the fill uses the same price the signal was computed from, rather than a quote that arrived in between.

### Time-series momentum - the one thing that survived (2026-09-16)

`strategy/tsmom.py` implements Moskowitz, Ooi & Pedersen to spec: sign of the
past 12-month return, position size inversely proportional to ex-ante
volatility (EWMA of daily returns, centre of mass 60 days, annualised by 261,
estimate from t-1 applied to t), monthly rebalance, long and short, costs
charged on turnover.

This is also the strategy the repo previously tested **wrongly**. Running
momentum on one stock against buy-and-hold of that stock asks whether
trend-following beats owning Volvo, which is not the claim. The claim is about
a diversified, volatility-scaled, long/short portfolio across asset classes.

Measured on 28 futures across 6 asset classes, 2016-2026 - entirely after the
paper was published, so genuinely out of sample:

| | CAGR | vol | Sharpe | maxDD |
|---|---|---|---|---|
| SPY buy & hold | +16.6% | 16.1% | 1.04 | 23.9% |
| TSMOM alone (10bps) | +4.8% | 11.7% | 0.41 | 20.4% |
| **70/30 SPY/TSMOM** | **+13.0%** | **11.6%** | **1.12** | **13.0%** |

**It does not beat buy-and-hold and is not meant to.** Its correlation with
SPY is -0.05, and that is the product: blended at 30% it cuts maximum drawdown
from 24% to 13% while improving Sharpe. Diversification is visible in the
components too - every individual asset class scores Sharpe 0.00-0.29 and the
combination scores 0.35, which is the effect working as described rather than
one lucky sleeve.

Three honest caveats, in order of importance:

1. **t-statistic is 1.21.** The usual bar is 2.0. Nine years is not enough to
   rule out chance, and this must not be presented as established.
2. A random-sign control beat it in 12% of 200 runs. Far better than TJR's 87%,
   but not conclusive.
3. The 12-month lookback works (+4.1%) while 1, 3, 6 and 24 months do not.
   That matches the paper, which is mild evidence it is real rather than fitted
   - but it is also exactly what cherry-picking looks like.

The robust findings here are the near-zero correlation and the drawdown
reduction, both estimated far more precisely than the mean return. If anything
in this repo is worth acting on, it is the blend, not the standalone strategy.

### TJR / ICT sweep-and-shift (measured 2026-09-16)

`strategy/tjr.py` mechanises the model taught by TJR (Tyler J. Riches):
Power-of-3 daily template, Asia range swept at the London or New York open,
market structure shift against the sweep, entry on the retracement into the
displacement's fair value gap, stop past the sweep extreme, target the
opposing external liquidity, 1% risk. Every discretionary term is pinned to
one written definition in the module docstring, chosen before any result was
seen, and the arbitrary ones are marked and adjustable.

`backtest/bracket.py` exists because the main engine fills at the next bar's
open and has no stops - run through it, a TJR setup exits five minutes after
being stopped, which measures the engine rather than the strategy. The bracket
engine resolves stop and target intrabar, gives every ambiguous bar to the
stop, charges slippage in ticks, and models futures costs per contract rather
than as a percentage of notional (one NQ contract is ~$480k of notional for
~$15 of round-turn friction; a percentage model invents a cost eighty times
the real one).

**Result: 60 days of 5-minute NQ and ES, 20 parameter variants, 18 lose
money.** The two that profit contradict each other - "NY killzone only" makes
+3.2% on NQ and -4.8% on ES; a fixed 3R target makes +4.4% on ES and -5.1% on
NQ. Same knob, opposite sign, which is what noise looks like.

The decisive test is in the scratch script `noise_tjr.py`, worth rebuilding if
lost: keep the model's entry times, stop distance, target distance and sizing,
and replace only the direction with a coin flip. **The coin did at least as
well as the model's own direction in 87% of 500 runs on NQ and 75% on ES.**
The sweep, the shift and the gap contributed nothing detectable - the P&L came
from the risk geometry alone.

Caveats that matter before anyone re-litigates this: 13-24 trades is a small
sample, 60 days is one regime, and this is the mechanical reading, not a
discretionary trader's. It does not show the idea is worthless. It does show
that *this* reading of it has no edge, and that a positive backtest found by
turning knobs on this data should be assumed to be noise until it survives the
coin-flip control.

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

Set up locally at `~/Desktop/emilmemecoin` with a venv at `.venv`; 312 tests
pass. No credentials are configured anywhere. The paper path runs end-to-end
with no account: `preflight --broker paper` is clean and `trial --broker paper` completes cycles, places dry-run orders and reconciles positions.

Known gaps, in order of how much they block progress:

1. **No broker adapter has been exercised against a live API.** Written against documented shapes; the sandbox had no credentials and blocked outbound calls. `preflight` reports which call fails. The paper broker does **not** reduce this risk — it never speaks a broker protocol.
2. **X account scores start empty** in memebot — they are learned from calls graded 24h later, so ~2 weeks of shadow mode are needed before they mean anything. `runtime.shadow_mode: true` is the default and should stay on.
3. **News entity universe is ~16 names** (`news/entities.py`). A story about a company not in that list is invisible.

## Working with the owner

Not a programmer. Explain in plain language, lead with the decision rather than the implementation, and say plainly when something cannot be done rather than hedging.

Two recurring themes worth holding onto:

- He is drawn to trading-bot content on TikTok and YouTube showing large returns. The measurement tools in this repo exist so claims like those can be checked rather than argued about. Run them rather than debating.
- Sequence before real money: shadow → paper (6–8 weeks, 30+ closed trades) → `readiness` → live at a fraction of paper size. Roughly two months. Say so when asked to shortcut it.

Docs written for him, not for Claude: `brokerbot/README.md`, `docs/PAPER_RUN.md`, `docs/SEVEN_DAY_TRIAL.md`, `docs/TUNING.md`, `docs/ALPHALEDGER.md`.

He asked about connecting Claude to TradingView (2026-09). The MCP bridge in
the article he found is read-only and cannot place an order; TradingView's own
broker integrations, Saxo included, are for trading by hand from a chart.
Automating through them means alert -> webhook -> third-party bridge -> broker,
which is three failure points more than talking to Saxo directly, as brokerbot
already does. `cli pine` is the part worth keeping: it exports a strategy to
Pine Script with commission and slippage pre-filled, so TradingView's tester
becomes an independent check on our backtester rather than a trading venue.

## Attribution

Git remote is `emilradelius/emilmemecoin`. The repo was empty at first push, so `main` and `claude/telegram-meme-coin-bot-4m9gqj` point at the same commit; push to both or the branches diverge.

`.gitignore` must keep the `data/` pattern anchored as **`/data/`**. Unanchored, it matches at any depth and silently excludes the `brokerbot/data/` source package — which is exactly what happened: the package was absent from a fresh clone and three test modules failed to import. Nothing warns you; `git status` stays clean.
