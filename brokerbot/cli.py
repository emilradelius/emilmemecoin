"""brokerbot command line.

    python -m brokerbot.cli costs
    python -m brokerbot.cli backtest --csv data/omxs30.csv --symbol OMXS30 --strategy sma_crossover
    python -m brokerbot.cli backtest --synthetic --strategy momentum --costs etoro
    python -m brokerbot.cli walkforward --synthetic --strategy sma_crossover
    python -m brokerbot.cli noise --strategy sma_crossover
    python -m brokerbot.cli broker --check paper
    python -m brokerbot.cli preflight --broker saxo --symbols VOLV-B.ST
    python -m brokerbot.cli trial --broker saxo --symbols VOLV-B.ST,ERIC-B.ST --days 7
    python -m brokerbot.cli trial --report
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .backtest.engine import BacktestEngine
from .backtest.metrics import render
from .backtest.walkforward import WalkForwardValidator
from .costs import PRESETS
from .data.base import BarSource
from .data.csv_source import CsvBarSource
from .data.synthetic import ASSET_PRESETS, random_walk
from .models import Bar
from .live import BarStore, LiveRunner
from .strategy.library import REGISTRY
from .trial import TrialTracker

GRIDS = {
    "sma_crossover": {"fast": [5, 10, 20, 50], "slow": [30, 60, 100, 200]},
    "momentum": {"lookback": [21, 63, 126, 252], "threshold": [0.0, 0.02, 0.05]},
    "mean_reversion": {"window": [10, 20, 50], "entry_z": [-1.0, -1.5, -2.0]},
    "buy_and_hold": {},
    "price_vs_sma": {"window": [20, 50, 100, 200]},
}


def _synthetic_kwargs(args) -> dict:
    """Preset settings, with an explicit --trend still winning.

    ``--trend`` defaults to None rather than 0.0 so that leaving it alone
    means "whatever the preset says" instead of silently flattening the
    momentum a preset deliberately includes.
    """
    kwargs: dict = {"bars": args.bars, "seed": args.seed}
    if getattr(args, "preset", None):
        kwargs["preset"] = args.preset
    if args.trend is not None:
        kwargs["trend_strength"] = args.trend
    return kwargs


def _load_bars(args) -> list[Bar]:
    if args.synthetic:
        return random_walk(args.symbol or "SYNTH", **_synthetic_kwargs(args))
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

    ``--preset`` picks the asset class to imitate, because the baseline is
    only meaningful if the synthetic paths resemble what you are really
    trading: a strategy scored against 1.2% daily equity noise will look far
    too good on crypto. Drift and momentum are forced to zero here whatever
    the preset carries, since a baseline with an edge in it is not a baseline.
    """
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")
    engine = _engine(args)
    results: list[float] = []
    beats = 0
    for seed in range(args.runs):
        bars = random_walk(
            "NOISE", **{**_synthetic_kwargs(args), "seed": seed,
                        "drift": 0.0, "trend_strength": args.trend or 0.0},
        )
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


