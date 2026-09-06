"""Capture real API payloads to verify the adapters against.

The adapters in this repo were written against documented response shapes,
but these APIs change and some of them differ between pools and plan tiers.
Run this on your own machine (which, unlike a sandboxed CI environment, can
reach the endpoints) to dump real payloads and confirm the parsers handle
them.

    python -m memebot.tools.probe_schemas --all
    python -m memebot.tools.probe_schemas --pumpportal --seconds 30
    python -m memebot.tools.probe_schemas --dexscreener <mint>

Anything that fails to parse is printed with the raw payload so the field
mapping can be corrected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

SAMPLE_MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


async def probe_pumpportal(seconds: int = 30) -> None:
    import websockets

    from ..config import Config
    from ..sources.pumpfun import PumpFunSource

    cfg = Config.load()
    src = PumpFunSource(cfg)
    url = cfg.get("sources.pumpfun.ws_url", "wss://pumpportal.fun/api/data")
    print(f"connecting to {url} for {seconds}s ...")

    seen = parsed = 0
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            try:
                async with asyncio.timeout(seconds):
                    async for raw in ws:
                        seen += 1
                        msg = json.loads(raw)
                        if seen <= 3:
                            print(f"\n--- message {seen} ---")
                            print(json.dumps(msg, indent=2)[:1500])
                        if await src.parse_trade(msg):
                            parsed += 1
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        print(f"pumpportal probe failed: {exc}")
        return
    print(f"\nreceived {seen} messages, {parsed} parsed as trades")
    if seen and not parsed:
        print(
            "NOTE: no trades parsed. That is expected for new-token/migration "
            "streams. Re-run with a trade subscription to exercise the trade "
            "parser, and compare field names against memebot/sources/pumpfun.py."
        )


async def probe_dexscreener(mint: str) -> None:
    from ..enrich.dexscreener import DexScreener

    dex = DexScreener()
    print(f"fetching DexScreener data for {mint} ...")
    raw = await dex.http.get_json(
        f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    )
    if not raw:
        print("no response")
    else:
        pairs = raw.get("pairs") or []
        print(f"{len(pairs)} pairs returned")
        if pairs:
            print(json.dumps(pairs[0], indent=2)[:1500])
        market = await dex.get(mint)
        print(f"\nparsed -> {market}")
    await dex.close()


async def probe_rugcheck(mint: str) -> None:
    from ..enrich.rugcheck import RugCheck

    rug = RugCheck(api_key=os.getenv("RUGCHECK_API_KEY"))
    print(f"fetching RugCheck report for {mint} ...")
    raw = await rug.http.get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if not raw:
        print("no response (rate limited, or the token is unknown to RugCheck)")
    else:
        print("top-level keys:", sorted(raw)[:25])
        report = rug._parse(mint, raw)
        print(f"\nparsed -> mint_revoked={report.mint_authority_revoked} "
              f"freeze_revoked={report.freeze_authority_revoked} "
              f"lp_locked={report.lp_locked_pct} holders={report.total_holders} "
              f"top10={report.top10_pct} danger={report.danger_flags}")
        unknown = [
            name for name, val in (
                ("mint_authority_revoked", report.mint_authority_revoked),
                ("freeze_authority_revoked", report.freeze_authority_revoked),
                ("lp_locked_pct", report.lp_locked_pct),
                ("total_holders", report.total_holders),
            ) if val is None
        ]
        if unknown:
            print(
                f"\nWARNING: these fields did not parse: {', '.join(unknown)}.\n"
                "The safety gate fails closed on unknowns, so it will reject "
                "everything until the mapping in memebot/enrich/rugcheck.py is "
                "corrected against the payload above."
            )
    await rug.close()


async def probe_x() -> None:
    from ..config import Config
    from ..enrich.dexscreener import DexScreener
    from ..enrich.resolver import TokenResolver
    from ..sources.xfeed import XSource
    from ..store import Store

    cfg = Config.load()
    key = os.getenv("X_API_KEY")
    if not key:
        print("X_API_KEY is not set - skipping")
        return

    store = Store(":memory:")
    dex = DexScreener()
    src = XSource(cfg, store, TokenResolver(dex), api_key=key,
                  api_base=os.getenv("X_API_BASE"))
    src.set_accounts({"solana": 0.5})
    query = src.build_query(list(src.accounts.values()))
    print(f"query: {query}")
    tweets = await src._search(query)
    print(f"{len(tweets)} tweets returned")
    if tweets:
        print(json.dumps(tweets[0], indent=2)[:1200])
        print("\nextracted fields ->", src._tweet_fields(tweets[0]))
    else:
        print(
            "No tweets parsed. Check that your provider supports the "
            "advanced_search endpoint and the 'from:' operator, and compare "
            "the envelope key against XSource._search."
        )
    await src.close()
    await dex.close()


async def _main(args) -> int:
    if args.all or args.dexscreener is not None:
        await probe_dexscreener(args.dexscreener or SAMPLE_MINT)
        print("\n" + "=" * 70 + "\n")
    if args.all or args.rugcheck is not None:
        await probe_rugcheck(args.rugcheck or SAMPLE_MINT)
        print("\n" + "=" * 70 + "\n")
    if args.all or args.pumpportal:
        await probe_pumpportal(args.seconds)
        print("\n" + "=" * 70 + "\n")
    if args.all or args.x:
        await probe_x()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all", action="store_true")
    p.add_argument("--pumpportal", action="store_true")
    p.add_argument("--dexscreener", nargs="?", const=SAMPLE_MINT, default=None)
    p.add_argument("--rugcheck", nargs="?", const=SAMPLE_MINT, default=None)
    p.add_argument("--x", action="store_true")
    p.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()
    if not any([args.all, args.pumpportal, args.x,
                args.dexscreener is not None, args.rugcheck is not None]):
        p.print_help()
        return 1
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
