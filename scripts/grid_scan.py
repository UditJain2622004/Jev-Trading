"""Download 1-minute bars and scan classic geometric long-grid lever sets.

Five coins. Six named grids (step × levels, one with a hard stop).

    python -u scripts/grid_scan.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_trading.binance.grid import (
    DEFAULT_COMBOS,
    DEFAULT_SYMBOLS,
    print_summary,
    scan_symbol,
    write_csv,
)
from jev_trading.binance.market import MarketClient
from jev_trading.binance.setup_scan import load_cached_klines
from jev_trading.binance.tp_sl_scan import load_klines, utc_ms_days_ago

log = logging.getLogger("grid")


def nearest_cache(cache_dir: Path, symbol: str, days: int) -> Path | None:
    want_ms = days * 24 * 60 * 60 * 1000
    best: Path | None = None
    best_diff: int | None = None
    for path in cache_dir.glob(f"{symbol}_1m_*.json"):
        start_ms = int(path.stem.split("_")[-2])
        end_ms = int(path.stem.split("_")[-1])
        diff = abs((end_ms - start_ms) - want_ms)
        if best is None or diff < best_diff:
            best = path
            best_diff = diff
    if best is None or best_diff is None or best_diff > 6 * 60 * 60 * 1000:
        return None
    return best


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Geometric long-grid lever scan on 1m data")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--out", default="data/grid/results.csv")
    parser.add_argument("--log-file", default="data/grid/scan.log")
    parser.add_argument("--cache-dir", default="data/klines")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log = log_file.with_name(f"grid_{stamp}.log")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    handlers = (
        stream,
        _FlushFileHandler(run_log, mode="w", encoding="utf-8"),
        _FlushFileHandler(log_file, mode="w", encoding="utf-8"),
    )
    for handler in handlers:
        handler.setFormatter(formatter)
    for name in ("grid", "tp_sl_scan"):
        root = logging.getLogger(name)
        root.setLevel(logging.INFO)
        root.handlers.clear()
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = False
    log.info("Log (this run) %s", run_log)
    log.info("Log (latest)   %s", log_file)


def main() -> None:
    args = parse_args()
    setup_logging(ROOT / args.log_file)
    symbols = [s.upper() for s in args.symbols]
    start_ms, end_ms = utc_ms_days_ago(args.days)
    log.info(
        "Geometric long grid. Paper only. $%.0f notional, 0.20%% round-trip fee.",
        args.notional,
    )
    log.info("Window %s days  %s to %s", args.days, start_ms, end_ms)
    log.info("Coins %s: %s", len(symbols), ", ".join(symbols))
    log.info("Combos %s", len(DEFAULT_COMBOS))
    for levers in DEFAULT_COMBOS:
        stop = f", stop {levers.stop_pct:g}%" if levers.stop_pct else ""
        log.info(
            "  %-26s step %.2f%% × %d levels%s",
            levers.name,
            levers.step_pct,
            levers.levels,
            stop,
        )

    market = MarketClient()
    cache_dir = ROOT / args.cache_dir
    rows = []
    started = time.perf_counter()
    for n, symbol in enumerate(symbols, start=1):
        log.info("----------  [%s/%s] %s  ----------", n, len(symbols), symbol)
        cached = nearest_cache(cache_dir, symbol, args.days)
        if cached is not None:
            log.info("%s: reusing %s", symbol, cached.name)
            klines = load_cached_klines(cached, symbol)
        else:
            klines = load_klines(market, symbol, "1m", start_ms, end_ms, cache_dir)
        log.info("%s: %s 1-minute bars", symbol, f"{len(klines):,}")
        rows.extend(scan_symbol(symbol, klines, DEFAULT_COMBOS, notional=args.notional))

    out = ROOT / args.out
    write_csv(out, rows)
    print_summary(rows)
    log.info("Wrote %s", out)
    log.info("Finished in %.1fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
