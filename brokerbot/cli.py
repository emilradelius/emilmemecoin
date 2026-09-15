"""brokerbot command line.

    python -m brokerbot.cli costs
    python -m brokerbot.cli backtest --csv data/omxs30.csv --symbol OMXS30 --strategy sma_crossover
    python -m brokerbot.cli backtest --synthetic --strategy momentum --costs etoro
    python -m brokerbot.cli walkforward --synthetic --strategy sma_crossover
    python -m brokerbot.cli noise --strategy sma_crossover
    python -m brokerbot.cli broker --check paper
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
from datetime import datetime

from .backtest.engine import BacktestEngine
from .backtest.metrics import render
from .backtest.walkforward import WalkForwardValidator
from .costs import PRESETS
from .data.base import BarSource
from .data.csv_source import CsvBarSource
from .data.synthetic import random_walk
from .models import Bar
from .strategy.library import REGISTRY

GRIDS = {
    "sma_crossover": {"fast": [5, 10, 20, 50], "slow": [30, 60, 100, 200]},
    "momentum": {"lookback": [21, 63, 126, 252], "threshold": [0.0, 0.02, 0.05]},
    "mean_reversion": {"window": [10, 20, 50], "entry_z": [-1.0, -1.5, -2.0]},
    "buy_and_hold": {},
}


def _load_bars(args) -> list[Bar]:
    if args.synthetic:
        return random_walk(
            args.symbol or "SYNTH", bars=args.bars, seed=args.seed,
            trend_strength=args.trend,
        )
    if not args.csv:
        raise SystemExit("provide --csv PATH or --synthetic")
    src = CsvBarSource(args.csv)
    bars = src.load(args.symbol or "ASSET")
    problems = BarSource.validate(bars)
    if problems:
        print("Data quality warnings:")
        for p in problems:
            print(f"  - {p}")
        print("Bad data produces confident, wrong answers. Fix these first.\n")
    return bars


def _engine(args) -> BacktestEngine:
    costs = PRESETS.get(args.costs)
    if costs is None:
        raise SystemExit(f"unknown cost preset {args.costs!r}. "
                         f"Options: {', '.join(PRESETS)}")
    return BacktestEngine(costs, starting_cash=args.cash,
                          needs_fx=costs.fx_pct > 0,
                          min_order_value=args.min_order)


def cmd_costs(args) -> int:
    print("Round-trip cost by broker profile (buy then sell):\n")
    print(f"{'preset':<24} {'10,000':>10} {'50,000':>10}")
    print("-" * 46)
    for name, m in PRESETS.items():
        fx = m.fx_pct > 0
        print(f"{name:<24} {m.round_trip_pct(10_000, needs_conversion=fx):>9.2%} "
              f"{m.round_trip_pct(50_000, needs_conversion=fx):>9.2%}")
    print(
        "\nThis is the hurdle. A strategy trading weekly under the eToro "
        "profile must earn ~65% a year in gross edge just to break even."
    )
    return 0


def cmd_backtest(args) -> int:
    bars = _load_bars(args)
    if len(bars) < 30:
        raise SystemExit(f"only {len(bars)} bars - not enough to backtest")
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")
    strat = cls()
    report = _engine(args).compare_to_benchmark(
        strat, bars, symbol=bars[0].symbol
    )
    print(render(report))
    return 0 if report.beats_benchmark else 1


def cmd_walkforward(args) -> int:
    bars = _load_bars(args)
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")
    validator = WalkForwardValidator(_engine(args), windows=args.windows)
    report = validator.run(cls, bars, GRIDS.get(args.strategy, {}))
    print(report.render())
    return 0 if report.robust else 1


def cmd_noise(args) -> int:
    """Run a strategy on many random walks containing no signal at all.

    The most useful calibration available. Prices here are pure noise, so the
    true edge is exactly zero. Whatever spread of returns this prints is the
    performance the method manufactures from nothing - and any real backtest
    result has to stand clearly outside it to mean anything.
    """
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")
    engine = _engine(args)
    results: list[float] = []
    beats = 0
    for seed in range(args.runs):
        bars = random_walk("NOISE", bars=args.bars, seed=seed, drift=0.0)
        rep = engine.compare_to_benchmark(cls(), bars)
        results.append(rep.strategy.total_return)
        beats += bool(rep.beats_benchmark)

    results.sort()
    print(f"{cls().describe()} on {args.runs} pure random walks "
          f"({args.bars} bars, zero true edge)\n")
    print(f"  best        {results[-1]:+.1%}")
    print(f"  90th pct    {results[int(0.9 * len(results))]:+.1%}")
    print(f"  median      {statistics.median(results):+.1%}")
    print(f"  10th pct    {results[int(0.1 * len(results))]:+.1%}")
    print(f"  worst       {results[0]:+.1%}")
    print(f"\n  beat buy-and-hold in {beats}/{args.runs} runs by luck alone")
    print(
        f"\nA backtest on real data only means something if it clears the 90th "
        f"percentile here ({results[int(0.9 * len(results))]:+.1%}). Anything "
        f"below that is indistinguishable from noise."
    )
    return 0


def cmd_broker(args) -> int:
    async def check() -> int:
        if args.check == "paper":
            from .brokers.paper import PaperBroker
            b = PaperBroker(PRESETS[args.costs], starting_cash=args.cash)
            b.set_price("TEST", 100.0)
        elif args.check == "saxo":
            import os
            from .brokers.saxo import SaxoBroker
            token = os.getenv("SAXO_TOKEN", "")
            if not token:
                print("Set SAXO_TOKEN. Get a 24h simulation token free at "
                      "https://www.developer.saxo/openapi/token")
                return 1
            b = SaxoBroker(token, simulation=not args.live)
        elif args.check == "ibkr":
            from .brokers.ibkr import IbkrBroker
            b = IbkrBroker()
        elif args.check == "etoro":
            import os
            from .brokers.etoro import EtoroBroker
            key = os.getenv("ETORO_API_KEY", "")
            if not key:
                print("Set ETORO_API_KEY from https://builders.etoro.com/")
                return 1
            b = EtoroBroker(key, demo=not args.live)
        else:
            print(f"unknown broker {args.check!r}")
            return 1

        ok = await b.connect()
        print(f"connect: {'OK' if ok else 'FAILED'}")
        if ok:
            acct = await b.account()
            print(f"account: {acct.equity:,.2f} {acct.currency} "
                  f"(cash {acct.cash:,.2f})")
            for p in await b.positions():
                print(f"  {p.symbol}: {p.quantity} @ {p.avg_price}")
        await b.close()
        return 0 if ok else 1

    return asyncio.run(check())


def main() -> int:
    parser = argparse.ArgumentParser(prog="brokerbot", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--csv")
        p.add_argument("--symbol")
        p.add_argument("--synthetic", action="store_true")
        p.add_argument("--bars", type=int, default=1500)
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--trend", type=float, default=0.0,
                       help="synthetic trend strength; 0 = efficient market")
        p.add_argument("--costs", default="nordic_equities",
                       choices=sorted(PRESETS))
        p.add_argument("--cash", type=float, default=100_000.0)
        p.add_argument("--min-order", type=float, default=500.0)

    sub.add_parser("costs").set_defaults(func=cmd_costs)

    p = sub.add_parser("backtest"); common(p)
    p.add_argument("--strategy", default="sma_crossover", choices=sorted(REGISTRY))
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("walkforward"); common(p)
    p.add_argument("--strategy", default="sma_crossover", choices=sorted(REGISTRY))
    p.add_argument("--windows", type=int, default=5)
    p.set_defaults(func=cmd_walkforward)

    p = sub.add_parser("noise"); common(p)
    p.add_argument("--strategy", default="sma_crossover", choices=sorted(REGISTRY))
    p.add_argument("--runs", type=int, default=100)
    p.set_defaults(func=cmd_noise)

    p = sub.add_parser("broker")
    p.add_argument("--check", required=True,
                   choices=["paper", "saxo", "ibkr", "etoro"])
    p.add_argument("--live", action="store_true",
                   help="use the real account instead of simulation")
    p.add_argument("--costs", default="nordic_equities", choices=sorted(PRESETS))
    p.add_argument("--cash", type=float, default=100_000.0)
    p.set_defaults(func=cmd_broker)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
