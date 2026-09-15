"""Application wiring and the main pipeline.

The flow, end to end::

    sources ──> signal queue ──> consensus ──> safety gate ──> alert gate
                                                                   │
                                              execution <──────────┤
                                                                   ▼
                                                              Telegram

Plus periodic background work: re-scoring traders, rebuilding wallet
clusters, grading X calls, monitoring exits, and the daily report.

Everything runs in one asyncio process. There is no queue broker, no worker
pool and no external database, because at this volume - a few thousand
signals a day for one user - adding them would only add ways to fail
silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from datetime import timedelta
from typing import Any

from .alerts.formatter import format_buy, format_daily_report, format_exit
from .alerts.gate import AlertGate
from .alerts.telegram import TelegramBot
from .config import Config
from .enrich.boosts import BoostTracker
from .enrich.dexscreener import DexScreener
from .enrich.flow import FlowAnalyzer
from .enrich.resolver import TokenResolver
from .enrich.rugcheck import RugCheck
from .enrich.solprice import SolPrice
from .execution.manager import ExecutionManager
from .exits import ExitMonitor
from .models import Candidate, Position, Side, Signal, Source, Tier
from .readiness import ReadinessAssessor
from .scoring.clustering import ClusterBuilder, ClusterMap
from .scoring.consensus import ConsensusEngine
from .scoring.discovery import WalletDiscovery
from .scoring.safety import SafetyGate
from .scoring.traders import WalletScorer, XAccountScorer
from .sources.alphaledger import AlphaLedgerSource
from .sources.pumpfun import PumpFunSource
from .sources.xfeed import XSource
from .store import Store

log = logging.getLogger(__name__)


class MemeBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.get("storage.db_path", "data/memebot.sqlite"))
        self.shadow = cfg.get("runtime.shadow_mode", True)

        # --- enrichment ---------------------------------------------------
        self.dex = DexScreener()
        self.rug = RugCheck()
        self.solprice = SolPrice(self.dex)
        flow_cfg = cfg.get("confirmation.flow", {}) or {}
        self.flow = FlowAnalyzer(
            enabled=flow_cfg.get("enabled", True),
            min_buy_pressure_5m=flow_cfg.get("min_buy_pressure_5m", 0.35),
            min_acceleration=flow_cfg.get("min_acceleration", 0.25),
            max_run_up_1h_pct=flow_cfg.get("max_run_up_1h_pct", 400.0),
            min_multiplier=flow_cfg.get("min_multiplier", 0.60),
            max_multiplier=flow_cfg.get("max_multiplier", 1.25),
        )
        boost_cfg = cfg.get("confirmation.boosts", {}) or {}
        self.boosts_enabled = boost_cfg.get("enabled", True)
        self.boosts = BoostTracker(
            heavy_boost_threshold=boost_cfg.get("heavy_boost_threshold", 500.0),
            max_penalty=boost_cfg.get("max_penalty", 0.35),
            refresh_seconds=boost_cfg.get("refresh_seconds", 120),
        )
        self.resolver = TokenResolver(
            self.dex,
            min_liquidity_usd=cfg.get("safety.min_liquidity_usd", 15000),
            max_age_hours=cfg.get("safety.max_age_hours", 72),
        )

        # --- scoring ------------------------------------------------------
        self.safety = SafetyGate(cfg, self.dex, self.rug)
        self.consensus = ConsensusEngine(cfg)
        self.cluster_builder = ClusterBuilder(cfg, self.store)
        self.clusters = ClusterMap({})
        self.wallet_scorer = WalletScorer(cfg, self.store)
        self.x_scorer = XAccountScorer(cfg, self.store)
        self.discovery = WalletDiscovery(cfg, self.store)

        # --- sources ------------------------------------------------------
        self.queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=10_000)
        self.pumpfun = PumpFunSource(
            cfg,
            sol_price_fn=self.solprice.get,
            actor_score_fn=self._wallet_score,
        )
        self.xfeed = XSource(
            cfg, self.store, self.resolver,
            api_key=os.getenv("X_API_KEY"),
            api_base=os.getenv("X_API_BASE"),
        )
        self.alphaledger = AlphaLedgerSource(
            cfg,
            api_key=os.getenv("ALPHALEDGER_API_KEY"),
            api_base=os.getenv("ALPHALEDGER_API_BASE"),
        )
        self.pumpfun.on_migration = self._on_migration

        # --- delivery and execution ---------------------------------------
        self.telegram = TelegramBot(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        )
        self.gate = AlertGate(cfg, self.store)
        self.execution = ExecutionManager(cfg, self.store, self.dex)
        self.exits = ExitMonitor(cfg, self.store, self.dex, self.safety)

        self.xfeed.on_budget_exhausted = self._notify_budget
        self.trade_provider = cfg.get("alerts.trade_link_provider", "axiom")

        self._score_cache: dict[str, float] = {}
        self._candidates_seen = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._paused = False
        self._register_commands()

    # --- helpers ----------------------------------------------------------
    def _wallet_score(self, wallet: str) -> float:
        if wallet in self._score_cache:
            return self._score_cache[wallet]
        rec = self.store.get_score(wallet, Source.PUMPFUN)
        score = rec.score if rec else 0.5
        self._score_cache[wallet] = score
        return score

    async def _notify_budget(self, status: str) -> None:
        await self.telegram.send(
            f"⚠️ <b>X data budget exhausted</b>\n\n{status}\n\n"
            "Polling is paused until next month. Consensus continues on the "
            "remaining sources, but cross-source agreement will be rarer, so "
            "expect fewer alerts.\n\nRaise the cap in config.yaml "
            "(sources.x.budget.monthly_usd_cap) or lower "
            "sources.x.max_tracked_accounts."
        )

    async def _on_migration(self, msg: dict[str, Any]) -> None:
        """A token graduated - the outcome label wallet discovery needs."""
        mint = msg.get("mint")
        if not mint:
            return
        since = time.time() - 24 * 3600
        early = [
            s.actor_id for s in self.store.signals_for_mint(mint, since)
            if s.side is Side.BUY
        ]
        self.discovery.observe_graduation(mint, early)

    # --- pipeline ----------------------------------------------------------
    async def _consume_signals(self) -> None:
        """Drain the queue, persist, and evaluate the affected token."""
        while True:
            sig = await self.queue.get()
            try:
                self.store.add_signal(sig)
                if not self._paused:
                    await self._evaluate(sig.token_mint)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("signal processing failed for %s", sig.token_mint[:8])
            finally:
                self.queue.task_done()

    async def _evaluate(self, mint: str) -> None:
        window = self.cfg.get("consensus.window_minutes", 30)
        signals = self.store.signals_for_mint(mint, time.time() - window * 60)
        if not signals:
            return

        cand = self.consensus.score(
            mint, signals, self.clusters, risk_off=self.alphaledger.risk_off
        )
        if cand.tier is Tier.NONE:
            return

        self._candidates_seen += 1

        # Safety is checked only once a token has real conviction behind it -
        # running it on every signal would be thousands of needless API calls.
        cand.safety = await self.safety.check(mint)
        if not cand.safety.passed:
            self.store.record_rejection(mint, "safety", cand.safety.failures)
            log.info(
                "%s cleared consensus (%.1f) but failed safety: %s",
                (cand.token_symbol or mint[:8]), cand.conviction,
                ", ".join(cand.safety.failures[:3]),
            )
            return

        # --- market-flow confirmation -------------------------------------
        # The traders told us WHO is buying. This asks whether the move is
        # still there to catch. It can veto or discount, never promote.
        market = await self.dex.get(mint)
        analysis = self.flow.analyze(market)
        if analysis.blocked:
            self.store.record_rejection(mint, "flow", [analysis.veto or "flow_veto"])
            log.info(
                "%s cleared consensus (%.1f) and safety but was vetoed by flow: %s",
                (cand.token_symbol or mint[:8]), cand.conviction, analysis.veto,
            )
            return

        multiplier = analysis.multiplier
        notes = list(analysis.notes)
        if self.boosts_enabled:
            await self.boosts.refresh()
            boost_mult, boost_note = self.boosts.penalty(
                mint, age_minutes=cand.safety.age_minutes if cand.safety else None
            )
            multiplier *= boost_mult
            if boost_note:
                notes.append(boost_note)

        before = cand.conviction
        self.consensus.apply_confirmation(
            cand, multiplier, risk_off=self.alphaledger.risk_off, notes=notes
        )
        if cand.tier is Tier.NONE:
            self.store.record_rejection(
                mint, "confirmation",
                [f"conviction {before:.1f} -> {cand.conviction:.1f} after flow/boost"],
            )
            return

        decision = self.gate.evaluate(cand)
        if not decision.send:
            if decision.defer:
                self.gate.defer(cand)
                log.info("deferring %s until quiet hours end", cand.token_symbol or mint[:8])
            else:
                self.store.record_rejection(mint, f"gate:{decision.reason}", [decision.reason])
            return

        await self._fire_buy(cand)

    async def _fire_buy(self, cand: Candidate) -> None:
        text = format_buy(
            cand, provider=self.trade_provider,
            shadow=self.shadow, mode=self.execution.mode,
        )
        await self.telegram.send(text)
        self.store.record_alert(
            uuid.uuid4().hex[:16], "buy",
            cand.token_mint, cand.tier.value, cand.conviction, text,
        )

        if self.shadow:
            log.info("[shadow] would have alerted %s", cand.token_symbol)
            return
        if not self.execution.trades:
            return
        await self._open_position(cand)

    async def _open_position(self, cand: Candidate) -> None:
        open_positions = self.store.open_positions()
        balance = await self.execution.executor.wallet_balance_sol()
        ok, why = self.execution.guardrails.can_open(
            open_positions=len(open_positions), wallet_balance_sol=balance
        )
        if not ok:
            await self.telegram.send(f"⏸ Not trading {cand.token_symbol}: {why}")
            return

        size = self.execution.guardrails.position_size(
            cand.conviction,
            strong_threshold=self.cfg.get("consensus.tiers.strong.min_conviction", 5.5),
        )
        price = cand.safety.price_usd if cand.safety else None
        result = await self.execution.executor.buy(cand.token_mint, size, price_usd=price)
        if not result.ok:
            await self.telegram.send(f"❌ Order failed for {cand.token_symbol}: {result.detail}")
            return

        pos = Position(
            token_mint=cand.token_mint,
            token_symbol=cand.token_symbol,
            entry_price_usd=result.filled_price_usd or price or 0.0,
            size_sol=result.filled_size_sol or size,
            tokens_held=result.tokens,
            mode=self.execution.mode,
            entry_tx=result.tx_signature,
            trigger_actors=[
                s.actor_id for s in cand.buy_signals if s.source is Source.PUMPFUN
            ],
        )
        pos.peak_price_usd = pos.entry_price_usd
        self.store.save_position(pos)
        self.exits.remember_entry_liquidity(
            pos.id, cand.safety.liquidity_usd if cand.safety else None
        )
        self.execution.guardrails.record_open()
        await self.telegram.send(
            f"✅ <b>Opened</b> {pos.size_sol:.3f} SOL in ${pos.token_symbol} "
            f"({self.execution.mode}) — {result.detail}"
        )

    # --- background loops --------------------------------------------------
    async def _exit_loop(self) -> None:
        interval = self.cfg.get("exits.poll_interval_seconds", 30)
        while True:
            try:
                for pos in self.store.open_positions():
                    recent = self.store.signals_for_mint(pos.token_mint, time.time() - 1800)
                    decision, price = await self.exits.evaluate(pos, recent_signals=recent)
                    if not decision.should_exit or price is None:
                        continue

                    if self.execution.trades:
                        result = await self.execution.executor.sell(
                            pos, decision.fraction, price_usd=price
                        )
                        if result.ok:
                            pnl = result.filled_size_sol - pos.size_sol * decision.fraction
                            self.execution.guardrails.record_close(pnl)
                            pos.realized_pnl_sol += pnl

                    await self.telegram.send(
                        format_exit(
                            pos, decision.reason, price,
                            sell_fraction=decision.fraction,
                            detail=decision.detail, provider=self.trade_provider,
                        ),
                        silent=not decision.urgent and decision.fraction < 1.0,
                    )
                    self.exits.apply_partial(pos, decision, price)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("exit loop iteration failed")
            await asyncio.sleep(interval)

    async def _rescore_loop(self) -> None:
        hours = self.cfg.get("traders.wallets.rescore_interval_hours", 24)
        while True:
            await asyncio.sleep(hours * 3600)
            try:
                await self.rescore_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rescore failed")

    async def rescore_all(self) -> None:
        log.info("re-scoring traders and rebuilding clusters")
        lookback = self.cfg.get("traders.wallets.lookback_days", 30)
        signals = self.store.signals_since(time.time() - lookback * 86400)
        self.clusters = self.cluster_builder.build(signals)
        self._score_cache.clear()

        for cand in self.discovery.promotable():
            existing = self.store.get_score(cand.wallet, Source.PUMPFUN)
            if existing is None:
                from .models import TraderScore
                self.store.upsert_score(
                    TraderScore(actor_id=cand.wallet, source=Source.PUMPFUN,
                                score=0.5, tracked=True)
                )

        wallets = [t.actor_id for t in self.store.tracked_actors(Source.PUMPFUN)]
        self.pumpfun.set_wallets(wallets)

        x_scores: dict[str, float] = {}
        for acc in self.xfeed.accounts.values():
            scored = self.x_scorer.score(acc.handle, promo_fraction=acc.promo_fraction)
            self.store.upsert_score(scored)
            x_scores[acc.handle] = scored.score
        if x_scores:
            self.xfeed.set_accounts(x_scores)

        log.info(
            "tracking %d wallets, %d X accounts, %d wallet clusters",
            len(wallets), len(self.xfeed.accounts), self.clusters.size(),
        )

    async def _grade_calls_loop(self) -> None:
        """Grade X calls 24h after they were made - this is what turns
        handles into scores."""
        while True:
            await asyncio.sleep(1800)
            try:
                cutoff = time.time() - 24 * 3600
                for row in self.store.ungraded_calls(cutoff):
                    market = await self.dex.get(row["token_mint"])
                    if not market or not market.price_usd:
                        continue
                    entry = row["price_at_call"] or market.price_usd
                    # DexScreener does not expose a 24h high, so the current
                    # price and the 24h change are used to bound the range.
                    change = (market.price_change_24h or 0) / 100.0
                    prior = market.price_usd / (1 + change) if change > -1 else entry
                    self.store.grade_x_call(
                        row["id"],
                        max(market.price_usd, prior, entry),
                        min(market.price_usd, prior, entry),
                        market.price_usd,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("call grading failed")

    async def _quiet_hours_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                if self.gate.deferred_count and not self.gate.in_quiet_hours():
                    held = self.gate.take_deferred()
                    if held:
                        await self.telegram.send(
                            f"🌅 <b>{len(held)} signal(s) held overnight</b>\n"
                            "<i>These are hours old - context, not live calls.</i>"
                        )
                        for cand in held:
                            await self.telegram.send(
                                format_buy(cand, provider=self.trade_provider,
                                           shadow=self.shadow, mode=self.execution.mode)
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("deferred-alert flush failed")

    async def _daily_report_loop(self) -> None:
        if not self.cfg.get("runtime.daily_report", True):
            return
        hour = self.cfg.get("runtime.daily_report_hour", 9)
        while True:
            now = self.gate._local_now()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep(max(60, target.timestamp() - now.timestamp()))
            try:
                await self.send_daily_report()
                self.store.prune(self.cfg.get("storage.retain_signals_days", 90))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily report failed")

    async def send_daily_report(self) -> None:
        day_start = self.gate._day_start()
        await self.telegram.send(
            format_daily_report(
                alerts_sent=self.store.alerts_since(day_start, "buy"),
                candidates_seen=self._candidates_seen,
                rejections=self.store.rejection_summary(day_start),
                open_positions=self.store.open_positions(),
                budget_status=self.xfeed.budget.status(),
                mode=self.execution.mode,
                shadow=self.shadow,
            )
        )
        self._candidates_seen = 0

    # --- telegram commands -------------------------------------------------
    def _register_commands(self) -> None:
        t = self.telegram

        async def help_cmd(_args: list[str]) -> str:
            return (
                "<b>memebot</b>\n\n"
                "/status - mode, breakers, tracked traders\n"
                "/mode [alerts|paper|live] - switch execution mode\n"
                "/panic - stop everything now\n"
                "/resume - clear a tripped circuit breaker\n"
                "/positions - open positions\n"
                "/watchlist - candidates below the alert threshold\n"
                "/budget - X data spend this month\n"
                "/traders - who is being followed\n"
                "/report - send the daily report now\n"
                "/pause, /unpause - stop/start processing signals\n"
                "/shadow [on|off] - shadow mode\n"
                "/readiness - are paper results good enough to go live?\n"
                "/set &lt;key&gt; &lt;value&gt; - change a config value at runtime"
            )

        async def status_cmd(_args: list[str]) -> str:
            return (
                f"{self.execution.status()}\n\n"
                f"<b>Shadow</b> {'on' if self.shadow else 'off'}  ·  "
                f"<b>Paused</b> {'yes' if self._paused else 'no'}\n"
                f"<b>Tracking</b> {self.pumpfun.tracked_count} wallets, "
                f"{len(self.xfeed.accounts)} X accounts\n"
                f"<b>Alerts today</b> {self.gate.alerts_today()}/"
                f"{self.gate.daily_budget}\n"
                f"<b>Queue</b> {self.queue.qsize()}\n"
                f"<b>Signals</b> pump={self.pumpfun.signals_emitted} "
                f"x={self.xfeed.signals_emitted} "
                f"al={self.alphaledger.signals_emitted}"
            )

        async def mode_cmd(args: list[str]) -> str:
            if not args:
                return f"Current mode: <b>{self.execution.mode}</b>. Use /mode alerts|paper|live"
            return await self.execution.set_mode(args[0])

        async def panic_cmd(_args: list[str]) -> str:
            self._paused = True
            return await self.execution.panic()

        async def resume_cmd(_args: list[str]) -> str:
            self._paused = False
            return self.execution.guardrails.resume()

        async def positions_cmd(_args: list[str]) -> str:
            positions = self.store.open_positions()
            if not positions:
                return "No open positions."
            lines = ["<b>Open positions</b>"]
            for p in positions:
                market = await self.dex.get(p.token_mint)
                price = market.price_usd if market else None
                mult = f"{p.multiple(price):.2f}x" if price else "?"
                lines.append(
                    f"  · ${p.token_symbol or p.token_mint[:6]} — "
                    f"{p.size_sol:.3f} SOL · {mult} · {p.age_hours():.1f}h"
                )
            return "\n".join(lines)

        async def budget_cmd(_args: list[str]) -> str:
            proj = self.xfeed.cost_projection()
            return (
                f"<b>X data budget</b>\n{self.xfeed.budget.status()}\n\n"
                f"Projected: ${proj['usd_per_month']:.2f}/month at "
                f"{proj['requests_per_day']:,.0f} requests/day"
            )

        async def traders_cmd(_args: list[str]) -> str:
            wallets = self.store.tracked_actors(Source.PUMPFUN)[:10]
            accounts = self.store.tracked_actors(Source.X)[:10]
            lines = [f"<b>Wallets</b> ({len(wallets)} shown)"]
            lines += [
                f"  · {w.actor_id[:4]}..{w.actor_id[-4:]} — score {w.score:.2f}, "
                f"win {w.win_rate:.0%}, {w.closed_trades} trades"
                for w in wallets
            ] or ["  (none yet)"]
            lines += [f"\n<b>X accounts</b> ({len(accounts)} shown)"]
            lines += [
                f"  · @{a.actor_id} — score {a.score:.2f}, hit {a.hit_rate:.0%}, "
                f"{a.graded_calls} graded"
                for a in accounts
            ] or ["  (none yet)"]
            return "\n".join(lines)

        async def watchlist_cmd(_args: list[str]) -> str:
            signals = self.store.signals_since(
                time.time() - self.cfg.get("consensus.window_minutes", 30) * 60
            )
            cands = [
                c for c in self.consensus.score_all(signals, self.clusters)
                if c.tier is not Tier.NONE
            ][:10]
            if not cands:
                return "Nothing on the watchlist right now."
            return "\n".join(
                [f"<b>Watchlist</b>"]
                + [
                    f"  · ${c.token_symbol or c.token_mint[:6]} — "
                    f"conviction {c.conviction:.1f}, {c.independent_actors} actors, "
                    f"{len(c.distinct_sources)} sources [{c.tier.value}]"
                    for c in cands
                ]
            )

        async def pause_cmd(_args: list[str]) -> str:
            self._paused = True
            return "⏸ Paused. Signals are still recorded but nothing will be evaluated."

        async def unpause_cmd(_args: list[str]) -> str:
            self._paused = False
            return "▶️ Resumed."

        async def shadow_cmd(args: list[str]) -> str:
            if args and args[0].lower() in ("on", "off"):
                self.shadow = args[0].lower() == "on"
            return f"Shadow mode is {'on' if self.shadow else 'off'}."

        async def readiness_cmd(args: list[str]) -> str:
            mode = args[0] if args and args[0] in ("paper", "live") else "paper"
            assessor = ReadinessAssessor(self.cfg, self.store)
            return assessor.render(assessor.assess(mode=mode), html=True)

        async def report_cmd(_args: list[str]) -> str:
            await self.send_daily_report()
            return ""

        async def set_cmd(args: list[str]) -> str:
            if len(args) < 2:
                return "Usage: /set consensus.tiers.strong.min_conviction 5.0"
            key, raw = args[0], " ".join(args[1:])
            try:
                current = self.cfg.get(key)
            except KeyError:
                return f"Unknown config key: {key}"
            try:
                value: Any = type(current)(raw) if not isinstance(current, bool) \
                    else raw.lower() in ("true", "1", "yes", "on")
            except (TypeError, ValueError):
                return f"Could not parse {raw!r} as {type(current).__name__}"
            self.cfg.set(key, value)
            self._rebuild_tunables()
            return (
                f"Set <code>{key}</code> = {value} (was {current}).\n"
                "<i>Runtime only - edit config.yaml to persist.</i>"
            )

        for name, fn in (
            ("start", help_cmd), ("help", help_cmd), ("status", status_cmd),
            ("mode", mode_cmd), ("panic", panic_cmd), ("resume", resume_cmd),
            ("positions", positions_cmd), ("budget", budget_cmd),
            ("traders", traders_cmd), ("watchlist", watchlist_cmd),
            ("pause", pause_cmd), ("unpause", unpause_cmd),
            ("shadow", shadow_cmd), ("report", report_cmd), ("set", set_cmd),
            ("readiness", readiness_cmd),
        ):
            t.register(name, fn)

    def _rebuild_tunables(self) -> None:
        """Re-read config-derived components after a runtime /set."""
        self.consensus = ConsensusEngine(self.cfg)
        self.gate = AlertGate(self.cfg, self.store)
        self.safety = SafetyGate(self.cfg, self.dex, self.rug)

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.telegram.set_my_commands({
            "status": "Mode, breakers, tracked traders",
            "mode": "Switch alerts/paper/live",
            "panic": "Stop everything now",
            "positions": "Open positions",
            "watchlist": "Candidates below alert threshold",
            "budget": "X data spend this month",
            "traders": "Who is being followed",
            "report": "Send the daily report",
            "readiness": "Are paper results good enough to go live?",
            "help": "All commands",
        })

        await self.rescore_all()

        async def on_source_failure(name: str, count: int) -> None:
            await self.telegram.send(
                f"⚠️ Source <b>{name}</b> has failed {count} times in a row. "
                f"It is being retried, but coverage is degraded."
            )

        self._tasks = [
            asyncio.create_task(self.pumpfun.run_forever(self.queue, on_source_failure)),
            asyncio.create_task(self.xfeed.run_forever(self.queue, on_source_failure)),
            asyncio.create_task(self.alphaledger.run_forever(self.queue, on_source_failure)),
            asyncio.create_task(self._consume_signals()),
            asyncio.create_task(self._exit_loop()),
            asyncio.create_task(self._rescore_loop()),
            asyncio.create_task(self._grade_calls_loop()),
            asyncio.create_task(self._quiet_hours_loop()),
            asyncio.create_task(self._daily_report_loop()),
            asyncio.create_task(self.telegram.poll_commands()),
        ]

        await self.telegram.send(
            f"🤖 <b>memebot started</b>\n\n"
            f"Mode: <b>{self.execution.mode}</b>"
            f"{' (shadow)' if self.shadow else ''}\n"
            f"Alert budget: {self.gate.daily_budget}/day\n"
            f"Tracking {self.pumpfun.tracked_count} wallets, "
            f"{len(self.xfeed.accounts)} X accounts\n\n"
            f"/help for commands."
        )
        log.info("memebot started in %s mode", self.execution.mode)

    async def run(self) -> None:
        await self.start()
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        log.info("shutting down")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for closable in (
            self.telegram, self.xfeed, self.alphaledger,
            self.dex, self.rug, self.boosts, self.execution,
        ):
            with contextlib.suppress(Exception):
                await closable.close()
        self.store.close()
