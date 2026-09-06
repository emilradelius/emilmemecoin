"""Turning a Candidate into a message a human can act on in ten seconds.

Design rule: every alert must show its own reasoning and its own risks. A bare
"BUY $DOG" trains you to click without thinking, which is how a bug or a
manipulated signal turns into a loss. The card shows who is buying, why it
cleared, and what would invalidate it.
"""

from __future__ import annotations

from ..models import Alert, Candidate, ExitReason, Position, Source, Tier

TRADE_LINKS = {
    "axiom": "https://axiom.trade/t/{mint}",
    "photon": "https://photon-sol.tinyastro.io/en/lp/{mint}",
    "gmgn": "https://gmgn.ai/sol/token/{mint}",
    "bullx": "https://bullx.io/terminal?chainId=1399811149&address={mint}",
    "dexscreener": "https://dexscreener.com/solana/{mint}",
}

SOURCE_LABEL = {
    Source.PUMPFUN: "Pump.fun",
    Source.X: "X",
    Source.ALPHALEDGER: "AlphaLedger",
}


def _esc(text: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _usd(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.8f}".rstrip("0")


def _age(minutes: float | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def trade_link(mint: str, provider: str = "axiom") -> str:
    return TRADE_LINKS.get(provider, TRADE_LINKS["dexscreener"]).format(mint=mint)


def format_buy(cand: Candidate, *, provider: str = "axiom",
               shadow: bool = False, mode: str = "alerts") -> str:
    sym = _esc(cand.token_symbol or cand.token_mint[:8])
    header = "🟢 <b>STRONG BUY</b>" if cand.tier is Tier.STRONG else "👀 <b>WATCH</b>"
    if shadow:
        header = "🌑 <b>[SHADOW]</b> " + header

    lines = [
        f"{header}  <b>${sym}</b>",
        f"<code>{_esc(cand.token_mint)}</code>",
        "",
        f"<b>Conviction</b> {cand.conviction:.1f}  ·  "
        f"<b>{cand.independent_actors}</b> independent traders  ·  "
        f"<b>{len(cand.distinct_sources)}</b> sources",
    ]

    by_source: dict[Source, list[str]] = {}
    for sig in cand.buy_signals:
        by_source.setdefault(sig.source, []).append(sig.actor_id)
    for src, actors in by_source.items():
        shown = ", ".join(
            _esc(a if src is Source.X else f"{a[:4]}..{a[-4:]}") for a in sorted(set(actors))[:4]
        )
        extra = len(set(actors)) - 4
        if extra > 0:
            shown += f" +{extra}"
        lines.append(f"  · <b>{SOURCE_LABEL.get(src, src.value)}</b>: {shown}")

    s = cand.safety
    if s:
        lines += [
            "",
            f"<b>Liquidity</b> {_usd(s.liquidity_usd)}  ·  "
            f"<b>Age</b> {_age(s.age_minutes)}  ·  "
            f"<b>Holders</b> {s.holders or '?'}",
            f"<b>Price</b> {_usd(s.price_usd)}  ·  "
            f"<b>1h vol</b> {_usd(s.volume_1h_usd)}  ·  "
            f"<b>Top10</b> {f'{s.top10_pct:.0f}%' if s.top10_pct is not None else '?'}",
        ]
        if s.warnings:
            lines.append(f"⚠️ {_esc(', '.join(s.warnings[:3]))}")

    if cand.rationale:
        lines += ["", "<b>Why</b>"] + [f"  · {_esc(r)}" for r in cand.rationale[:7]]

    lines += [
        "",
        f'<a href="{trade_link(cand.token_mint, provider)}">Trade</a>  ·  '
        f'<a href="https://dexscreener.com/solana/{cand.token_mint}">Chart</a>  ·  '
        f'<a href="https://rugcheck.xyz/tokens/{cand.token_mint}">RugCheck</a>',
    ]

    if mode == "live":
        lines.append("\n⚡️ <i>Auto-trade is LIVE - an order may already be placed.</i>")
    elif mode == "paper":
        lines.append("\n📝 <i>Paper mode - simulated only.</i>")

    lines.append(
        "\n<i>Not financial advice. Meme coins routinely go to zero. "
        "Never size a position you cannot afford to lose entirely.</i>"
    )
    return "\n".join(lines)


EXIT_HEADLINE = {
    ExitReason.SMART_MONEY_EXIT: ("🔴", "SELL - smart money is leaving"),
    ExitReason.STOP_LOSS: ("🔴", "SELL - stop loss hit"),
    ExitReason.TAKE_PROFIT: ("🟢", "TAKE PROFIT"),
    ExitReason.TRAILING_STOP: ("🟡", "SELL - trailing stop hit"),
    ExitReason.LIQUIDITY_COLLAPSE: ("🚨", "SELL NOW - liquidity collapsing"),
    ExitReason.SAFETY_REGRESSION: ("🚨", "SELL NOW - token safety regressed"),
    ExitReason.TIME_STOP: ("⏰", "SELL - time stop, position went nowhere"),
    ExitReason.MANUAL: ("ℹ️", "Position closed manually"),
}


def format_exit(pos: Position, reason: ExitReason, price_usd: float,
                *, sell_fraction: float = 1.0, detail: str = "",
                provider: str = "axiom") -> str:
    icon, headline = EXIT_HEADLINE.get(reason, ("ℹ️", "Exit"))
    sym = _esc(pos.token_symbol or pos.token_mint[:8])
    mult = pos.multiple(price_usd)
    pnl_pct = (mult - 1.0) * 100

    lines = [
        f"{icon} <b>{headline}</b>  <b>${sym}</b>",
        f"<code>{_esc(pos.token_mint)}</code>",
        "",
        f"<b>Entry</b> {_usd(pos.entry_price_usd)}  →  <b>Now</b> {_usd(price_usd)}",
        f"<b>{mult:.2f}x</b>  ({pnl_pct:+.0f}%)  ·  held {pos.age_hours():.1f}h",
    ]
    if sell_fraction < 1.0:
        lines.append(f"<b>Sell {sell_fraction:.0%}</b> of the position, let the rest run.")
    else:
        lines.append("<b>Exit the full position.</b>")
    if detail:
        lines += ["", _esc(detail)]
    lines += [
        "",
        f'<a href="{trade_link(pos.token_mint, provider)}">Sell</a>  ·  '
        f'<a href="https://dexscreener.com/solana/{pos.token_mint}">Chart</a>',
    ]
    return "\n".join(lines)


def format_daily_report(
    *, alerts_sent: int, candidates_seen: int,
    rejections: list[tuple[str, int]], open_positions: list[Position],
    budget_status: str, mode: str, shadow: bool,
) -> str:
    lines = [
        "📊 <b>Daily report</b>",
        "",
        f"<b>Alerts sent</b> {alerts_sent}  ·  <b>Candidates evaluated</b> {candidates_seen}",
        f"<b>Mode</b> {mode}{' (shadow)' if shadow else ''}",
        f"<b>X budget</b> {_esc(budget_status)}",
    ]
    if rejections:
        lines += ["", "<b>Filtered out at</b>"]
        lines += [f"  · {_esc(stage)}: {count}" for stage, count in rejections[:8]]
    if open_positions:
        lines += ["", f"<b>Open positions</b> ({len(open_positions)})"]
        for p in open_positions[:10]:
            lines.append(
                f"  · ${_esc(p.token_symbol or p.token_mint[:6])} "
                f"· {p.size_sol:.2f} SOL · {p.age_hours():.1f}h"
            )
    else:
        lines += ["", "<b>No open positions.</b>"]
    lines.append(
        "\n<i>If this is filtering out too much, the dials are "
        "alerts.daily_budget and consensus.tiers.strong.min_conviction.</i>"
    )
    return "\n".join(lines)
