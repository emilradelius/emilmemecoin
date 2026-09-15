"""The live runner: one cycle, repeated, against a real broker.

Built for an unattended multi-day run, which means the failures it guards
against are not strategy failures but operational ones. In order of how often
they actually happen:

1. **Credentials expire.** Saxo simulation tokens last 24 hours; IBKR's
   gateway needs a browser re-auth daily. A runner that ignores this works
   beautifully for one day and then silently stops trading. Every cycle
   verifies the connection and surfaces a failure loudly.
2. **Local state drifts from the broker's.** A partial fill, a manual trade in
   the app, an order rejected while the process restarted - any of these make
   our idea of the portfolio wrong. Every cycle reconciles against the broker
   and treats the broker as authoritative.
3. **The process dies.** A heartbeat file is written each cycle so you can
   tell "running and finding nothing" from "dead since Tuesday" - which look
   identical from the outside and mean opposite things.

State is persisted each cycle, so a restart resumes rather than starting over
with an empty view of its own positions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .brokers.base import AccountSummary, Broker, BrokerPosition
from .costs import CostModel
from .data.base import BarSource
from .models import Bar, Order, OrderType, Side
from .strategy.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    ts: datetime
    ok: bool = True
    connected: bool = False
    equity: float = 0.0
    cash: float = 0.0
    positions: int = 0
    orders_placed: int = 0
    orders_rejected: int = 0
    news_ingested: int = 0
    news_tradeable: int = 0
    news_cost_usd: float = 0.0
    reconcile_drift: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


class BarStore:
    """Daily bars: seeded from history, appended from live prices.

    A strategy needs history to compute anything - a 60-day moving average
    cannot be built from a week of live data. So the store seeds from a
    historical source at startup and appends one bar per trading day from
    observed prices.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._bars: dict[str, list[Bar]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            log.warning("could not read bar store; starting empty")
            return
        for symbol, rows in raw.items():
            self._bars[symbol] = [
                Bar(symbol, datetime.fromisoformat(r["ts"]), r["o"], r["h"],
                    r["l"], r["c"], r.get("v", 0.0))
                for r in rows
            ]

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                sym: [{"ts": b.ts.isoformat(), "o": b.open, "h": b.high,
                       "l": b.low, "c": b.close, "v": b.volume} for b in bars]
                for sym, bars in self._bars.items()
            }))
        except OSError as exc:
            log.warning("could not persist bar store: %s", exc)

    def seed(self, symbol: str, source: BarSource) -> int:
        """Load historical bars. Skipped when we already have history."""
        if self._bars.get(symbol):
            return len(self._bars[symbol])
        try:
            bars = source.load(symbol)
        except Exception as exc:
            log.error("could not seed history for %s: %s", symbol, exc)
            return 0
        problems = BarSource.validate(bars)
        if problems:
            log.warning("history for %s has %d data warnings: %s",
                        symbol, len(problems), problems[0])
        self._bars[symbol] = bars
        return len(bars)

    def observe(self, symbol: str, price: float, ts: datetime) -> bool:
        """Fold a live price into today's bar. Returns True on a new day."""
        bars = self._bars.setdefault(symbol, [])
        today = ts.date()
        if bars and bars[-1].ts.date() == today:
            last = bars[-1]
            bars[-1] = Bar(symbol, last.ts, last.open, max(last.high, price),
                           min(last.low, price), price, last.volume)
            return False
        bars.append(Bar(symbol, ts, price, price, price, price, 0.0))
        return True

    def history(self, symbol: str) -> list[Bar]:
        return list(self._bars.get(symbol, []))


