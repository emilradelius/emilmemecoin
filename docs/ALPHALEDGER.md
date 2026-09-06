# AlphaLedger integration

## Status: dormant by default

You identified **AlphaLedger.ai** — the platform that publishes verified
trader track records, ranks strategies, and connects traders with investor
funding.

I could not verify that it exposes a public API, developer documentation, or
webhooks. (The build environment's egress proxy also blocked the domain, so I
could not check directly.) Rather than guess at an endpoint and ship something
that silently fails, the adapter is written as a **generic poller**: you give
it a base URL and describe the response shape in `config.yaml`, and it maps
that into signals. No code change is needed if AlphaLedger gives you API
access, or adds one later.

With `sources.alphaledger.enabled: false` (the default) the adapter stays
dormant and the bot runs on Pump.fun + X, which is the minimum that
cross-source consensus needs.

## Why it is not a primary trigger

AlphaLedger covers majors — BTC, ETH, SOL, large caps. A trader's BTC position
tells you nothing about whether a three-hour-old dog coin is going to work, and
wiring it as a meme coin trigger would be importing noise dressed as signal.

So it does two narrower jobs instead:

**1. Corroboration.** On the rare occasions a token appears on both, it adds
weight (source weight 0.45, versus 1.00 for on-chain flow).

**2. Regime detection — the more useful one.** When the platform's top-ranked
traders are net de-risking on majors, meme coin risk appetite is usually about
to dry up. Meme coins are a leveraged bet on crypto risk appetite generally;
they bleed hardest when majors turn. In that state the consensus engine
multiplies its thresholds by `consensus.regime.risk_off_conviction_multiplier`
(default 1.5) rather than going silent — marginal setups stop working in a
risk-off tape, but genuinely exceptional ones still do.

Risk-off is triggered when two-thirds of observed major-pair activity is
selling, and the state decays back to neutral after 6 hours without data so
that a dead feed cannot silently suppress the bot forever.

## Enabling it

```yaml
sources:
  alphaledger:
    enabled: true
    endpoint: "/v1/trades/recent"
    poll_interval_seconds: 300
    use_as_regime_filter: true
    field_map:
      items_path: "data"        # where the array lives in the response
      actor: "trader.username"  # dotted paths are supported
      symbol: "symbol"
      side: "side"              # buy/sell/long/short
      size_usd: "notional_usd"
      price: "price"
      timestamp: "executed_at"
      url: "url"
```

```bash
ALPHALEDGER_API_KEY=...
ALPHALEDGER_API_BASE=https://api.alphaledger.ai
```

Authentication is sent as `Authorization: Bearer <key>`. If they use a
different scheme, adjust the header in `AlphaLedgerSource.__init__`.

## If there is no API

Three options, in order of how much I'd recommend them:

1. **Leave it off.** Two sources is enough for cross-source consensus. This is
   the honest default and costs you very little.
2. **Ask them.** Platforms courting developers often have an undocumented API
   or will enable one on request.
3. **Substitute a different third source.** The architecture is
   source-agnostic — implement `SignalSource` and hand the pipeline `Signal`
   objects. Candidates that genuinely serve the "what are good traders doing"
   role, with better data access:
   - **Cielo Finance** — multi-chain wallet tracking with Telegram-native
     alerts and an API. Closest in spirit to what you described.
   - **GMGN.ai** — smart-money and KOL tracking built specifically for meme
     coins, with an API and Telegram integration.
   - **Dune / Nansen** — query-based; better for the wallet *discovery*
     cold start than for real-time signal.

A note on the third option: adding a source that reads the same underlying
on-chain data as Pump.fun does **not** give you real cross-source
confirmation. The whole value of the multiplier is that the sources fail
independently. Two views of the same chain agreeing is one source counted
twice, and wiring it as two would inflate conviction on exactly the tokens
you should be most careful about.