def cmd_pine(args) -> int:
    from .pine import export, traps_text

    if args.traps:
        print(traps_text())
        return 0
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")
    try:
        script = export(cls(), PRESETS[args.costs], initial_capital=args.cash)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.out:
        Path(args.out).write_text(script)
        print(f"wrote {args.out}")
    else:
        print(script)
    print(
        "\nPaste into TradingView's Pine Editor, add to the chart, then compare "
        "the Strategy Tester's numbers against:\n"
        f"  python -m brokerbot.cli backtest --strategy {args.strategy} "
        f"--csv <same data> --costs {args.costs}\n"
        "Material disagreement means one of them is wrong. "
        "`--traps` lists the usual reasons.",
        file=__import__("sys").stderr,
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




def _build_broker(name: str, args):
    """Construct a broker from the environment. Demo/simulation by default."""
    import os
    if name == "paper":
        from .brokers.paper import PaperBroker
        return PaperBroker(PRESETS[args.costs], starting_cash=args.cash,
                           quotes=_build_quotes(args))
    if name == "saxo":
        from .brokers.saxo import SaxoBroker
        return SaxoBroker(
            os.getenv("SAXO_TOKEN") or None,
            simulation=True,
            refresh_token=os.getenv("SAXO_REFRESH_TOKEN"),
            client_id=os.getenv("SAXO_CLIENT_ID"),
            client_secret=os.getenv("SAXO_CLIENT_SECRET"),
            token_store=Path(args.state_dir) / "saxo_tokens.json",
        )
    if name == "ibkr":
        from .brokers.ibkr import IbkrBroker
        return IbkrBroker()
    if name == "etoro":
        from .brokers.etoro import EtoroBroker
        key = os.getenv("ETORO_API_KEY", "")
        if not key:
            raise SystemExit("set ETORO_API_KEY from https://builders.etoro.com/")
        return EtoroBroker(key, demo=True)
    raise SystemExit(f"unknown broker {name!r}")


def _build_quotes(args):
    """Price feed for the paper broker. Real brokers quote their own prices."""
    if getattr(args, "quotes", "yahoo") != "yahoo":
        return None
    from .data.yahoo import YahooBarSource
    return YahooBarSource()


def _build_news(args):
    if not args.news:
        return None
    import os
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        print("--news needs ANTHROPIC_API_KEY (or `ant auth login`). "
              "Continuing without news.\n")
        return None
    from .news.classify import NewsClassifier
    from .news.pipeline import NewsPipeline
    symbols = {s.strip() for s in args.symbols.split(",") if s.strip()}
    return NewsPipeline(
        classifier=NewsClassifier(
            monthly_usd_cap=args.news_cap,
            state_path=None,
        ),
        watchlist=symbols or None,
    )


def cmd_history(args) -> int:
    """Export real daily bars to CSV so backtests stay reproducible.

    Yahoo revises its history. A backtest keyed to the live feed quietly
    changes underneath you between runs, which is indistinguishable from your
    own edits having done something. A file on disk does not move.
    """
    import csv

    from .data.base import BarSource
    from .data.yahoo import YahooBarSource, YahooError

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is required, e.g. --symbols VOLV-B.ST")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = YahooBarSource()
    failures = 0

    for symbol in symbols:
        try:
            bars = source.load(symbol, range=f"{args.years}y")
        except YahooError as exc:
            print(f"{symbol:14} FAILED  {exc}")
            failures += 1
            continue
        if not bars:
            print(f"{symbol:14} FAILED  no bars returned")
            failures += 1
            continue

        path = out_dir / f"{symbol.replace('^', '_')}.csv"
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            for bar in bars:
                writer.writerow([
                    bar.ts.date(), f"{bar.open:.4f}", f"{bar.high:.4f}",
                    f"{bar.low:.4f}", f"{bar.close:.4f}", int(bar.volume),
                ])

        # Say so rather than staying silent: a flaw here does not produce an
        # error, it produces a confident and wrong backtest.
        problems = BarSource.validate(bars)
        note = "clean" if not problems else f"{len(problems)} WARNING(S)"
        print(f"{symbol:14} {len(bars):5} bars  {bars[0].ts.date()} -> "
              f"{bars[-1].ts.date()}  {source.currency_of(symbol) or '?':4} {note}")
        for problem in problems[:3]:
            print(f"               ! {problem}")

    print(f"\nWrote to {out_dir}/. Back these up - they are what makes a "
          f"result you can reproduce next month.")
    return 1 if failures else 0


def cmd_preflight(args) -> int:
    """Verify every moving part before committing a week to the run."""
    import os

    print("Preflight - checking each dependency the trial needs.\n")
    problems: list[str] = []

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"[ok]   symbols: {', '.join(symbols) or '(none)'}")
    if not symbols:
        problems.append("no symbols given (--symbols VOLV-B.ST,ERIC-B.ST)")

    try:
        broker = _build_broker(args.broker, args)
        print(f"[ok]   broker adapter: {broker.name}")
    except (SystemExit, ValueError) as exc:
        print(f"[FAIL] broker: {exc}")
        return 1

    async def checks() -> None:
        connected = await broker.connect()
        print(f"[{'ok' if connected else 'FAIL'}]   broker connect")
        if not connected:
            problems.append("broker would not connect - check credentials")
        else:
            acct = await broker.account()
            print(f"[ok]   account: {acct.equity:,.2f} {acct.currency}")
            quotes = getattr(broker, "quotes", None)
            for sym in symbols:
                price = await broker.last_price(sym)
                if not price:
                    print(f"[FAIL] no price for {sym}")
                    problems.append(f"no price available for {sym}")
                    continue

                ccy = quotes.currency_of(sym) if quotes is not None else None
                print(f"[ok]   price {sym}: {price:,.4g} {ccy or ''}".rstrip())

                # A symbol quoted in the wrong currency still produces a
                # perfectly plausible fill, a perfectly plausible equity
                # curve, and a number that means nothing. AAPL at 260 USD
                # spends 260 SEK of a SEK account and nobody notices.
                if ccy and ccy != acct.currency:
                    print(f"[FAIL] {sym} is quoted in {ccy}, account is in "
                          f"{acct.currency}")
                    problems.append(
                        f"{sym} quoted in {ccy} but the account is in "
                        f"{acct.currency} - no FX conversion is applied, so "
                        f"position sizes would be wrong. Use symbols from one "
                        f"currency, or set --costs us_equities_from_sek."
                    )
        await broker.close()

    asyncio.run(checks())

    if args.broker == "saxo":
        if os.getenv("SAXO_REFRESH_TOKEN"):
            print("[ok]   Saxo OAuth2 refresh configured - survives multi-day runs")
        else:
            print("[WARN] static Saxo token: expires within 24h, so a 7-day "
                  "run will stop after day one. Configure the refresh flow.")
            problems.append("static Saxo token cannot survive 7 days")

    news = _build_news(args)
    if args.news:
        print(f"[{'ok' if news else 'WARN'}]   news pipeline")

    print()
    if problems:
        print("Preflight found problems:")
        for p in problems:
            print(f"  - {p}")
        print("\nFix these first. A week-long run on a broken setup wastes a week.")
        return 1
    print("Preflight clean. Start the trial with:")
    print(f"  python -m brokerbot.cli trial --broker {args.broker} "
          f"--symbols {args.symbols} --days {getattr(args, 'days', 7)}")
    return 0


