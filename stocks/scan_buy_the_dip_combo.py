"""Custom buy-the-dip combo sweep from phone request.

Dip 4/5%, lookback 4 sessions, wait 30..300m, stable ±1.5%,
TP 5% / SL 4%, timeout 2 calendar days (wall-clock via bar open_time).
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
from stocks.dip import (
    BAR_MINUTES,
    ROUND_TRIP_FEE,
    SESSION_MINUTES,
    DipLevers,
    DipResult,
    find_entries,
    minutes_to_bars,
    print_summary,
    scan_symbol,
    write_csv,
)
import stocks.dip as dip

log = logging.getLogger("stocks")

TIMEOUT_CALENDAR_MS = 2 * 24 * 60 * 60 * 1000  # 2 days wall-clock


def build_combos() -> tuple[DipLevers, ...]:
    waits = (30, 60, 120, 180, 240, 300)
    dips = (4.0, 5.0)
    out: list[DipLevers] = []
    for dip_pct in dips:
        for wait in waits:
            name = f"d{int(dip_pct)}_w{wait}"
            out.append(
                DipLevers(
                    name=name,
                    dip_pct=dip_pct,
                    dip_minutes=4 * SESSION_MINUTES,
                    stable_range_pct=1.5,
                    stable_minutes=wait,
                    take_profit_pct=5.0,
                    stop_loss_pct=4.0,
                    # unused by calendar exit; kept for CSV/reporting
                    timeout_minutes=2 * 24 * 60,
                )
            )
    return tuple(out)


def _exit_trade_calendar(
    bars,
    entry_index: int,
    levers: DipLevers,
    *,
    notional: float,
    fee_rate: float,
):
    entry = bars[entry_index].close
    tp = entry * (1.0 + levers.take_profit_pct / 100.0)
    sl = entry * (1.0 - levers.stop_loss_pct / 100.0)
    deadline = bars[entry_index].open_time + TIMEOUT_CALENDAR_MS
    j = entry_index + 1
    last = len(bars) - 1
    while j <= last:
        hit_tp = bars[j].high >= tp
        hit_sl = bars[j].low <= sl
        hold = j - entry_index
        timed_out = bars[j].open_time >= deadline
        if hit_tp and hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, True
        if hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, False
        if hit_tp:
            return "win", (levers.take_profit_pct / 100.0 - fee_rate) * notional, hold, False
        if timed_out:
            move = (bars[j].close / entry) - 1.0
            return "timeout", (move - fee_rate) * notional, hold, False
        j += 1
    hold = last - entry_index
    move = (bars[last].close / entry) - 1.0
    return "timeout", (move - fee_rate) * notional, hold, False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy-the-dip custom combo sweep")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--period", default="60d")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--out", default="data/stocks/buy_the_dip/combo_d4d5_4sess_2day.csv")
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
        _FlushFileHandler(log_dir / f"combo_{stamp}.log", mode="w", encoding="utf-8"),
        _FlushFileHandler(log_dir / "combo_scan.log", mode="w", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.propagate = False


def main() -> None:
    args = parse_args()
    setup_logging()
    combos = build_combos()
    # Use calendar-day timeout instead of session-bar timeout
    dip._exit_trade = _exit_trade_calendar

    symbols = [s.upper() for s in args.symbols]
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    log.info(
        "Combo sweep: dip 4/5%%, 4 sessions lookback, waits %s, ±1.5%%, TP5/SL4, timeout 2 calendar days",
        [30, 60, 120, 180, 240, 300],
    )
    log.info("$%.0f clip. 0.30%% buy + 0.30%% sell.", args.notional)
    log.info("Symbols %s: %s", len(symbols), ", ".join(symbols))
    log.info("Combos %s", len(combos))
    for levers in combos:
        log.info(
            "  %-10s dip %.0f%% / 4sess  wait ±%.1f%% after %sm  TP %.0f SL %.0f  timeout 2d wall",
            levers.name,
            levers.dip_pct,
            levers.stable_range_pct,
            levers.stable_minutes,
            levers.take_profit_pct,
            levers.stop_loss_pct,
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
        rows.extend(scan_symbol(symbol, bars, combos, notional=args.notional))

    out = ROOT / args.out
    write_csv(out, rows)
    print_summary(rows)

    # Aggregate by combo across symbols
    log.info("")
    log.info("=== Aggregate by combo (all symbols) ===")
    by = {}
    for r in rows:
        by.setdefault(r.combo, []).append(r)
    log.info(
        "  %-10s %6s %6s %8s %8s %8s %10s %8s",
        "combo",
        "rows",
        "trades",
        "W",
        "L",
        "T",
        "net$",
        "avg_net",
    )
    for name in sorted(by.keys(), key=lambda k: sum(x.net_usd for x in by[k]), reverse=True):
        group = by[name]
        trades = sum(x.trades for x in group)
        wins = sum(x.wins for x in group)
        losses = sum(x.losses for x in group)
        timeouts = sum(x.timeouts for x in group)
        net = sum(x.net_usd for x in group)
        log.info(
            "  %-10s %6d %6d %8d %8d %8d %10.2f %8.2f",
            name,
            len(group),
            trades,
            wins,
            losses,
            timeouts,
            net,
            net / len(group),
        )

    log.info("Wrote %s", out)
    log.info("Finished in %.1fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
