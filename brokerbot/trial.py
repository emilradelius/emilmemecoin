"""The 7-day demo trial: what it can and cannot tell you.

**A week cannot tell you whether the strategy makes money.** The readiness
gate in ``memebot`` wants 30 closed trades over 21 days before it will take a
result seriously, and for good reason - below that, returns are dominated by
noise. The drift strategy here holds positions for twenty days, so a seven-day
run will finish with somewhere between zero and two completed trades. Any
profit or loss figure from that is a coin flip, and reading it as evidence is
the single most expensive mistake available at this stage.

What a week *is* excellent for is finding out whether the machine works. Every
one of these has stopped a real trial dead, and none of them show up in a
backtest:

* the broker token expired overnight and nothing traded again
* the process died on day three and looked identical to "no signals today"
* local position state drifted from the broker's after a partial fill
* the news classifier cost ten times the projection
* every order was rejected for a reason the backtest never modelled

So this scores **operational** criteria and deliberately refuses to render a
verdict on profitability. Passing means "the plumbing works, now run it long
enough to learn something". It does not mean "this is profitable".
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .live import CycleResult
from .models import to_local, utcnow

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass
class TrialReport:
    started_at: datetime
    ended_at: datetime
    expected_cycles: int = 0
    completed_cycles: int = 0
    failed_cycles: int = 0
    connection_failures: int = 0
    orders_placed: int = 0
    orders_rejected: int = 0
    drift_events: int = 0
    price_gaps: int = 0
    news_ingested: int = 0
    news_tradeable: int = 0
    news_cost_usd: float = 0.0
    longest_gap_hours: float = 0.0
    equity_first: float = 0.0
    equity_last: float = 0.0
    dry_run: bool = True
    checks: list[Check] = field(default_factory=list)

    downtime_hours: float = 0.0
    """Total time spent in gaps longer than twice the expected cadence."""

    @property
    def uptime(self) -> float:
        """Fraction of the run actually covered by cycles.

        Measured from observed gaps rather than from
        ``completed / expected``. A ratio against a configured interval breaks
        the moment the real cadence differs from the configured one - after a
        restart with different settings, or a slow cycle - and reports a
        nonsense percentage rather than an obviously wrong one.
        """
        elapsed = (self.ended_at - self.started_at).total_seconds() / 3600.0
        if elapsed <= 0:
            return 1.0 if self.completed_cycles else 0.0
        return max(0.0, min(1.0, 1.0 - self.downtime_hours / elapsed))

    @property
    def days(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 86400.0

    @property
    def news_cost_per_month(self) -> float:
        return (self.news_cost_usd / self.days * 30.4) if self.days > 0 else 0.0

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.critical and not c.passed]

    @property
    def operationally_sound(self) -> bool:
        return not self.blocking and self.completed_cycles > 0


class TrialTracker:
    """Records cycles to disk and scores the run at the end."""

    def __init__(self, state_dir: Path | str = "data/live",
                 *, cycle_seconds: float = 900.0, days: int = 7) -> None:
        self.dir = Path(state_dir)
        self.log_path = self.dir / "trial_cycles.jsonl"
        self.meta_path = self.dir / "trial_meta.json"
        self.cycle_seconds = cycle_seconds
        self.days = days

    # --- recording --------------------------------------------------------
    def start(self, *, dry_run: bool) -> datetime:
        self.dir.mkdir(parents=True, exist_ok=True)
        started = utcnow()
        self.meta_path.write_text(json.dumps({
            "started_at": started.isoformat(),
            "planned_end": (started + timedelta(days=self.days)).isoformat(),
            "cycle_seconds": self.cycle_seconds,
            "days": self.days,
            "dry_run": dry_run,
        }, indent=2))
        log.info("trial started, planned end %s",
                 (started + timedelta(days=self.days)).isoformat())
        return started

    def record(self, result: CycleResult) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(result.to_json()) + "\n")
        except OSError as exc:
            log.warning("could not append to trial log: %s", exc)

    def _load_cycles(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows: list[dict] = []
        with self.log_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    # --- scoring ----------------------------------------------------------
    def assess(self) -> TrialReport:
        rows = self._load_cycles()
        meta = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text())
            except (OSError, ValueError):
                pass

        if not rows:
            now = utcnow()
            r = TrialReport(started_at=now, ended_at=now)
            r.checks.append(Check(
                "any_cycles", False,
                "No cycles recorded. The runner never completed a cycle - "
                "check that it actually started and could reach the broker.",
            ))
            return r

        stamps = [datetime.fromisoformat(r["ts"]) for r in rows]
        started = (
            datetime.fromisoformat(meta["started_at"]) if meta.get("started_at")
            else stamps[0]
        )
        report = TrialReport(
            started_at=started, ended_at=stamps[-1],
            dry_run=bool(meta.get("dry_run", True)),
        )

        report.completed_cycles = sum(1 for r in rows if r.get("ok"))
        report.failed_cycles = sum(1 for r in rows if not r.get("ok"))
        report.connection_failures = sum(1 for r in rows if not r.get("connected"))
        report.orders_placed = sum(r.get("orders_placed", 0) for r in rows)
        report.orders_rejected = sum(r.get("orders_rejected", 0) for r in rows)
        report.drift_events = sum(1 for r in rows if r.get("reconcile_drift"))
        report.price_gaps = sum(
            1 for r in rows
            for e in r.get("errors", []) if "no price" in str(e)
        )
        report.news_ingested = sum(r.get("news_ingested", 0) for r in rows)
        report.news_tradeable = sum(r.get("news_tradeable", 0) for r in rows)
        report.news_cost_usd = sum(r.get("news_cost_usd", 0.0) for r in rows)

        elapsed = (report.ended_at - report.started_at).total_seconds()
        report.expected_cycles = max(1, int(elapsed / self.cycle_seconds))

        # Longest silence between cycles - a six-hour hole means it was down,
        # and an average would hide it completely.
        if len(stamps) > 1:
            gaps = [
                (b - a).total_seconds() / 3600.0
                for a, b in zip(stamps, stamps[1:])
            ]
            report.longest_gap_hours = round(max(gaps), 2)
            # Anything beyond twice the expected cadence counts as downtime;
            # normal jitter does not.
            threshold = (self.cycle_seconds * 2) / 3600.0
            report.downtime_hours = round(
                sum(g - threshold for g in gaps if g > threshold), 3
            )

        equities = [r.get("equity", 0.0) for r in rows if r.get("equity")]
        if equities:
            report.equity_first, report.equity_last = equities[0], equities[-1]

        self._build_checks(report)
        return report

    def _build_checks(self, r: TrialReport) -> None:
        c = r.checks.append

        c(Check(
            "ran_long_enough", r.days >= self.days * 0.9,
            f"Ran {r.days:.1f} of {self.days} planned days.",
        ))

        c(Check(
            "uptime", r.uptime >= 0.90,
            f"{r.completed_cycles} cycles, {r.uptime:.0%} uptime "
            f"({r.downtime_hours:.1f}h lost to gaps). Below 90% means it was "
            f"down for hours at a time.",
        ))

        # The one that kills most multi-day runs.
        c(Check(
            "credentials_held", r.connection_failures <= 1,
            f"{r.connection_failures} cycles could not connect. Saxo "
            f"simulation tokens expire after 24h and IBKR's gateway needs a "
            f"daily browser login - if this is high, that is why.",
        ))

        max_gap = max(2.0, self.cycle_seconds * 4 / 3600.0)
        c(Check(
            "no_long_outage", r.longest_gap_hours <= max_gap,
            f"Longest gap between cycles: {r.longest_gap_hours:.1f}h. A long "
            f"gap looks exactly like 'no signals today' from the outside.",
        ))

        c(Check(
            "positions_reconcile", r.drift_events == 0,
            f"{r.drift_events} cycles found position drift between our state "
            f"and the broker's. Any drift at all means a bug worth finding "
            f"before real money is involved.",
        ))

        total_orders = r.orders_placed + r.orders_rejected
        reject_rate = r.orders_rejected / total_orders if total_orders else 0.0
        c(Check(
            "orders_accepted", reject_rate <= 0.1,
            f"{r.orders_placed} placed, {r.orders_rejected} rejected "
            f"({reject_rate:.0%}). Rejections are broker rules the backtest "
            f"never modelled.",
        ))

        c(Check(
            "price_data_available", r.price_gaps <= r.expected_cycles * 0.05,
            f"{r.price_gaps} cycles were missing a price for at least one "
            f"symbol.",
            critical=False,
        ))

        if r.news_ingested:
            c(Check(
                "news_cost_in_budget", r.news_cost_per_month <= 15.0,
                f"Classification cost ${r.news_cost_usd:.2f} over {r.days:.1f} "
                f"days = ${r.news_cost_per_month:.2f}/month projected.",
                critical=False,
            ))
            c(Check(
                "news_filter_is_selective", r.news_tradeable < r.news_ingested * 0.1,
                f"{r.news_tradeable} of {r.news_ingested} stories were judged "
                f"tradeable ({r.news_tradeable / r.news_ingested:.1%}). Most of "
                f"a news feed is noise; a filter passing more than ~10% is "
                f"finding signal that is not there.",
                critical=False,
            ))

        c(Check(
            "was_a_dry_run", True,
            "Ran in dry-run mode - orders were logged, not sent."
            if r.dry_run else
            "Placed real orders on the demo account.",
            critical=False,
        ))

    # --- rendering --------------------------------------------------------
    def render(self, r: TrialReport) -> str:
        lines = [
            "7-day demo trial - operational assessment",
            f"{to_local(r.started_at):%Y-%m-%d %H:%M} to "
            f"{to_local(r.ended_at):%Y-%m-%d %H:%M} (local) "
            f"({r.days:.1f} days, {'dry-run' if r.dry_run else 'orders placed'})",
            "",
            f"Cycles      {r.completed_cycles} completed, {r.failed_cycles} failed "
            f"({r.uptime:.0%} uptime)",
            f"Orders      {r.orders_placed} placed, {r.orders_rejected} rejected",
            f"News        {r.news_ingested} ingested, {r.news_tradeable} tradeable, "
            f"${r.news_cost_usd:.2f} spent",
            f"Equity      {r.equity_first:,.0f} -> {r.equity_last:,.0f}",
            "",
        ]
        for check in r.checks:
            mark = "PASS" if check.passed else ("FAIL" if check.critical else "WARN")
            lines.append(f"[{mark}] {check.detail}")

        lines += ["", "-" * 68, ""]
        if r.operationally_sound:
            lines += [
                "VERDICT: the plumbing works.",
                "",
                "This says the system stays up, stays authenticated, keeps its "
                "position state in step with the broker, and places orders the "
                "broker accepts. That is what a week can establish.",
            ]
        else:
            lines += [
                f"VERDICT: not operationally sound - {len(r.blocking)} blocking "
                f"issue(s) above.",
                "",
                "Fix these before extending the run. A longer trial on a broken "
                "runner produces more data of the same worthlessness.",
            ]

        lines += [
            "",
            "WHAT THIS DOES NOT TELL YOU: whether the strategy is profitable.",
            "",
            f"Seven days produced {r.orders_placed} order(s). The drift strategy "
            f"holds for 20 days, so almost nothing has completed a round trip. "
            f"Any profit or loss here is noise - treating it as evidence is the "
            f"most expensive mistake available at this stage. For an answer on "
            f"profitability, run the paper phase for 6-8 weeks and use "
            f"`python -m memebot.tools.readiness` (30+ closed trades, 21+ days) "
            f"or the brokerbot backtester against buy-and-hold.",
        ]
        return "\n".join(lines)
