# memebot

A Telegram bot that watches good traders across **Pump.fun**, **X/Twitter**
and **AlphaLedger**, and messages you only when several of them independently
converge on the same token.

The point is not more signal. It is *less*: the defaults are tuned to send
**2–5 alerts a day**, each one backed by multiple independent traders on more
than one platform, and each one already checked for the ways meme coins are
engineered to take your money.

---

## The idea

Following one good trader is noisy. Following fifty is unusable. But when
three traders who have never interacted, on two different platforms, buy the
same token inside half an hour — that is a real signal, because the ways each
platform can be gamed are different. A paid campaign moves X. A copy-trading
swarm moves on-chain flow. Something that moves both at once is more likely to
be genuine.

So the bot computes a **conviction score** per token:

```
conviction = cross_source_multiplier(n_sources)
             × Σ over (actor, source) of
                 actor_score × source_weight × size_factor × recency × confidence
```

and alerts when conviction clears a threshold *and* at least 3 independent
actors across ≥2 platforms are involved *and* the token passes every safety
check.

### Why "independent" is doing a lot of work there

The obvious version of this bot is trivially exploitable: one person with five
wallets looks exactly like five traders agreeing. So wallets are collapsed
into **clusters** before they are counted, using three kinds of evidence —
direct SOL transfers between them, a shared funding wallet, and repeated
co-occurrence (buying the same tokens within seconds of each other, many
times). Five wallets belonging to one person contribute once.

This is the single most important correctness property in the system, and it
is what `tests/test_clustering_and_resolver.py` spends most of its time on.

---

## What gets filtered, and why

You said you don't know meme coins, so these are set for you. Every threshold
lives in `config.yaml` with a comment explaining it. The important ones:

### Safety gate — hard filters, checked before anything else

Most money lost in meme coins is lost to rugs, not to bad timing, and rugs are
detectable in advance. A token is discarded if **any** of these fail:

| Check | Default | Why |
|---|---|---|
| Liquidity | ≥ $15k | Below this you cannot exit without catastrophic slippage |
| Mint authority | must be revoked | Otherwise the deployer can print unlimited supply |
| Freeze authority | must be revoked | Otherwise they can freeze your wallet |
| LP burned/locked | required | Otherwise they can withdraw the entire pool in one transaction |
| Top-10 holders | < 30% | Ten wallets holding a third of supply decide your exit price |
| Dev holding | < 5% | |
| Unique holders | ≥ 75 | Fewer means "the community" is one person with many wallets |
| Bundled supply | < 25% | Coordinated launch wallets are engineered exit liquidity |
| Volume / liquidity | < 50× | Higher means bots cycling the same SOL to fake volume |
| Age | 5 min – 72 h | Old enough to survive the instant-rug window, young enough that the move hasn't happened |

**Unknown counts as unsafe.** If a lookup fails and we cannot confirm the mint
authority is revoked, the token is rejected. Failing closed costs you missed
opportunities; failing open costs you the position.

### Trader quality — who is worth following

Wallets are scored on reconstructed, *realised* PnL over 30 days, and are
rejected for:

- **Too few trades** (< 20 closed) — no statistical meaning yet
- **Low median multiple** — this is the one that matters. A wallet with one
  lucky 100× and twenty losses has enormous total PnL and is worthless to
  follow. The mean hides that; the median does not.
- **Sub-minute hold times** — snipers and MEV bots. Profitable, but their edge
  is latency you do not have. By the time their trade reaches you, it's over.
- **Deployer-linked** — a wallet repeatedly early on one deployer's tokens
  isn't predicting anything, it's being told. Insider, not trader.
- **7-day decay** — traders go cold. A wallet whose recent form drops 40%
  below its 30-day form is demoted automatically.

X accounts can't be scored on PnL, so every time they post a contract address
the price is snapshotted and graded 24h later. A "hit" requires the token to
reach 2× *without first drawing down 50%* — crediting a wick that only printed
after you'd have been stopped out would be lying to yourself. Accounts posting
more than 8 calls a day, or whose median call is already up 4× when they post,
are rejected as spray-and-pray and late callers respectively.

### Exits — because a buy signal without a sell plan is worthless

Every alerted position is tracked until closed, with six triggers in priority
order: liquidity collapse / safety regression (urgent), smart-money exit (the
traders who got you in are leaving), stop loss at −45%, a take-profit ladder
(40% at 2×, 30% at 4×, 20% at 10×), a trailing stop after the first rung, and
a 24h time stop.

**Exit alerts ignore the daily budget and quiet hours.** If the bot told you to
buy, it owes you the sell.

---

## Cost

| Source | Cost |
|---|---|
| Pump.fun (PumpPortal websocket) | free |
| DexScreener, RugCheck | free |
| X / Twitter | ~$13/month |
| **Total** | **~$13/month** |

X's official filtered stream costs **$5,000/month** (Pro tier), so this uses a
third-party mirror at ~$0.15 per 1,000 tweets instead.

But that alone isn't enough. Polling 40 accounts individually every 2 minutes
is ~$260/month. The fix is **batching**: X search syntax accepts
`from:a OR from:b OR ...`, so 40 accounts become 2 search requests instead of
40 timeline requests — a 20× cost reduction, which is what brings a
2-minute polling cadence inside a $20 cap.

