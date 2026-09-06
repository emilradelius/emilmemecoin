"""Config loading with dotted access and environment overlay."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

try:  # optional; the process env works fine without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a: Any, **_k: Any) -> bool:
        return False


class Config:
    """Thin wrapper over the parsed YAML.

    Access is dotted (``cfg.get("safety.min_liquidity_usd")``) so that call
    sites read like the config file and a typo raises immediately rather than
    silently defaulting to something dangerous.
    """

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path = "config.yaml", env_file: str | Path = ".env") -> "Config":
        load_dotenv(env_file)
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"config file not found: {p}. Copy config.yaml from the repo root."
            )
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data, p)

    _MISSING = object()

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is self._MISSING:
                    raise KeyError(f"missing config key: {dotted}")
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        node = self.get(dotted, {})
        return node if isinstance(node, dict) else {}

    def set(self, dotted: str, value: Any) -> None:
        """Runtime override (used by Telegram /set). Not persisted to disk
        unless :meth:`save` is called."""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> None:
        if self.path is None:
            raise RuntimeError("config has no backing file")
        with self.path.open("w") as fh:
            yaml.safe_dump(self._data, fh, sort_keys=False)

    # --- environment -----------------------------------------------------
    @staticmethod
    def env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
        val = os.getenv(name, default)
        if required and not val:
            raise RuntimeError(
                f"required environment variable {name} is not set - see .env.example"
            )
        return val

    def as_dict(self) -> dict[str, Any]:
        return self._data
