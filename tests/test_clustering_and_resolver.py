"""Sybil collapse and token resolution - the two places where a naive
implementation is directly exploitable."""

from __future__ import annotations

import time

import pytest

from memebot.enrich.resolver import (
    extract_mints, extract_tickers, looks_like_mint,
)
from memebot.models import Side, Source
from memebot.scoring.clustering import ClusterBuilder, ClusterMap, UnionFind

from .conftest import make_signal

REAL_MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
OTHER_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


def test_union_find_is_deterministic():
    a, b = UnionFind(), UnionFind()
    a.union("x", "y"); a.union("y", "z")
    b.union("z", "y"); b.union("y", "x")
    assert a.find("x") == b.find("x")


def test_cluster_map_collapses_actors():
    cm = ClusterMap({"W1": "W0", "W2": "W0", "W3": "W3"})
    assert cm.collapse(["W0", "W1", "W2", "W3"]) == {"W0", "W3"}


def test_direct_funding_collapses_wallets(cfg, store):
    store.add_wallet_link("A", "B", "direct_funding", 1.0)
    cmap = ClusterBuilder(cfg, store).build([])
    assert cmap.cluster_of("A") == cmap.cluster_of("B")


def test_co_occurring_wallets_are_collapsed(cfg, store):
    """Two wallets that keep buying the same tokens within seconds of each
    other are one actor. Independent traders do not do this repeatedly."""
    now = time.time()
    signals = []
    for i in range(6):
        mint = f"MINT{i}"
        signals.append(make_signal(actor="A", mint=mint, age_seconds=100 * i, now=now))
        signals.append(make_signal(actor="B", mint=mint, age_seconds=100 * i - 5, now=now))
    cmap = ClusterBuilder(cfg, store).build(signals)
    assert cmap.cluster_of("A") == cmap.cluster_of("B")


def test_wallets_sharing_one_popular_token_are_not_collapsed(cfg, store):
    """One token that many wallets touch must not link them all - that would
    collapse the entire market into a single actor."""
    now = time.time()
    signals = [
        make_signal(actor=f"W{i}", mint="POPULAR", age_seconds=i * 3, now=now)
        for i in range(15)
    ]
    cmap = ClusterBuilder(cfg, store).build(signals)
    assert len({cmap.cluster_of(f"W{i}") for i in range(15)}) == 15


def test_disabled_clustering_is_a_noop(cfg, store):
    cfg.set("traders.clustering.enabled", False)
    store.add_wallet_link("A", "B", "direct_funding", 1.0)
    cmap = ClusterBuilder(cfg, store).build([])
    assert cmap.cluster_of("A") != cmap.cluster_of("B")


# --- resolver -------------------------------------------------------------
def test_explicit_mint_is_extracted():
    assert extract_mints(f"aped this {REAL_MINT} lfg") == [REAL_MINT]


def test_url_mint_is_extracted():
    text = f"https://pump.fun/coin/{REAL_MINT} looks good"
    assert REAL_MINT in extract_mints(text)


def test_pump_suffix_is_preferred_when_ambiguous():
    """When several base58 blobs appear, the pump.fun-style mint is the one
    most likely being referenced."""
    assert extract_mints(f"{OTHER_MINT} and {REAL_MINT}")[0] == REAL_MINT


def test_known_non_tokens_are_filtered():
    assert extract_mints("So11111111111111111111111111111111111111112") == []
    assert extract_mints("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == []


def test_ticker_stopwords_are_filtered():
    assert extract_tickers("moving $USD into $SOL and $BONK") == ["BONK"]


def test_tickers_are_normalised():
    assert extract_tickers("$wif and $WIF") == ["WIF"]


@pytest.mark.parametrize("text,expected", [(REAL_MINT, True), ("hello", False),
                                           ("0OIl" * 10, False)])
def test_looks_like_mint(text, expected):
    assert looks_like_mint(text) is expected
