# The 7-day demo trial

## What a week can and cannot tell you

**It cannot tell you whether the strategy makes money.** The readiness gate
wants 30 closed trades over 21 days before taking a result seriously, and the
drift strategy holds positions for 20 days. A seven-day run finishes with
somewhere between zero and two completed round trips. Any P&L figure from that
is a coin flip, and reading it as evidence is the most expensive mistake
available at this stage.

**It is excellent for finding out whether the machine works.** Every one of
these has stopped a real trial dead, and none of them show up in a backtest:

- the broker token expired overnight and nothing traded again
- the process died on day three and looked identical to "no signals today"
- local position state drifted from the broker's after a partial fill
- the news classifier cost ten times the projection
- every order was rejected for a reason the backtest never modelled

So the trial scores **operational** criteria and explicitly refuses to render a
verdict on profitability.

---

## Before you start: the token problem

This decides whether a 7-day run is possible at all.

| Broker | Credential | Survives 7 days unattended? |
|---|---|---|
| **Saxo** (copy-paste token) | 24-hour developer token | ❌ dies after day 1 |
| **Saxo** (OAuth2 app) | refresh token, 20-min access tokens | ✅ **use this** |
| **IBKR** | gateway, browser login | ❌ needs daily re-auth |
| **eToro** | API key | ✅ likely |
| **paper** | none | ✅ always |

For Saxo, register an app at [developer.saxo](https://www.developer.saxo/) and
use the refresh flow. `SaxoBroker` renews access tokens itself.

**One trap:** Saxo rotates the refresh token on every use — the old one dies
immediately. Pass `token_store` (the CLI does this automatically) so the new
one is written to disk, or the credential chain breaks on your first restart.

---

## Step 1 — preflight

Never start a week-long run without this. It checks every moving part and
takes seconds.

```bash
export SAXO_REFRESH_TOKEN=... SAXO_CLIENT_ID=... SAXO_CLIENT_SECRET=...
export ANTHROPIC_API_KEY=...          # only if using --news

python -m brokerbot.cli preflight \
    --broker saxo --symbols VOLV-B.ST,ERIC-B.ST --news
```

It verifies the adapter builds, the broker connects, the account reads, a price
comes back for every symbol, and whether your Saxo credentials can actually
survive a week. It fails loudly rather than letting you discover a problem on
day four.

## Step 2 — start in dry-run

```bash
python -m brokerbot.cli trial \
    --broker saxo --symbols VOLV-B.ST,ERIC-B.ST \
    --csv data/volvo.csv --strategy news_drift \
    --news --news-cap 5 --days 7 --cycle-seconds 900
```

Dry-run is the **default**: orders are logged, not sent. Run at least a day
this way and read the log. A runner that sends real orders because a flag was
forgotten is not an acceptable failure mode, even on a demo account — the habit
carries to the live one.

Add `--live-orders` once the dry-run output looks right.

`--csv` matters: strategies need a warmup window. Without history, a 60-day
moving average cannot exist until 60 live days have passed.

## Step 3 — keep it alive

`systemd` on Linux:

```ini
# /etc/systemd/system/brokerbot-trial.service
[Unit]
Description=brokerbot 7-day demo trial
After=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/emilmemecoin
EnvironmentFile=/home/youruser/emilmemecoin/.env
ExecStart=/home/youruser/emilmemecoin/.venv/bin/python -m brokerbot.cli trial \
    --broker saxo --symbols VOLV-B.ST,ERIC-B.ST --csv data/volvo.csv \
    --news --days 7 --live-orders
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now brokerbot-trial
journalctl -u brokerbot-trial -f
```

`Restart=always` matters — the run must survive its own crashes, and the trial
report will tell you how often that happened.

## Step 4 — check in daily

```bash
cat data/live/heartbeat.json                    # is it alive?
python -m brokerbot.cli trial --report          # assessment so far
```

The heartbeat is the important one. **"Running and finding nothing" and "dead
since Tuesday" look identical from the outside and mean opposite things.**

To stop early, either Ctrl-C or:

```bash
touch data/live/STOP
```

## Step 5 — read the verdict

```bash
python -m brokerbot.cli trial --report
```

Blocking checks:

| Check | Fails when |
|---|---|
| `ran_long_enough` | stopped before ~90% of the planned days |
| `uptime` | more than 10% of the window lost to gaps |
| `credentials_held` | more than one cycle couldn't connect (**usually token expiry**) |
| `no_long_outage` | any gap beyond ~2h |
| `positions_reconcile` | our state ever disagreed with the broker's |
| `orders_accepted` | more than 10% of orders rejected |

Warnings (not blocking): missing prices, news cost above budget, and a news
filter passing more than ~10% of stories as tradeable — most of a feed is
noise, and a filter finding signal everywhere is finding signal that isn't
there.

---

## After the week

Passing means **the plumbing works**. That is all it means.

The next phase is the one that answers your actual question:

1. Keep it running in **paper mode for 6–8 weeks**. Long enough to accumulate
   30+ closed trades.
2. Run `python -m brokerbot.cli backtest --strategy news_drift --csv ...` and
   check it against **buy-and-hold** — the comparison that decides everything.
3. Run `python -m brokerbot.cli noise --strategy news_drift` to see what
   returns the method manufactures from pure randomness. Your real result has
   to clear that bar.
4. Only then consider real money, at a fraction of the paper position size.

Roughly two months before anything is at risk. If that feels slow, the
alternative is finding out by losing money instead of by waiting.
