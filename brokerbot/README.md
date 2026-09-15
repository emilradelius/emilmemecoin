# brokerbot

An automated trading bot for **listed instruments through a regulated broker** —
stocks, ETFs, funds. Separate from `memebot/`, which trades Solana meme coins
on decentralised exchanges. Different venues, different data, different costs,
different realistic edge.

**Most of this project is the part that tells you whether a strategy works.**
Connecting to a broker and sending orders is the easy 20%. The other 80% —
costs, look-ahead prevention, walk-forward validation, noise calibration — is
what stands between "my bot made money last week" and knowing anything.

---

## Which broker

You're in Sweden, which rules out more than you'd expect:

| Broker | API | Available to you | Verdict |
|---|---|---|---|
| **Saxo Bank** | OpenAPI, REST + streaming | ✅ | **Best fit.** Free simulation environment, identical API |
| **Interactive Brokers** | Client Portal Web API | ✅ | Cheapest to trade. Gateway needs daily manual re-auth |
| **eToro** | Public API (Apr 2026) | ✅ | Easiest access, ~5× the trading costs |
| Nordnet | External API exists | ⚠️ | **Not onboarding new API customers** |
| Robinhood | Crypto API | ❌ | **US-only API**, even though the app works here |
| Avanza | None | ❌ | No public API exists |

Adapters are included for Saxo, IBKR, eToro, and a **paper broker that needs no
account at all** — so you can run the whole system before opening anything.

**Start with Saxo simulation.** A 24-hour developer token, free, identical API
surface to live. Nothing else lets you build against the real thing at zero risk.

---

## The costs, which decide everything

```bash
python -m brokerbot.cli costs
```

```
preset                       10,000     50,000
----------------------------------------------
nordic_equities              0.48%     0.48%
us_equities_from_sek         0.93%     0.93%
ibkr_us                      0.25%     0.25%
etoro                        1.25%     1.25%
```

A round trip through eToro costs **5× what it costs through IBKR**. Trade
weekly and that's ~65% a year in gross edge just to break even.

Here's the same mean-reversion strategy on identical data, varying only costs:

| Costs | Strategy return | Buy & hold |
|---|---|---|
| Zero (naive backtest) | **+18.9%** | +111.7% |
| IBKR | +9.2% | +111.4% |
| Nordic broker | +2.6% | +111.2% |
| eToro | **−16.6%** | +110.4% |

A strategy that looks profitable frictionless is *losing money* at realistic
costs — and holding the instrument beat all four. Backtests that skip costs
don't just overstate returns, **they invert the conclusion.**

---

## The three honesty mechanisms

### 1. Fills happen at the next bar's open

A strategy deciding on Monday's close and filling at Monday's close is trading
on information it didn't have. That single mistake explains a large share of
backtests that look extraordinary and then fail live. Here, a signal from bar
*i* fills at bar *i+1*'s open, and strategies receive only history — peeking
isn't discouraged, it's structurally unavailable.

### 2. Every result is compared to buy-and-hold

Not optional. If a strategy doesn't beat holding the same instrument over the
same period after costs, it's an elaborate way to pay commission. Beating it on
return alone isn't enough either — it must also not have taken more risk to get
there.

### 3. Noise calibration

```bash
python -m brokerbot.cli noise --strategy sma_crossover --runs 100
```

Runs the strategy on random walks containing **zero real signal**:

```
  best        +92.9%
  90th pct    +44.3%
  median      -11.1%
  worst       -36.3%

  beat buy-and-hold in 20/60 runs by luck alone
```

On pure noise, the best run returns +92.9%, and the strategy beats holding a
third of the time by chance. **That's the bar your real backtest has to clear**
to mean anything — anything under +44% here is indistinguishable from luck.

Nothing else in this repo will save you as much money as internalising that.

---

## Walk-forward validation

Try 200 parameter combinations on ten years of data and the best one looks
brilliant even if every rule is worthless. Walk-forward splits history into
windows, picks parameters on the earlier part, and measures them on the later
part they never saw:

```bash
python -m brokerbot.cli walkforward --synthetic --strategy sma_crossover
```

```
window     in-sample  out-sample  parameters
1             -1.6%      -3.5%  fast=50, slow=60
3             31.2%       2.4%  fast=5, slow=30
4             25.5%      23.3%  fast=10, slow=30

Degradation:  59%
VERDICT: NOT robust - 59% of in-sample returns vanish.
```

Window 3 is the lesson: +31.2% in-sample becomes +2.4% out. That's what
overfitting looks like, and this is the cheap way to find it.

---

## Usage

```bash
python -m brokerbot.cli costs                                  # the hurdle
python -m brokerbot.cli noise --strategy momentum              # the baseline
python -m brokerbot.cli backtest --csv data/volvo.csv --symbol VOLV-B.ST \
       --strategy sma_crossover --costs nordic_equities
python -m brokerbot.cli walkforward --csv data/volvo.csv --strategy momentum
python -m brokerbot.cli broker --check saxo                    # SAXO_TOKEN env var
```

**Data:** CSV is the primary path — export from your broker or Yahoo, and the
backtest stays reproducible forever. Use **split- and dividend-adjusted**
prices; the loader prefers an `adj_close` column and warns on suspected
unadjusted splits. A Yahoo source is included but was written blind (this
sandbox's proxy blocked it), so verify it before relying on it.

```bash
pip install -r requirements-dev.txt && pytest    # 172 tests, both projects
```

---

## Layout

```
brokerbot/
  models.py       Bar, Order, Fill, ClosedTrade  (Bar is frozen — no accidental mutation)
  costs.py        commission, spread, slippage, FX + real Swedish broker presets
  data/           CSV, synthetic, Yahoo, and a validator that catches bad data
  strategy/       interface + SMA crossover, momentum, mean reversion, buy & hold
  backtest/       engine (next-bar fills), metrics (benchmark-first), walk-forward
  brokers/        paper, Saxo, IBKR, eToro
  cli.py
```

---

## Honest limitations

- **The reference strategies are textbook and unlikely to have an edge.** Moving
  average crossovers on liquid large-caps are among the most published rules in
  existence; if they reliably beat holding, that would've been arbitraged away
  decades ago. They're here for calibration — so you can tell whether your own
  ideas are actually better or merely untested.
- **No broker adapter has been run against a live API.** The sandbox had no
  credentials and blocked outbound calls. They're written against documented
  shapes; `broker --check` reports exactly which call fails.
- **Long-only.** Shorting has borrow costs and assignment risk this doesn't
  model, so signal weights are clamped to [0, 1].
- **Single instrument at a time.** No portfolio construction, correlation, or
  risk parity across positions.
- **Backtests assume your orders don't move the market.** True at retail size in
  liquid names, false in small caps.
- **Automated trading of liquid equities is a market where retail systematically
  underperforms indexing after costs.** That's not a reason not to build this —
  it's a reason the measurement tools are the valuable part. Use them before
  concluding you've found something.
