"""Scan take-profit / stop-loss pairs across coins.

Uses 1-minute prices for the last N days (default 7). One $1000 long at a
time. Pays a 0.20% round-trip Binance fee on every exit, including timeouts.

Example:
    python -u scripts/tp_sl_scan.py --days 7 --timeout-minutes 60
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_trading.binance.market import MarketClient
from jev_trading.binance.tp_sl_scan import (
    DEFAULT_PAIRS,
    DEFAULT_SYMBOLS,
    available_symbols,
    load_klines,
    print_summary,
    scan_symbol,
    utc_ms_days_ago,
    write_csv,
)

log = logging.getLogger("tp_sl_scan")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Take-profit / stop-loss scan on Binance Spot")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--interval", default="1m", help="Binance kline size. 1m is right for 7 days.")
    parser.add_argument("--notional", type=float, default=1000.0, help="Dollars per trade")
    parser.add_argument("--timeout-minutes", type=int, default=60)
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=list(DEFAULT_SYMBOLS),
        help="Symbols to scan. Default is a mix of majors, large alts, memecoins, faster alts.",
    )
    parser.add_argument(
        "--out",
        default="data/tp_sl_scan/results.csv",
        help="Where to write the full table",
    )
    parser.add_argument(
        "--log-file",
        default="data/tp_sl_scan/scan.log",
        help="Latest-run log. A timestamped copy is also kept next to it.",
    )
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(latest_log: Path, run_log: Path) -> None:
    latest_log.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    run_handler = _FlushFileHandler(run_log, mode="w", encoding="utf-8")
    run_handler.setFormatter(formatter)
    latest_handler = _FlushFileHandler(latest_log, mode="w", encoding="utf-8")
    latest_handler.setFormatter(formatter)
    root = logging.getLogger("tp_sl_scan")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(run_handler)
    root.addHandler(latest_handler)
    root.propagate = False


def main() -> None:
    args = parse_args()
    out_path = ROOT / args.out
    log_path = ROOT / args.log_file
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log_path = log_path.with_name(f"scan_{stamp}.log")
    setup_logging(log_path, run_log_path)

    timeout_bars = args.timeout_minutes
    if args.interval != "1m":
        log.info("Note: --timeout-minutes is treated as bar count unless interval is 1m.")

    started = time.perf_counter()
    start_ms, end_ms = utc_ms_days_ago(args.days)
    log.info("Starting  %s-day scan", args.days)
    log.info("Interval  %s", args.interval)
    log.info("TP/SL pairs  %s", len(DEFAULT_PAIRS))
    log.info("Timeout  %s minutes", args.timeout_minutes)
    log.info("Fee  0.20%% round trip on $%.0f each trade", args.notional)
    log.info("Log file (this run)  %s", run_log_path)
    log.info("Log file (latest)    %s", log_path)
    log.info("Results CSV          %s", out_path)
    log.info("First run downloads prices (about 2-4 min). Next run reuses the cache.")

    market = MarketClient()
    symbols = available_symbols(market, [s.upper() for s in args.symbols])
    cache_dir = ROOT / "data" / "klines"
    total = len(symbols)
    all_rows = []

    for i, symbol in enumerate(symbols, start=1):
        coin_started = time.perf_counter()
        log.info("----------  [%s/%s] %s  ----------", i, total, symbol)
        klines = load_klines(market, symbol, args.interval, start_ms, end_ms, cache_dir)
        log.info("%s: scanning %s take-profit/stop-loss pairs", symbol, len(DEFAULT_PAIRS))
        rows = scan_symbol(
            symbol,
            klines,
            DEFAULT_PAIRS,
            notional=args.notional,
            timeout_bars=timeout_bars,
            fee_rate=0.002,
        )
        all_rows.extend(rows)
        best = max(rows, key=lambda r: r.net_usd)
        elapsed = time.perf_counter() - started
        coin_secs = time.perf_counter() - coin_started
        remaining = (elapsed / i) * (total - i)
        log.info(
            "%s done in %.1fs. Best here: TP %.2f%% / SL %.2f%%  net $%.2f. "
            "Elapsed %.0fs, about %.0fs left.",
            symbol,
            coin_secs,
            best.take_profit_pct,
            best.stop_loss_pct,
            best.net_usd,
            elapsed,
            remaining,
        )

    write_csv(out_path, all_rows)
    log.info("Wrote full table to %s", out_path)
    log.info("Finished in %.1f seconds", time.perf_counter() - started)
    print_summary(all_rows)


if __name__ == "__main__":
    main()