Price your own settings:

```bash
python -m memebot.tools.cost
```

A budget governor tracks estimated spend, paces it across the month, throttles
past 80%, and stops rather than overspending.

---

## Setup

```bash
git clone <this repo> && cd emilmemecoin
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN, then:
python -m memebot.tools.whoami    # message your bot first, this finds your chat id
# put the chat id in .env, plus X_API_KEY if you have one

python run.py --check     # validates config and credentials
python run.py
```

Verify the live API shapes on your machine (this sandbox couldn't reach them):

```bash
python -m memebot.tools.probe_schemas --all
```

### Run it in shadow mode first

`runtime.shadow_mode: true` is the default and you should leave it on for
**at least two weeks**. Alerts are marked `[SHADOW]` and no orders are placed.

This is not caution theatre — it's structural. X account scores are learned
from graded calls, and until ~2 weeks of calls have been graded the bot does
not yet know who is any good. Shadow mode is how you find out whether these
filters actually work before any money depends on them, and the daily report
tells you what was filtered and why so you can tune.

---

## Telegram commands

| Command | |
|---|---|
| `/status` | mode, circuit breakers, tracked traders |
| `/mode alerts\|paper\|live` | switch execution mode |
| `/panic` | stop everything immediately |
| `/resume` | clear a tripped breaker |
| `/positions` | open positions with live multiples |
| `/watchlist` | candidates below the alert threshold |
| `/budget` | X data spend this month |
| `/traders` | who is being followed and why |
| `/set <key> <value>` | change any config value at runtime |

---

## Auto-trading

You asked to be able to switch between alerts-only and auto-trade, so
`/mode live` exists. It is deliberately awkward to reach.

**Two independent locks, both required:**

1. `execution.live_mode_armed: true` in `config.yaml` — set on the machine
2. `TRADING_WALLET_PRIVATE_KEY` in the environment

`/mode live` checks both and refuses if either is missing. **It cannot set
them.** That asymmetry is the point: your phone can always *stop* trading
instantly, but it can never start it. If someone gets into your Telegram, the
worst they can do is turn the bot off.

Trading is **non-custodial** — PumpPortal's "Local" API returns an *unsigned*
transaction that this process signs itself, so your key never leaves the
machine. (Their "Lightning" API, where they hold the key, is not used.)

Circuit breakers halt trading and require an explicit `/resume`: daily loss cap
(2 SOL), daily trade cap (12), 4 consecutive losses, max 5 concurrent
positions, and a minimum wallet balance floor.

**Even so: live mode means a hot wallet on a server. Use a burner funded with
only what you are willing to lose entirely.** `paper` mode does full PnL
accounting with modelled slippage and fees and is the honest way to find out
whether the strategy works.

---

## Architecture

```
sources ──> signal queue ──> consensus ──> safety gate ──> alert gate ──> Telegram
   │                             ▲                              │
   │                             │                              ▼
   └─ pump.fun (websocket)   clustering                     execution
      X (batched search)     trader scores                  (paper/live)
      AlphaLedger (polling)                                      │
                                                                 ▼
                                                          exit monitor
```

One asyncio process, SQLite, no broker, no external database. At a few
thousand signals a day for one user, adding those would only add ways to fail
silently.

```
memebot/
  sources/     pump.fun websocket, X batched poller + budget governor, AlphaLedger
  enrich/      DexScreener, RugCheck, ticker→mint resolver, SOL price
  scoring/     safety gate, trader scorecards, Sybil clustering, consensus
  alerts/      Telegram, formatting, daily budget + quiet hours
  execution/   paper, live (self-signing), guardrails, mode manager
  exits.py     six exit triggers
  app.py       wiring and the pipeline
```

```bash
pip install -r requirements-dev.txt && pytest    # 96 tests
```

---

## Honest limitations

- **AlphaLedger has no public API** that I could verify, so that adapter is
  written as a *generic* poller you point at an endpoint and describe in
  config. It is disabled by default and stays dormant rather than failing.
  See [docs/ALPHALEDGER.md](docs/ALPHALEDGER.md). With it off, the bot runs on
  two sources, which is the minimum cross-source consensus needs.
- **API schemas are unverified from here.** The build environment's egress
  proxy blocked `pumpportal.fun` and `alphaledger.ai`, so adapters were written
  against documented shapes with tolerant field mapping. Run
  `python -m memebot.tools.probe_schemas --all` on your machine before trusting
  it.
- **X account scores start empty.** ~2 weeks of shadow mode before they mean
  anything.
- **Wallet discovery needs a cold start.** Graduation-based discovery works on
  free data but takes time to accumulate. Fastest start is exporting a
  leaderboard from a tool that has already indexed history (GMGN, Cielo, Dune)
  into a seed file — those wallets are then re-scored from scratch by this
  bot's own criteria, so the external list is only a shortlist of who to look
  at, never a claim that they are good.
- **Nothing here predicts anything.** It filters. Most meme coins go to zero,
  including ones that pass every check in this repo.
