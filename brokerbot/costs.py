"""Trading cost model.

This is the most important file in the backtester, and the one most often
omitted. A frictionless backtest turns losing strategies into winning ones:
the more a strategy trades, the more flattering the omission, which means the
strategies that look best without costs are precisely the ones that lose most
with them.

Four costs are modelled, all of which a Swedish retail account actually pays:

* **Commission** - per-trade, usually with a minimum that dominates small orders.
* **Spread** - you buy at the ask and sell at the bid. Charged as half the
  quoted spread per side.
* **Slippage** - market impact and movement between decision and fill.
* **FX** - the conversion fee on trading a USD instrument from a SEK account.
  Swedish brokers typically charge 0.25%-0.5% per conversion, both ways, and
  it is invisible on the trade confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CostModel:
    commission_pct: float = 0.0025
    """Fraction of trade value. Nordnet/Avanza Swedish equities are ~0.15-0.25%."""

    commission_min: float = 1.0
    """Per-order minimum in account currency. Dominates small orders - this is
    why trading 500 SEK positions is close to hopeless."""

    spread_pct: float = 0.0005
    """Full quoted spread as a fraction of price. Half is charged per side.
    Large-cap equities sit near 0.01-0.05%; small caps are far wider."""

    slippage_pct: float = 0.0005
    """Movement between decision and fill."""

    fx_pct: float = 0.0025
    """Currency conversion, charged when the instrument is not in the account
    currency. Applied on every trade, both directions."""

    def commission(self, value: float) -> float:
        return max(self.commission_min, abs(value) * self.commission_pct)

    def spread_cost(self, value: float) -> float:
        # Half-spread per side: you cross half the quoted spread each way.
        return abs(value) * self.spread_pct / 2.0

    def slippage(self, value: float) -> float:
        return abs(value) * self.slippage_pct

    def fx(self, value: float, *, needs_conversion: bool) -> float:
        return abs(value) * self.fx_pct if needs_conversion else 0.0

    def total(self, value: float, *, needs_conversion: bool = False) -> float:
        return (
            self.commission(value)
            + self.spread_cost(value)
            + self.slippage(value)
            + self.fx(value, needs_conversion=needs_conversion)
        )

    def round_trip_pct(self, value: float, *, needs_conversion: bool = False) -> float:
        """What fraction of the position a full in-and-out trade costs.

        The number worth internalising before designing any strategy: if a
        round trip costs 1.2% and your average edge per trade is 0.8%, no
        amount of tuning saves you. Print this first.
        """
        if value <= 0:
            return 0.0
        return 2 * self.total(value, needs_conversion=needs_conversion) / value


# Presets reflecting real Swedish retail conditions. Verify against your own
# broker's fee schedule before trusting a backtest built on them.
PRESETS: dict[str, CostModel] = {
    # Swedish equities from a Swedish broker: no FX, low commission.
    "nordic_equities": CostModel(
        commission_pct=0.0015, commission_min=1.0, spread_pct=0.0008,
        slippage_pct=0.0005, fx_pct=0.0,
    ),
    # US equities from a SEK account: FX applies and usually dominates.
    "us_equities_from_sek": CostModel(
        commission_pct=0.0015, commission_min=1.0, spread_pct=0.0003,
        slippage_pct=0.0005, fx_pct=0.0025,
    ),
    # Interactive Brokers tiered: cheapest realistic retail option.
    "ibkr_us": CostModel(
        commission_pct=0.0005, commission_min=0.35, spread_pct=0.0003,
        slippage_pct=0.0004, fx_pct=0.0002,
    ),
    # Crypto spot on a major exchange (Bybit/Binance taker ~0.055-0.1% per
    # side). Cheap per trade by equity standards, but crypto spreads widen
    # hard in exactly the fast markets a breakout rule trades into, so
    # slippage is set above the headline fee rather than below it.
    "crypto_spot": CostModel(
        commission_pct=0.00075, commission_min=0.0, spread_pct=0.0006,
        slippage_pct=0.0010, fx_pct=0.0,
    ),
    # eToro: zero stated commission, but the spread is the product, and
    # non-USD deposits are converted.
    "etoro": CostModel(
        commission_pct=0.0, commission_min=0.0, spread_pct=0.0009,
        slippage_pct=0.0008, fx_pct=0.005,
    ),
    "zero": CostModel(0.0, 0.0, 0.0, 0.0, 0.0),
}
