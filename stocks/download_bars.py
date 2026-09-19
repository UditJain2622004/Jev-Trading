"""Download 5-minute US stock bars and cache them.

    python -u stocks/download_bars.py
    python -u stocks/download_bars.py --force
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stocks.bars import DEFAULT_CACHE_DIR, DEFAULT_SYMBOLS, download_symbol

log = logging.getLogger("stocks")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and cache 5-minute US stock bars")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--period", default="60d")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--force", action="store_true", help="Ignore cache and download again")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging() -> None:
    log_dir = ROOT / "data" / "stocks"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root = logging.getLogger("stocks")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (
        stream,
        _FlushFileHandler(log_dir / f"download_{stamp}.log", mode="w", encoding="utf-8"),
        _FlushFileHandler(log_dir / "download.log", mode="w", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.propagate = False


def main() -> None:
    args = parse_args()
    setup_logging()
    symbols = [s.upper() for s in args.symbols]
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    log.info("US stocks  interval=%s  period=%s  force=%s", args.interval, args.period, args.force)
    log.info("Cache dir %s", cache_dir)
    log.info("Symbols %s: %s", len(symbols), ", ".join(symbols))
    failed: list[str] = []
    for n, symbol in enumerate(symbols, start=1):
        log.info("----------  [%s/%s] %s  ----------", n, len(symbols), symbol)
        try:
            path, bars = download_symbol(
                symbol,
                period=args.period,
                interval=args.interval,
                cache_dir=cache_dir,
                force=args.force,
            )
            log.info("%s: %s bars  %s -> %s", symbol, f"{len(bars):,}", bars[0].open_time, bars[-1].open_time)
            log.info("%s: file %s", symbol, path)
        except Exception as exc:
            log.exception("%s: download failed: %s", symbol, exc)
            failed.append(symbol)
        time.sleep(0.4)
    if failed:
        log.info("Failed: %s", ", ".join(failed))
        raise SystemExit(1)
    log.info("Done. Later scans reuse these files unless you pass --force.")


if __name__ == "__main__":
    main()
