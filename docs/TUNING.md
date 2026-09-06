# Tuning

Every threshold is in `config.yaml` and every one has a comment. This is the
short version of which dials to turn when.

## "I'm getting no alerts"

Expected for the first two weeks — X account scores start empty and
cross-source consensus needs at least two live sources. Check in order:

1. `python run.py --check` — is X actually connected? Without it you have one
   source, and STRONG requires two.
2. `/report` — the daily report shows what was filtered and at which stage. If
   everything dies at `safety`, your safety thresholds are too tight for
   current market conditions. If everything dies at `gate:below_alert_tier`,
   conviction is not clearing.
3. `/watchlist` — shows near-misses with their conviction scores. If the top
   candidate sits at 4.5 and STRONG is 5.5, lower the threshold:
   `/set consensus.tiers.strong.min_conviction 4.5`

Loosen in this order, one at a time, waiting a few days between changes:

| Change | From | To | Effect |
|---|---|---|---|
| `consensus.tiers.strong.min_conviction` | 5.5 | 4.5 | more alerts, weaker |
| `consensus.min_independent_actors` | 3 | 2 | much more, much weaker |
| `alerts.suppress_watch_tier` | true | false | pushes WATCH tier too |
| `alerts.fill_budget_with_watch` | false | true | fills quiet days with marginal setups |

If `/report` shows most rejections at `flow`, the move-confirmation gate is
too strict for current conditions — loosen `confirmation.flow.min_acceleration`
(0.25 → 0.15) before touching anything else, since that one vetoes hardest in
slow markets.

Do **not** start by loosening `safety.*`. Those thresholds are what stop you
buying rugs, and they are the cheapest protection in the system.

## "I'm getting too many alerts"

`alerts.daily_budget` is a hard cap — lower it and only the highest-conviction
candidates survive. That is a better first move than raising the conviction
threshold, because it preserves ranking.

## "The alerts are bad"

Look at what the losers had in common in `/report` and the positions table.

- Losing right after entry → tokens are too young or too thin. Raise
  `safety.min_age_minutes` and `safety.min_liquidity_usd`.
- Losing after a big initial pump → you are being used as exit liquidity.
  Lower `safety.max_age_hours` and tighten
  `traders.x_accounts.max_median_multiple_at_call_time`.
- Rugs getting through → tighten `safety.max_top10_holder_pct` and
  `safety.max_bundled_supply_pct`, and check the probe tool output: if
  RugCheck fields are not parsing, the gate is passing tokens it should be
  rejecting.
- One trader dragging results down → `/traders` shows scores. Trader scoring
  demotes automatically on 7-day decay, but you can tighten
  `traders.wallets.min_median_multiple`.

## The metric that actually matters

Not win rate. **Median multiple of closed positions**, which the paper-trading
history gives you directly. A strategy that wins 30% of the time with a 4×
median on winners beats one that wins 70% with a 1.2× median, and the second
one feels much better while you are running it.

Give any change at least 30 closed positions before judging it. Below that you
are reading noise.

## Flow confirmation

`confirmation.flow` vetoes signals where the move is already over. Three dials:

| Setting | Default | Loosen if |
|---|---|---|
| `min_acceleration` | 0.25 | Too many rejections at `flow` in slow markets |
| `min_buy_pressure_5m` | 0.35 | Rarely worth loosening — this one catches active distribution |
| `max_run_up_1h_pct` | 400 | You want to chase faster movers (raises late-entry risk) |

`confirmation.boosts.max_penalty` (0.35) controls how much paid promotion
costs a token. Set it to 0 to ignore boosts entirely; raise it toward 1.0 to
treat any paid promotion as near-disqualifying.

Both can be disabled with `enabled: false`, which makes them exact no-ops
rather than silently neutral.
