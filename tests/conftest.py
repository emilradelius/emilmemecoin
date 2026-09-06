from __future__ import annotations

import time

import pytest

from memebot.config import Config
from memebot.models import Side, Signal, Source
from memebot.store import Store


@pytest.fixture
def cfg() -> Config:
    return Config.load("config.yaml")


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "test.sqlite")
    yield s
    s.close()


@pytest.fixture
def now() -> float:
    return time.time()


def make_signal(
    source: Source = Source.PUMPFUN,
    actor: str = "W1",
    mint: str = "MINT",
    side: Side = Side.BUY,
    size_usd: float | None = 5000,
    age_seconds: float = 60,
    actor_score: float = 0.85,
    confidence: float = 1.0,
    now: float | None = None,
) -> Signal:
    return Signal(
        source=source,
        actor_id=actor,
        token_mint=mint,
        side=side,
        size_usd=size_usd,
        ts=(now or time.time()) - age_seconds,
        actor_score=actor_score,
        confidence=confidence,
    )
