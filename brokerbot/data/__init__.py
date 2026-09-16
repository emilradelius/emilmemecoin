"""Bar data: CSV files, synthetic series, Yahoo, and a quality validator.

CSV is the primary path. A backtest built on a file you exported once stays
reproducible forever; one built on a live API silently changes underneath you
when the vendor revises history.
"""

from .base import BarSource
from .csv_source import CsvBarSource
from .synthetic import ASSET_PRESETS, random_walk
from .yahoo import YahooBarSource, YahooError

__all__ = [
    "BarSource", "CsvBarSource", "random_walk", "ASSET_PRESETS",
    "YahooBarSource", "YahooError",
]
