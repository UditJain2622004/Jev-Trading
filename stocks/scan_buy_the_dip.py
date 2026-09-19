"""First buy-the-dip test on cached 5-minute US stocks.

Downloads missing caches, then runs 10 lever sets. 0.3% in, 0.3% out.

    python -u stocks/scan_buy_the_dip.py
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
from stocks.dip import DEFAULT_COMBOS, print_summary, scan_symbol, write_csv

log = logging.getLogger("stocks")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy-the-dip scan on cached 5m US stocks")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--period", default="60d")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--out", default="data/stocks/buy_the_dip/results.csv")
    parser.add_argument("--force-download", action="store_true")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging() -> None:
    log_dir = ROOT / "data" / "stocks" / "buy_the_dip"
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
        _FlushFileHandler(log_dir / f"scan_{stamp}.log", mode="w", encoding="utf-8"),
        _FlushFileHandler(log_dir / "scan.log", mode="w", encoding="utf-8"),
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
    log.info("US stocks buy-the-dip. $%.0f clip. 0.30%% buy + 0.30%% sell.", args.notional)
    log.info("Bars %s %s  cache %s", args.period, args.interval, cache_dir)
    log.info("Symbols %s: %s", len(symbols), ", ".join(symbols))
    log.info("Combos %s", len(DEFAULT_COMBOS))
    for levers in DEFAULT_COMBOS:
        log.info(
            "  %-10s dip %.1f%% in %.1f sessions, end within %.1f%% after %sm, TP %.1f SL %.1f, timeout %.1f sess",
            levers.name,
            levers.dip_pct,
            levers.dip_minutes / 390,
            levers.stable_range_pct,
            levers.stable_minutes,
            levers.take_profit_pct,
            levers.stop_loss_pct,
            levers.timeout_minutes / 390,
        )

    rows = []
    started = time.perf_counter()
    for n, symbol in enumerate(symbols, start=1):
        log.info("----------  [%s/%s] %s  ----------", n, len(symbols), symbol)
        path, bars = download_symbol(
            symbol,
            period=args.period,
            interval=args.interval,
            cache_dir=cache_dir,
            force=args.force_download,
        )
        log.info("%s: %s 5-minute bars from %s", symbol, f"{len(bars):,}", path.name)
        rows.extend(scan_symbol(symbol, bars, DEFAULT_COMBOS, notional=args.notional))

    out = ROOT / args.out
    write_csv(out, rows)
    print_summary(rows)
    log.info("Wrote %s", out)
    log.info("Finished in %.1fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
