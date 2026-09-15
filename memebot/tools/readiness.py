"""Is the bot ready to trade real money?

    python -m memebot.tools.readiness
    python -m memebot.tools.readiness --mode live    # review live results too
"""

from __future__ import annotations

import argparse
import sys

from ..config import Config
from ..readiness import ReadinessAssessor
from ..store import Store


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--mode", default="paper", choices=["paper", "live"])
    args = p.parse_args()

    cfg = Config.load(args.config)
    store = Store(cfg.get("storage.db_path", "data/memebot.sqlite"))
    assessor = ReadinessAssessor(cfg, store)
    report = assessor.assess(mode=args.mode)
    print(assessor.render(report))
    store.close()
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(main())
