"""Wider-horizon setup scan. No Jev.

Always-in 1-minute longs lose because a 0.20% fee sits on a 0.50% target.
This scan asks a prior question: if we only buy a dump-then-reclaim, with
room to a 12-hour high, and we aim for 1.5–3% with a 1–1.5% stop over
4–12 hours, is brute even near breakeven?

Entry is the close of a completed 15-minute bar. Exits still walk 1-minute
bars so we do not skip intra-bar stops. One $1000 long at a time.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from jev_trading.binance.market import parse_kline
from jev_trading.binance.tp_sl_scan import COIN_GROUPS, ROUND_TRIP_FEE
from jev_trading.binance.types import Kline

log = logging.getLogger("setup_scan")

BAR_MINUTES = 15
ROOM_LOOKBACK_MINUTES = 12 * 60
DEFAULT_PAIRS: tuple[tuple[float, float], ...] = (
    (1.50, 1.00),
    (2.00, 1.00),
    (2.00, 1.50),
    (2.50, 1.20),
    (3.00, 1.50),
)
DEFAULT_TIMEOUTS = (4 * 60, 8 * 60, 12 * 60)
DEFAULT_DUMP_PCTS = (0.80, 1.20, 1.50)


@dataclass(frozen=True, slots=True)
class Bar15:
    start_ms: int
    end_index: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class SetupResult:
    symbol: str
    group: str
    mode: str
    dump_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    timeout_minutes: int
    trades: int
    wins: int
    losses: int
    timeouts: int
    both_hit: int
    win_rate_pct: float
    resolved_win_rate_pct: float | None
    breakeven_win_rate_pct: float
    edge_vs_be_pp: float | None
    net_usd: float
    fees_usd: float
    avg_hold_minutes: float
    avg_win_usd: float
    avg_loss_usd: float
    worst_trade_usd: float
    best_trade_usd: float


def latest_1m_caches(cache_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in cache_dir.glob("*_1m_*.json"):
        symbol = path.name.split("_1m_")[0]
        end_ts = int(path.stem.split("_")[-1])
        prev = found.get(symbol)
        if prev is None or int(prev.stem.split("_")[-1]) < end_ts:
            found[symbol] = path
    return found


def load_cached_klines(path: Path, symbol: str) -> list[Kline]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [parse_kline(symbol, "1m", row) for row in raw]


def breakeven_win_rate_pct(take_profit_pct: float, stop_loss_pct: float, fee_rate: float = ROUND_TRIP_FEE) -> float:
    win = take_profit_pct / 100.0 - fee_rate
    loss = stop_loss_pct / 100.0 + fee_rate
    if win + loss <= 0:
        return 100.0
    return 100.0 * loss / (win + loss)


def build_15m_bars(klines: Sequence[Kline], bar_minutes: int = BAR_MINUTES) -> list[Bar15]:
    width_ms = bar_minutes * 60_000
    buckets: dict[int, list[int]] = {}
    order: list[int] = []
    for i, kline in enumerate(klines):
        key = kline.open_time // width_ms
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(i)

    bars: list[Bar15] = []
    for key in order:
        idxs = buckets[key]
        if len(idxs) < bar_minutes:
            continue
        window = [klines[i] for i in idxs]
        bars.append(
            Bar15(
                start_ms=window[0].open_time,
                end_index=idxs[-1],
                open=float(window[0].open),
                high=max(float(k.high) for k in window),
                low=min(float(k.low) for k in window),
                close=float(window[-1].close),
            )
        )
    return bars


def is_dump_reclaim(prev: Bar15, curr: Bar15, dump_pct: float) -> bool:
    if prev.open <= 0:
        return False
    dump = (prev.close / prev.open - 1.0) * 100.0 <= -dump_pct
    if not dump:
        return False
    mid = (prev.high + prev.low) / 2.0
    reclaim = curr.close > mid and curr.close > curr.open
    return reclaim


def room_pct(entry: float, lookback_high: float) -> float:
    if entry <= 0:
        return 0.0
    return (lookback_high / entry - 1.0) * 100.0


def lookback_high(klines: Sequence[Kline], end_index: int, lookback_minutes: int) -> float:
    start = max(0, end_index - lookback_minutes + 1)
    return max(float(klines[i].high) for i in range(start, end_index + 1))


def _simulate_entries(
    klines: Sequence[Kline],
    entries: Sequence[int],
    take_profit_pct: float,
    stop_loss_pct: float,
    *,
    timeout_minutes: int,
    notional: float,
    fee_rate: float,
) -> dict[str, float | int | None]:
    tp_frac = take_profit_pct / 100.0
    sl_frac = stop_loss_pct / 100.0
    wins = losses = timeouts = both_hit = 0
    pnls: list[float] = []
    holds: list[int] = []
    last_exit = -1
    last_bar = len(klines) - 1

    for i in entries:
        if i <= last_exit or i >= last_bar:
            continue
        entry = float(klines[i].close)
        tp = entry * (1.0 + tp_frac)
        sl = entry * (1.0 - sl_frac)
        end_j = min(last_bar, i + timeout_minutes)
        resolved = False
        j = i + 1
        while j <= end_j:
            high = float(klines[j].high)
            low = float(klines[j].low)
            hit_tp = high >= tp
            hit_sl = low <= sl
            hold = j - i
            if hit_tp and hit_sl:
                pnl = (-sl_frac - fee_rate) * notional
                losses += 1
                both_hit += 1
                pnls.append(pnl)
                holds.append(hold)
                last_exit = j
                resolved = True
                break
            if hit_sl:
                pnl = (-sl_frac - fee_rate) * notional
                losses += 1
                pnls.append(pnl)
                holds.append(hold)
                last_exit = j
                resolved = True
                break
            if hit_tp:
                pnl = (tp_frac - fee_rate) * notional
                wins += 1
                pnls.append(pnl)
                holds.append(hold)
                last_exit = j
                resolved = True
                break
            j += 1
        if not resolved:
            last_close = float(klines[end_j].close)
            move = (last_close / entry) - 1.0
            pnl = (move - fee_rate) * notional
            timeouts += 1
            pnls.append(pnl)
            holds.append(end_j - i)
            last_exit = end_j

    trades = len(pnls)
    resolved_n = wins + losses
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    be = breakeven_win_rate_pct(take_profit_pct, stop_loss_pct, fee_rate)
    resolved_wr = (100.0 * wins / resolved_n) if resolved_n else None
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "both_hit": both_hit,
        "win_rate_pct": (100.0 * wins / trades) if trades else 0.0,
        "resolved_win_rate_pct": resolved_wr,
        "breakeven_win_rate_pct": be,
        "edge_vs_be_pp": (resolved_wr - be) if resolved_wr is not None else None,
        "net_usd": sum(pnls) if pnls else 0.0,
        "fees_usd": trades * fee_rate * notional,
        "avg_hold_minutes": (sum(holds) / trades) if trades else 0.0,
        "avg_win_usd": (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0,
        "avg_loss_usd": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0,
        "worst_trade_usd": min(pnls) if pnls else 0.0,
        "best_trade_usd": max(pnls) if pnls else 0.0,
    }


def setup_entries(
    klines: Sequence[Kline],
    bars: Sequence[Bar15],
    *,
    dump_pct: float,
    room_need_pct: float,
    lookback_minutes: int = ROOM_LOOKBACK_MINUTES,
) -> list[int]:
    entries: list[int] = []
    for prev, curr in zip(bars, bars[1:]):
        if curr.end_index < lookback_minutes:
            continue
        if not is_dump_reclaim(prev, curr, dump_pct):
            continue
        high = lookback_high(klines, curr.end_index, lookback_minutes)
        if room_pct(curr.close, high) < room_need_pct:
            continue
        entries.append(curr.end_index)
    return entries


def always_in_entries(bars: Sequence[Bar15], *, lookback_minutes: int = ROOM_LOOKBACK_MINUTES) -> list[int]:
    return [bar.end_index for bar in bars if bar.end_index >= lookback_minutes]


def scan_symbol(
    symbol: str,
    klines: Sequence[Kline],
    *,
    pairs: Iterable[tuple[float, float]] = DEFAULT_PAIRS,
    timeouts: Iterable[int] = DEFAULT_TIMEOUTS,
    dump_pcts: Iterable[float] = DEFAULT_DUMP_PCTS,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
    include_always_in: bool = True,
) -> list[SetupResult]:
    group = COIN_GROUPS.get(symbol, "other")
    bars = build_15m_bars(klines)
    results: list[SetupResult] = []

    def add(mode: str, dump_pct: float, tp: float, sl: float, timeout: int, entries: Sequence[int]) -> None:
        stats = _simulate_entries(
            klines,
            entries,
            tp,
            sl,
            timeout_minutes=timeout,
            notional=notional,
            fee_rate=fee_rate,
        )
        row = SetupResult(
            symbol=symbol,
            group=group,
            mode=mode,
            dump_pct=dump_pct,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            timeout_minutes=timeout,
            **stats,  # type: ignore[arg-type]
        )
        results.append(row)
        log.info(
            "%s  %s  dump=%.2f  TP %.2f SL %.2f  %sh  n=%s  W/L/T=%s/%s/%s  res=%.1f  be=%.1f  net=$%.2f",
            symbol,
            mode,
            dump_pct,
            tp,
            sl,
            timeout // 60,
            row.trades,
            row.wins,
            row.losses,
            row.timeouts,
            row.resolved_win_rate_pct if row.resolved_win_rate_pct is not None else -1.0,
            row.breakeven_win_rate_pct,
            row.net_usd,
        )

    pair_list = list(pairs)
    timeout_list = list(timeouts)
    if include_always_in:
        always = always_in_entries(bars)
        for tp, sl in pair_list:
            for timeout in timeout_list:
                add("always_in_15m", 0.0, tp, sl, timeout, always)

    for dump_pct in dump_pcts:
        for tp, sl in pair_list:
            entries = setup_entries(klines, bars, dump_pct=dump_pct, room_need_pct=tp)
            for timeout in timeout_list:
                add("dump_reclaim", dump_pct, tp, sl, timeout, entries)
    return results


def write_csv(path: Path, rows: Sequence[SetupResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def print_summary(rows: Sequence[SetupResult]) -> None:
    if not rows:
        log.info("No results.")
        return
    setup = [r for r in rows if r.mode == "dump_reclaim"]
    always = [r for r in rows if r.mode == "always_in_15m"]
    positive = [r for r in setup if r.net_usd > 0 and r.trades >= 5]
    near = [
        r
        for r in setup
        if r.trades >= 8
        and r.resolved_win_rate_pct is not None
        and r.resolved_win_rate_pct + 1e-9 >= r.breakeven_win_rate_pct
    ]
    log.info("")
    log.info("Setup rows %s  always-in rows %s", len(setup), len(always))
    log.info("Setup with net > 0 and at least 5 trades: %s", len(positive))
    log.info("Setup at/above breakeven resolved WR and at least 8 trades: %s", len(near))

    def table(title: str, picked: Sequence[SetupResult]) -> None:
        log.info("")
        log.info(title)
        if not picked:
            log.info("  (none)")
            return
        log.info(
            "  %s %s %s %s %s %s %s %s %s %s %s",
            f"{'coin':12}",
            f"{'mode':14}",
            f"{'dump':>5}",
            f"{'TP':>5}",
            f"{'SL':>5}",
            f"{'h':>3}",
            f"{'n':>4}",
            f"{'res%':>6}",
            f"{'be%':>6}",
            f"{'net$':>9}",
            f"{'edge':>6}",
        )
        for row in picked:
            log.info(
                "  %s %s %5.2f %5.2f %5.2f %3d %4d %6s %6.1f %9.2f %6s",
                f"{row.symbol:12}",
                f"{row.mode:14}",
                row.dump_pct,
                row.take_profit_pct,
                row.stop_loss_pct,
                row.timeout_minutes // 60,
                row.trades,
                f"{row.resolved_win_rate_pct:.1f}" if row.resolved_win_rate_pct is not None else "  n/a",
                row.breakeven_win_rate_pct,
                row.net_usd,
                f"{row.edge_vs_be_pp:+.1f}" if row.edge_vs_be_pp is not None else "  n/a",
            )

    table("Best 12 setup rows by net dollars", sorted(setup, key=lambda r: r.net_usd, reverse=True)[:12])
    table("Worst 8 setup rows by net dollars", sorted(setup, key=lambda r: r.net_usd)[:8])
    if near:
        table("Setup rows at/above breakeven", sorted(near, key=lambda r: r.net_usd, reverse=True)[:15])
    sol_setup = [r for r in setup if r.symbol == "SOLUSDT"]
    sol_always = [r for r in always if r.symbol == "SOLUSDT"]
    table("SOL setup, best 8 by net", sorted(sol_setup, key=lambda r: r.net_usd, reverse=True)[:8])
    table("SOL always-in 15m, best 5 by net", sorted(sol_always, key=lambda r: r.net_usd, reverse=True)[:5])
