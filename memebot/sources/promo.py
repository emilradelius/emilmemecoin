"""Detecting paid promotion in social posts.

A paid call is not a call. These patterns are deliberately conservative -
they match disclosure language and the structural tells of a promoted post,
not merely enthusiastic writing, because penalising enthusiasm would knock
out most genuine meme coin traders.
"""

from __future__ import annotations

import re

PROMO_PATTERNS = [
    r"\b(?:paid|sponsored)\s+(?:promo|promotion|post|partnership|collab)",
    r"\bad\b\s*[:|-]",
    r"^\s*#ad\b",
    r"\b(?:this is (?:an|a)\s+)?(?:ad|advert|advertisement)\b",
    r"\bnot financial advice\b.*\bpaid\b",
    r"\bdisclosure\b.*\b(?:paid|compensated|sponsor)",
    r"\bi (?:was |am )?(?:paid|compensated)\b",
    r"\b(?:dm|dms?)\s+(?:me\s+)?for\s+(?:promo|promotion|ads?)\b",
    r"\bpromo\s+(?:rate|price|package|slot)s?\b",
]

# Structural tells of a low-effort shill post rather than a considered call.
SHILL_PATTERNS = [
    r"(?:🚀|🔥|💎|🌙){4,}",
    r"\b(?:100x|1000x)\s+(?:gem|guaranteed|incoming|confirmed)\b",
    r"\bguaranteed\s+(?:profit|gains|returns|moon)\b",
    r"\bnext\s+(?:pepe|doge|shib|bonk|wif)\b.*\b(?:100x|1000x)\b",
    r"\bpresale\s+(?:live|ending|now)\b",
    r"\bjoin\s+(?:my|our)\s+(?:telegram|tg|discord)\b.*\bcall(?:s)?\b",
]

_PROMO_RE = [re.compile(p, re.I | re.M) for p in PROMO_PATTERNS]
_SHILL_RE = [re.compile(p, re.I | re.M) for p in SHILL_PATTERNS]


def is_paid_promo(text: str) -> bool:
    return any(r.search(text) for r in _PROMO_RE)


def shill_score(text: str) -> float:
    """0.0 (clean) to 1.0 (obvious shill)."""
    hits = sum(bool(r.search(text)) for r in _SHILL_RE)
    return min(1.0, hits / 3.0)
