"""Load bars from a CSV file.

Column names vary by vendor, so the reader matches case-insensitively and
accepts the common spellings. One rule is not negotiable: when an adjusted
close is present it wins. Unadjusted prices turn every split into a crash the
strategy looks clever for dodging.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from ..models import Bar
from .base import BarSource

log = logging.getLogger(__name__)

_DATE_KEYS = ("date", "datetime", "timestamp", "time")
_ADJ_KEYS = ("adj close", "adj_close", "adjclose", "adjusted close", "adjusted_close")
_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
)


def _parse_ts(raw: str) -> datetime:
    text = raw.strip().replace("Z", "")
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # Last resort: ISO-8601 with an offset or fractional seconds.
    return datetime.fromisoformat(text)


class CsvBarSource(BarSource):
    """Read OHLCV bars from a CSV file on disk."""

    name = "csv"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, symbol: str, **kwargs) -> list[Bar]:
        if not self.path.exists():
            raise FileNotFoundError(f"no such CSV: {self.path}")

        bars: list[Bar] = []
        skipped = 0

        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                lower = {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in row.items()
                }
                try:
                    bars.append(self._to_bar(symbol, lower))
                except (ValueError, TypeError, KeyError):
                    # A malformed row is a bad print, not a reason to lose the
                    # other 2,499 rows. Count them so the damage is visible.
                    skipped += 1

        if skipped:
            log.warning("%s: skipped %d malformed row(s)", self.path, skipped)

        bars.sort(key=lambda b: b.ts)
        return bars

    @staticmethod
    def _to_bar(symbol: str, row: dict[str, str]) -> Bar:
        date_key = next((k for k in _DATE_KEYS if k in row), None)
        if date_key is None:
            raise KeyError("no date column")
        ts = _parse_ts(row[date_key])

        close = float(row["close"])

        # Adjusted close wins when present, and the OHLC is rescaled by the
        # same factor so the bar stays internally consistent rather than
        # mixing an adjusted close with unadjusted highs and lows.
        adj_key = next((k for k in _ADJ_KEYS if row.get(k)), None)
        factor = 1.0
        if adj_key is not None:
            adjusted = float(row[adj_key])
            if adjusted > 0 and close > 0:
                factor = adjusted / close
                close = adjusted

        open_ = float(row.get("open") or close) * factor
        high = float(row.get("high") or close) * factor
        low = float(row.get("low") or close) * factor
        volume = float(row.get("volume") or 0.0)

        return Bar(symbol, ts, open_, high, low, close, volume)