class LiveRunner:
    def __init__(
        self,
        broker: Broker,
        strategy: Strategy,
        symbols: list[str],
        *,
        costs: CostModel,
        bar_store: BarStore | None = None,
        history_source: BarSource | None = None,
        news_pipeline=None,
        max_position_weight: float = 0.2,
        min_order_value: float = 500.0,
        cycle_seconds: float = 900.0,
        dry_run: bool = True,
        state_dir: Path | str = "data/live",
        on_event=None,
    ) -> None:
        self.broker = broker
        self.strategy = strategy
        self.symbols = symbols
        self.costs = costs
        self.state_dir = Path(state_dir)
        self.bars = bar_store or BarStore(self.state_dir / "bars.json")
        self.history_source = history_source
        self.news = news_pipeline
        self.max_position_weight = max_position_weight
        self.min_order_value = min_order_value
        self.cycle_seconds = cycle_seconds
        # Dry run is the default. A runner that places real orders because
        # someone forgot a flag is not an acceptable failure mode, even on a
        # demo account - the habit carries over to the live one.
        self.dry_run = dry_run
        self.on_event = on_event

        self.cycles: list[CycleResult] = []
        self._pending: dict[str, float] = {}
        self._consecutive_failures = 0

    # --- lifecycle --------------------------------------------------------
    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat.json"

    @property
    def kill_switch_path(self) -> Path:
        return self.state_dir / "STOP"

    def _heartbeat(self, result: CycleResult) -> None:
        """Written every cycle. 'Running and finding nothing' and 'dead since
        Tuesday' look identical from outside and mean opposite things."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.write_text(json.dumps({
                "last_cycle": result.ts.isoformat(),
                "ok": result.ok,
                "connected": result.connected,
                "equity": result.equity,
                "positions": result.positions,
                "dry_run": self.dry_run,
                "consecutive_failures": self._consecutive_failures,
                "errors": result.errors[:5],
            }, indent=2))
        except OSError as exc:
            log.warning("could not write heartbeat: %s", exc)

    async def seed_history(self) -> None:
        if self.history_source is None:
            log.warning(
                "no history source configured - strategies needing a warmup "
                "window will not produce signals until enough live days accrue"
            )
            return
        for symbol in self.symbols:
            n = self.bars.seed(symbol, self.history_source)
            log.info("seeded %d historical bars for %s", n, symbol)
        self.bars.save()

    # --- one cycle --------------------------------------------------------
    async def cycle(self) -> CycleResult:
        result = CycleResult(ts=datetime.utcnow())

        # 1. Connection. Checked first and every time: an expired token is the
        #    single most likely reason a multi-day run stops.
        try:
            result.connected = await self.broker.connect()
        except Exception as exc:
            result.errors.append(f"connect raised: {exc}")
        if not result.connected:
            result.ok = False
            result.errors.append(
                "broker not connected - most likely an expired token or a "
                "gateway needing re-authentication"
            )
            self._consecutive_failures += 1
            self._heartbeat(result)
            await self._emit(result)
            return result

        # 2. Account and reconciliation against the broker's own view.
        try:
            account = await self.broker.account()
            positions = await self.broker.positions()
            result.equity = account.equity
            result.cash = account.cash
            result.positions = len(positions)
            result.reconcile_drift = self._reconcile(positions)
        except Exception as exc:
            result.ok = False
            result.errors.append(f"account/positions failed: {exc}")
            self._consecutive_failures += 1
            self._heartbeat(result)
            await self._emit(result)
            return result

        # 3. News, if configured.
        if self.news is not None:
            try:
                signals = self.news.process()
                stats = self.news.stats
                result.news_ingested = stats.ingested
                result.news_tradeable = stats.tradeable
                result.news_cost_usd = stats.cost_usd
                for sig in signals:
                    if hasattr(self.strategy, "add_signal"):
                        self.strategy.add_signal(sig)
            except Exception as exc:
                result.errors.append(f"news pipeline failed: {exc}")

        # 4. Prices, bars, signals, orders.
        held = {p.symbol: p for p in positions}
        for symbol in self.symbols:
            try:
                price = await self.broker.last_price(symbol)
                if price is None or price <= 0:
                    result.errors.append(f"no price for {symbol}")
                    continue
                self.bars.observe(symbol, price, result.ts)

                history = self.bars.history(symbol)
                if len(history) < max(1, self.strategy.warmup):
                    continue
                signal = self.strategy.on_bar(symbol, history)
                if signal is None:
                    continue

                placed, rejected = await self._apply(
                    symbol, signal.target_weight, price, account, held.get(symbol),
                    signal.reason, result,
                )
                result.orders_placed += placed
                result.orders_rejected += rejected
            except Exception as exc:
                result.errors.append(f"{symbol}: {exc}")

        self.bars.save()
        self._consecutive_failures = 0 if result.ok else self._consecutive_failures + 1
        self.cycles.append(result)
        self._heartbeat(result)
        await self._emit(result)
        return result

    def _reconcile(self, positions: list[BrokerPosition]) -> list[str]:
        """Compare the broker's positions against what we think we hold.

        The broker is authoritative. Drift is reported rather than corrected:
        silently overwriting either side hides the bug that caused it.
        """
        drift: list[str] = []
        broker_view = {p.symbol: p.quantity for p in positions}
        for symbol, expected in self._pending.items():
            actual = broker_view.get(symbol, 0.0)
            if abs(actual - expected) > max(1e-6, abs(expected) * 0.02):
                drift.append(
                    f"{symbol}: expected {expected:.4f}, broker reports {actual:.4f}"
                )
        for symbol, qty in broker_view.items():
            if symbol not in self._pending and abs(qty) > 1e-9:
                drift.append(f"{symbol}: broker holds {qty:.4f} we did not open")
        if drift:
            log.warning("position drift detected: %s", "; ".join(drift))
        return drift

    async def _apply(self, symbol: str, target_weight: float, price: float,
                     account: AccountSummary, position: BrokerPosition | None,
                     reason: str, result: CycleResult) -> tuple[int, int]:
        weight = min(target_weight, self.max_position_weight)
        current_qty = position.quantity if position else 0.0
        target_qty = (account.equity * weight) / price
        delta = target_qty - current_qty

        if delta > 0:
            affordable = max(0.0, account.cash * 0.98) / price
            delta = min(delta, affordable)
        if abs(delta * price) < self.min_order_value:
            return 0, 0

        order = Order(
            symbol=symbol, side=Side.BUY if delta > 0 else Side.SELL,
            quantity=abs(delta), order_type=OrderType.MARKET,
            ts=result.ts, reason=reason,
        )

        if self.dry_run:
            log.info("[dry-run] would %s %.4f %s at ~%.4f (%s)",
                     order.side.value, order.quantity, symbol, price, reason)
            return 1, 0

        outcome = await self.broker.place(order)
        if not outcome.ok:
            log.error("order rejected for %s: %s", symbol, outcome.detail)
            result.errors.append(f"{symbol} order rejected: {outcome.detail}")
            return 0, 1

        self._pending[symbol] = target_qty
        log.info("placed %s %.4f %s (%s) -> %s",
                 order.side.value, order.quantity, symbol, reason, outcome.detail)
        return 1, 0

    async def _emit(self, result: CycleResult) -> None:
        if self.on_event is None:
            return
        try:
            out = self.on_event(result)
            if asyncio.iscoroutine(out):
                await out
        except Exception:
            log.exception("event callback failed")

    # --- loop -------------------------------------------------------------
    async def run(self, *, until: datetime | None = None) -> list[CycleResult]:
        await self.seed_history()
        log.info(
            "live runner starting: %s mode, %d symbols, %.0fs cycle%s",
            "DRY-RUN" if self.dry_run else "PLACING ORDERS",
            len(self.symbols), self.cycle_seconds,
            f", until {until.isoformat()}" if until else "",
        )
        while True:
            if self.kill_switch_path.exists():
                log.warning("kill switch present at %s - stopping",
                            self.kill_switch_path)
                break
            if until and datetime.utcnow() >= until:
                log.info("scheduled end reached")
                break

            await self.cycle()

            if self._consecutive_failures >= 4:
                log.error(
                    "%d consecutive failed cycles - the run is not working. "
                    "Check the heartbeat file and the broker connection.",
                    self._consecutive_failures,
                )
            await asyncio.sleep(self.cycle_seconds)

        await self.broker.close()
        return self.cycles
