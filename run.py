#!/usr/bin/env python3
"""memebot entry point.

    python run.py                 # run the bot
    python run.py --check         # validate config and credentials, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from memebot.app import MemeBot
from memebot.config import Config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )
    # These are chatty at DEBUG and drown out everything useful.
    for noisy in ("aiohttp", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def preflight(cfg: Config) -> list[str]:
    """Check the things that will otherwise fail confusingly at 3am."""
    problems: list[str] = []
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        problems.append("TELEGRAM_BOT_TOKEN is not set (required)")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        problems.append("TELEGRAM_CHAT_ID is not set (required) - run python -m memebot.tools.whoami")

    if cfg.get("sources.x.enabled", True) and not os.getenv("X_API_KEY"):
        problems.append(
            "sources.x.enabled is true but X_API_KEY is not set - X coverage "
            "will be dormant, and cross-source consensus needs two live sources"
        )
    if cfg.get("sources.alphaledger.enabled", False) and not os.getenv("ALPHALEDGER_API_KEY"):
        problems.append("sources.alphaledger.enabled is true but ALPHALEDGER_API_KEY is not set")

    if cfg.get("execution.mode", "alerts") == "live":
        if not cfg.get("execution.live_mode_armed", False):
            problems.append("execution.mode is 'live' but execution.live_mode_armed is false")
        if not os.getenv("TRADING_WALLET_PRIVATE_KEY"):
            problems.append("execution.mode is 'live' but TRADING_WALLET_PRIVATE_KEY is not set")

    enabled = [
        name for name, key in (
            ("pumpfun", "sources.pumpfun.enabled"),
            ("x", "sources.x.enabled"),
            ("alphaledger", "sources.alphaledger.enabled"),
        )
        if cfg.get(key, False)
    ]
    if len(enabled) < 2:
        problems.append(
            f"only {len(enabled)} source(s) enabled ({', '.join(enabled) or 'none'}) - "
            "cross-source consensus needs at least two, so STRONG alerts will "
            "never fire with the default thresholds"
        )
    return problems


async def _run(cfg: Config) -> int:
    bot = MemeBot(cfg)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    runner = asyncio.create_task(bot.run())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    await bot.shutdown()
    runner.cancel()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="memebot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--check", action="store_true",
                        help="validate configuration and exit")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    setup_logging(cfg.get("runtime.log_level", "INFO"))

    problems = preflight(cfg)
    if problems:
        print("Preflight findings:\n")
        for p in problems:
            print(f"  - {p}")
        print()
        fatal = [p for p in problems if "required" in p or "live" in p]
        if fatal:
            print("Fatal. Fix the above and try again.")
            return 1
        if args.check:
            return 0
        print("Continuing with degraded coverage.\n")
    elif args.check:
        print("Preflight OK.")
        return 0

    if cfg.get("runtime.shadow_mode", True):
        print("SHADOW MODE: alerts are marked and no orders will be placed.\n")

    return asyncio.run(_run(cfg))


if __name__ == "__main__":
    sys.exit(main())
