"""SQLite persistence.

Single-user bot, so SQLite in WAL mode is the right call: no server to run,
and the write volume (a few thousand signals a day) is nowhere near its
limits. Everything is stored rather than just the alerts, because trader
re-scoring and threshold tuning both need the history of what was *rejected*,
not only what was sent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import (
    ExitReason, Position, Side, Signal, Source, TraderScore,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_symbol TEXT,
    side TEXT NOT NULL,
    size_usd REAL,
    price_usd REAL,
    actor_score REAL,
    confidence REAL,
    url TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_mint_ts ON signals(token_mint, ts);
CREATE INDEX IF NOT EXISTS idx_signals_actor_ts ON signals(actor_id, ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS trader_scores (
    actor_id TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    score_7d REAL,
    tracked INTEGER,
    cluster_id TEXT,
    excluded_reason TEXT,
    metrics TEXT,
    updated_at REAL,
    PRIMARY KEY (actor_id, source)
);
CREATE INDEX IF NOT EXISTS idx_scores_tracked ON trader_scores(source, tracked);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    token_mint TEXT NOT NULL,
    token_symbol TEXT,
    mode TEXT,
    entry_price_usd REAL,
    size_sol REAL,
    tokens_held REAL,
    opened_at REAL,
    closed_at REAL,
    close_reason TEXT,
    peak_price_usd REAL,
    realized_pnl_sol REAL,
    remaining_fraction REAL,
    ladder_rungs_hit TEXT,
    trigger_actors TEXT,
    entry_tx TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    tier TEXT,
    conviction REAL,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_mint_ts ON alerts_sent(token_mint, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts_sent(ts);

-- Every token we evaluated and rejected, with the reason. This table is what
-- makes the daily report useful for tuning.
CREATE TABLE IF NOT EXISTS rejections (
    ts REAL NOT NULL,
    token_mint TEXT NOT NULL,
    stage TEXT NOT NULL,
    reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_rejections_ts ON rejections(ts);

-- Graded X calls: price at call time vs what happened after.
CREATE TABLE IF NOT EXISTS x_calls (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    handle TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    price_at_call REAL,
    max_price_24h REAL,
    min_price_24h REAL,
    price_24h REAL,
    multiple_at_call REAL,
    graded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_xcalls_handle ON x_calls(handle, ts);
CREATE INDEX IF NOT EXISTS idx_xcalls_ungraded ON x_calls(graded, ts);

-- Wallet co-occurrence, for Sybil collapse.
CREATE TABLE IF NOT EXISTS wallet_links (
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    kind TEXT NOT NULL,
    weight REAL,
    updated_at REAL,
    PRIMARY KEY (a, b, kind)
);

-- Generic key/value for budget counters, runtime mode, circuit breaker state.
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    updated_at REAL
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- signals ---------------------------------------------------------
    def add_signal(self, sig: Signal) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO signals
                   (id, ts, source, actor_id, token_mint, token_symbol, side,
                    size_usd, price_usd, actor_score, confidence, url, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.id, sig.ts, sig.source.value, sig.actor_id, sig.token_mint,
                 sig.token_symbol, sig.side.value, sig.size_usd, sig.price_usd,
                 sig.actor_score, sig.confidence, sig.url, json.dumps(sig.raw)[:20000]),
            )
            self._conn.commit()

    def signals_for_mint(self, mint: str, since: float) -> list[Signal]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE token_mint=? AND ts>=? ORDER BY ts",
                (mint, since),
            ).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def signals_since(self, since: float) -> list[Signal]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE ts>=? ORDER BY ts", (since,)
            ).fetchall()
        return [self._row_to_signal(r) for r in rows]

    @staticmethod
    def _row_to_signal(r: sqlite3.Row) -> Signal:
        return Signal(
            id=r["id"], ts=r["ts"], source=Source(r["source"]), actor_id=r["actor_id"],
            token_mint=r["token_mint"], token_symbol=r["token_symbol"],
            side=Side(r["side"]), size_usd=r["size_usd"], price_usd=r["price_usd"],
            actor_score=r["actor_score"] or 0.5, confidence=r["confidence"] or 1.0,
            url=r["url"], raw=json.loads(r["raw"] or "{}"),
        )

    # --- trader scores ---------------------------------------------------
    def upsert_score(self, ts: TraderScore) -> None:
        metrics = {
            k: getattr(ts, k) for k in (
                "realized_pnl_usd", "win_rate", "median_multiple", "closed_trades",
                "unique_tokens", "avg_hold_seconds", "rug_rate", "graded_calls",
                "hit_rate", "avg_max_multiple", "median_multiple_at_call",
                "calls_per_day",
            )
        }
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO trader_scores
                   (actor_id, source, score, score_7d, tracked, cluster_id,
                    excluded_reason, metrics, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts.actor_id, ts.source.value, ts.score, ts.score_7d,
                 int(ts.tracked), ts.cluster_id, ts.excluded_reason,
                 json.dumps(metrics), time.time()),
            )
            self._conn.commit()

    def get_score(self, actor_id: str, source: Source) -> TraderScore | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM trader_scores WHERE actor_id=? AND source=?",
                (actor_id, source.value),
            ).fetchone()
        return self._row_to_score(r) if r else None

    def tracked_actors(self, source: Source) -> list[TraderScore]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trader_scores WHERE source=? AND tracked=1 "
                "ORDER BY score DESC",
                (source.value,),
            ).fetchall()
        return [self._row_to_score(r) for r in rows]

    @staticmethod
    def _row_to_score(r: sqlite3.Row) -> TraderScore:
        m = json.loads(r["metrics"] or "{}")
        ts = TraderScore(
            actor_id=r["actor_id"], source=Source(r["source"]), score=r["score"] or 0.0,
            score_7d=r["score_7d"] or 0.0, tracked=bool(r["tracked"]),
            cluster_id=r["cluster_id"], excluded_reason=r["excluded_reason"],
            updated_at=r["updated_at"] or 0.0,
        )
        for k, v in m.items():
            if hasattr(ts, k) and v is not None:
                setattr(ts, k, v)
        return ts

    # --- positions -------------------------------------------------------
    def save_position(self, p: Position) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO positions
                   (id, token_mint, token_symbol, mode, entry_price_usd, size_sol,
                    tokens_held, opened_at, closed_at, close_reason, peak_price_usd,
                    realized_pnl_sol, remaining_fraction, ladder_rungs_hit,
                    trigger_actors, entry_tx)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.id, p.token_mint, p.token_symbol, p.mode, p.entry_price_usd,
                 p.size_sol, p.tokens_held, p.opened_at, p.closed_at,
                 p.close_reason.value if p.close_reason else None,
                 p.peak_price_usd, p.realized_pnl_sol, p.remaining_fraction,
                 json.dumps(p.ladder_rungs_hit), json.dumps(p.trigger_actors),
                 p.entry_tx),
            )
            self._conn.commit()

    def open_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL"
            ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def closed_positions(self, since: float = 0.0,
                         mode: str | None = None) -> list[Position]:
        """Closed positions, oldest first. Used by the readiness report to
        judge whether paper results justify trading live."""
        sql = "SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at>=?"
        params: list[Any] = [since]
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        sql += " ORDER BY closed_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_position(r) for r in rows]

    def position_for_mint(self, mint: str) -> Position | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM positions WHERE token_mint=? AND closed_at IS NULL",
                (mint,),
            ).fetchone()
        return self._row_to_position(r) if r else None

    @staticmethod
    def _row_to_position(r: sqlite3.Row) -> Position:
        return Position(
            id=r["id"], token_mint=r["token_mint"], token_symbol=r["token_symbol"],
            mode=r["mode"] or "paper", entry_price_usd=r["entry_price_usd"] or 0.0,
            size_sol=r["size_sol"] or 0.0, tokens_held=r["tokens_held"] or 0.0,
            opened_at=r["opened_at"] or 0.0, closed_at=r["closed_at"],
            close_reason=ExitReason(r["close_reason"]) if r["close_reason"] else None,
            peak_price_usd=r["peak_price_usd"] or 0.0,
            realized_pnl_sol=r["realized_pnl_sol"] or 0.0,
            remaining_fraction=r["remaining_fraction"] if r["remaining_fraction"] is not None else 1.0,
            ladder_rungs_hit=json.loads(r["ladder_rungs_hit"] or "[]"),
            trigger_actors=json.loads(r["trigger_actors"] or "[]"),
            entry_tx=r["entry_tx"],
        )

    # --- alerts / dedupe -------------------------------------------------
    def record_alert(self, alert_id: str, kind: str, mint: str, tier: str,
                     conviction: float, text: str, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts_sent VALUES (?,?,?,?,?,?,?)",
                (alert_id, ts if ts is not None else time.time(), kind, mint,
                 tier, conviction, text[:4000]),
            )
            self._conn.commit()

    def last_alert_for_mint(self, mint: str, kind: str = "buy") -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM alerts_sent WHERE token_mint=? AND kind=? "
                "ORDER BY ts DESC LIMIT 1",
                (mint, kind),
            ).fetchone()

    def alerts_since(self, since: float, kind: str = "buy") -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) c FROM alerts_sent WHERE ts>=? AND kind=?",
                (since, kind),
            ).fetchone()
        return int(r["c"])

    # --- rejections / reporting -----------------------------------------
    def record_rejection(self, mint: str, stage: str, reasons: Iterable[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejections VALUES (?,?,?,?)",
                (time.time(), mint, stage, json.dumps(list(reasons))),
            )
            self._conn.commit()

    def rejection_summary(self, since: float) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, COUNT(*) c FROM rejections WHERE ts>=? "
                "GROUP BY stage ORDER BY c DESC",
                (since,),
            ).fetchall()
        return [(r["stage"], int(r["c"])) for r in rows]

    # --- x call grading --------------------------------------------------
    def record_x_call(self, call_id: str, handle: str, mint: str,
                      price_at_call: float | None, multiple_at_call: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO x_calls (id, ts, handle, token_mint, "
                "price_at_call, multiple_at_call, graded) VALUES (?,?,?,?,?,?,0)",
                (call_id, time.time(), handle, mint, price_at_call, multiple_at_call),
            )
            self._conn.commit()

    def ungraded_calls(self, older_than_ts: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM x_calls WHERE graded=0 AND ts<=?", (older_than_ts,)
            ).fetchall()

    def grade_x_call(self, call_id: str, max_p: float, min_p: float, p24: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE x_calls SET max_price_24h=?, min_price_24h=?, price_24h=?, "
                "graded=1 WHERE id=?",
                (max_p, min_p, p24, call_id),
            )
            self._conn.commit()

    def graded_calls_for(self, handle: str, since: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM x_calls WHERE handle=? AND graded=1 AND ts>=?",
                (handle, since),
            ).fetchall()

    def calls_for(self, handle: str, since: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM x_calls WHERE handle=? AND ts>=?", (handle, since)
            ).fetchall()

    # --- wallet links ----------------------------------------------------
    def add_wallet_link(self, a: str, b: str, kind: str, weight: float) -> None:
        lo, hi = sorted((a, b))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO wallet_links VALUES (?,?,?,?,?)",
                (lo, hi, kind, weight, time.time()),
            )
            self._conn.commit()

    def wallet_links(self) -> list[tuple[str, str, str, float]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM wallet_links").fetchall()
        return [(r["a"], r["b"], r["kind"], r["weight"]) for r in rows]

    # --- kv --------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                (key, json.dumps(value), time.time()),
            )
            self._conn.commit()

    # --- maintenance -----------------------------------------------------
    def prune(self, retain_days: int) -> None:
        cutoff = time.time() - retain_days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM signals WHERE ts<?", (cutoff,))
            self._conn.execute("DELETE FROM rejections WHERE ts<?", (cutoff,))
            self._conn.commit()
            self._conn.execute("VACUUM")
