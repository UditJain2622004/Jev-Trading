"""Scan dump-then-reclaim longs on cached 1-minute coins. No Jev.

Uses the latest 1m cache per symbol. Enters only at a 15-minute close
after a dump bar, a reclaim bar, and room to the 12-hour high. Also
runs an always-in-every-15m control at the same TP/SL/timeouts.

    python -u scripts/setup_scan.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_trading.binance.setup_scan import (
    DEFAULT_DUMP_PCTS,
    DEFAULT_PAIRS,
    DEFAULT_TIMEOUTS,
    latest_1m_caches,
    load_cached_klines,
    print_summary,
    scan_symbol,
    write_csv,
)

log = logging.getLogger("setup_scan")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump-reclaim swing scan on cached 1m klines")
    parser.add_argument("--cache-dir", default="data/klines")
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--out", default="data/setup_scan/results.csv")
    parser.add_argument("--log-file", default="data/setup_scan/scan.log")
    parser.add_argument("--symbols", nargs="*", default=None, help="Default: every symbol with a 1m cache")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log = log_file.with_name(f"setup_scan_{stamp}.log")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root = logging.getLogger("setup_scan")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (
        stream,
        _FlushFileHandler(run_log, mode="w", encoding="utf-8"),
        _FlushFileHandler(log_file, mode="w", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.propagate = False
    log.info("Log (this run) %s", run_log)
    log.info("Log (latest)   %s", log_file)


def main() -> None:
    args = parse_args()
    out = ROOT / args.out
    setup_logging(ROOT / args.log_file)
    caches = latest_1m_caches(ROOT / args.cache_dir)
    symbols = args.symbols or sorted(caches)
    missing = [s for s in symbols if s not in caches]
    for symbol in missing:
        log.warning("skip %s: no 1m cache", symbol)
    symbols = [s for s in symbols if s in caches]
    log.info("Setup scan: dump+reclaim+room vs always-in 15m. No Jev.")
    log.info("Pairs %s", ", ".join(f"{tp}/{sl}" for tp, sl in DEFAULT_PAIRS))
    log.info("Timeouts hours %s", [t // 60 for t in DEFAULT_TIMEOUTS])
    log.info("Dump pcts %s", list(DEFAULT_DUMP_PCTS))
    log.info("Coins %s: %s", len(symbols), ", ".join(symbols))

    rows = []
    for symbol in symbols:
        path = caches[symbol]
        log.info("%s: %s", symbol, path.name)
        klines = load_cached_klines(path, symbol)
        log.info("%s: %s 1-minute bars", symbol, f"{len(klines):,}")
        rows.extend(
            scan_symbol(
                symbol,
                klines,
                pairs=DEFAULT_PAIRS,
                timeouts=DEFAULT_TIMEOUTS,
                dump_pcts=DEFAULT_DUMP_PCTS,
                notional=args.notional,
            )
        )

    write_csv(out, rows)
    print_summary(rows)
    log.info("Wrote %s", out)


if __name__ == "__main__":
    main()