def cmd_trial(args) -> int:
    tracker = TrialTracker(args.state_dir, cycle_seconds=args.cycle_seconds,
                           days=args.days)

    if args.report:
        print(tracker.render(tracker.assess()))
        return 0 if tracker.assess().operationally_sound else 1

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is required, e.g. --symbols VOLV-B.ST")

    cls = REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"unknown strategy. Options: {', '.join(REGISTRY)}")

    broker = _build_broker(args.broker, args)

    # Warmup history. A CSV you exported stays reproducible, so it wins when
    # given. Otherwise fall back to the same feed the paper broker quotes
    # from: without it, a strategy with a 50-day warmup produces no signal at
    # all in a 7-day trial, and the run measures nothing.
    if args.csv:
        history = CsvBarSource(args.csv)
    else:
        history = _build_quotes(args)
        if history is not None:
            print(f"No --csv given - seeding warmup from {history.name}. "
                  "Export a CSV if you need the run to be reproducible.\n")
    if history is None:
        print("No --csv history given. Strategies with a warmup window will "
              "not signal until enough live days accumulate.\n")

    runner = LiveRunner(
        broker, cls(), symbols,
        costs=PRESETS[args.costs],
        bar_store=BarStore(Path(args.state_dir) / "bars.json"),
        history_source=history,
        news_pipeline=_build_news(args),
        cycle_seconds=args.cycle_seconds,
        dry_run=not args.live_orders,
        state_dir=args.state_dir,
        on_event=tracker.record,
    )

    if args.live_orders:
        print("*** --live-orders: real orders will be sent to the DEMO "
              "account. Ctrl-C or `touch "
              f"{Path(args.state_dir) / 'STOP'}` to stop. ***\n")
    else:
        print("Dry-run: orders are logged, not sent. Add --live-orders to "
              "place them on the demo account.\n")

    started = tracker.start(dry_run=not args.live_orders)
    until = started + timedelta(days=args.days)
    try:
        asyncio.run(runner.run(until=until))
    except KeyboardInterrupt:
        print("\ninterrupted")

    print("\n" + tracker.render(tracker.assess()))
    return 0


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
        p.add_argument("--trend", type=float, default=None,
                       help="synthetic trend strength; 0 = efficient market. "
                            "Default: whatever --preset specifies.")
        p.add_argument("--preset", choices=sorted(ASSET_PRESETS),
                       help="asset class for synthetic data. The noise "
                            "baseline is only valid for the class it imitates "
                            "- equity settings badly understate crypto.")
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

    def live_common(p):
        p.add_argument("--broker", default="paper",
                       choices=["paper", "saxo", "ibkr", "etoro"])
        p.add_argument("--symbols", default="")
        p.add_argument("--csv", help="historical bars to seed the strategy warmup")
        p.add_argument("--quotes", default="yahoo", choices=["yahoo", "none"],
                       help="price feed for --broker paper. Real brokers quote "
                            "their own prices and ignore this.")
        p.add_argument("--strategy", default="news_drift", choices=sorted(REGISTRY))
        p.add_argument("--costs", default="nordic_equities", choices=sorted(PRESETS))
        p.add_argument("--cash", type=float, default=100_000.0)
        p.add_argument("--state-dir", default="data/live")
        p.add_argument("--cycle-seconds", type=float, default=900.0)
        p.add_argument("--news", action="store_true", help="enable news classification")
        p.add_argument("--news-cap", type=float, default=10.0,
                       help="monthly USD cap for classification")

    p = sub.add_parser("history")
    p.add_argument("--symbols", required=True,
                   help="comma-separated Yahoo symbols, e.g. VOLV-B.ST,ERIC-B.ST")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--out-dir", default="data/history")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("preflight"); live_common(p)
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("trial"); live_common(p)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--live-orders", action="store_true",
                   help="actually send orders to the demo account")
    p.add_argument("--report", action="store_true",
                   help="assess a finished or in-progress trial and exit")
    p.set_defaults(func=cmd_trial)

    p = sub.add_parser("pine")
    p.add_argument("--strategy", default="price_vs_sma", choices=sorted(REGISTRY))
    p.add_argument("--costs", default="nordic_equities", choices=sorted(PRESETS))
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--out")
    p.add_argument("--traps", action="store_true",
                   help="list what inflates TradingView Strategy Tester results")
    p.set_defaults(func=cmd_pine)

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
