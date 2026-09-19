"""Buy-the-dip scan. No Jev.

A dip is an X% fall from the window high to the close, inside `dip_minutes`.
If the next `stable_minutes` stay inside a tight range, buy the close of that
base. Exit with the combo's take-profit / stop. Same-bar TP+SL counts as a stop.
One $1000 long at a time. 0.20% round-trip fee, including timeouts.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from typing import Sequence

from jev_trading.binance.setup_scan import breakeven_win_rate_pct
from jev_trading.binance.tp_sl_scan import COIN_GROUPS, ROUND_TRIP_FEE
from jev_trading.binance.types import Kline

log = logging.getLogger("buy_the_dip")

DEFAULT_SYMBOLS = ("SOLUSDT", "ETHUSDT", "ARBUSDT", "NEARUSDT", "WIFUSDT")
TIMEOUT_MINUTES = 8 * 60


@dataclass(frozen=True, slots=True)
class DipLevers:
    name: str
    dip_pct: float
    dip_minutes: int
    stable_range_pct: float
    stable_minutes: int
    take_profit_pct: float
    stop_loss_pct: float
    timeout_minutes: int = TIMEOUT_MINUTES


# Ten locked combinations. Same list on every coin.
DEFAULT_COMBOS: tuple[DipLevers, ...] = (
    DipLevers("sharp_quick", dip_pct=1.2, dip_minutes=15, stable_range_pct=0.25, stable_minutes=10, take_profit_pct=1.2, stop_loss_pct=0.7),
    DipLevers("sharp_base", dip_pct=1.5, dip_minutes=30, stable_range_pct=0.30, stable_minutes=15, take_profit_pct=1.5, stop_loss_pct=0.8),
    DipLevers("mid_fast", dip_pct=2.0, dip_minutes=30, stable_range_pct=0.30, stable_minutes=15, take_profit_pct=2.0, stop_loss_pct=1.0),
    DipLevers("mid_base", dip_pct=2.0, dip_minutes=60, stable_range_pct=0.40, stable_minutes=30, take_profit_pct=2.0, stop_loss_pct=1.0),
    DipLevers("mid_longbase", dip_pct=2.0, dip_minutes=60, stable_range_pct=0.30, stable_minutes=45, take_profit_pct=2.0, stop_loss_pct=1.0),
    DipLevers("hard_hour", dip_pct=3.0, dip_minutes=60, stable_range_pct=0.40, stable_minutes=20, take_profit_pct=2.5, stop_loss_pct=1.2),
    DipLevers("hard_2h", dip_pct=3.0, dip_minutes=120, stable_range_pct=0.50, stable_minutes=30, take_profit_pct=3.0, stop_loss_pct=1.5),
    DipLevers("wide_slow", dip_pct=4.0, dip_minutes=180, stable_range_pct=0.60, stable_minutes=45, take_profit_pct=3.0, stop_loss_pct=1.5),
    DipLevers("chop_tight", dip_pct=1.5, dip_minutes=45, stable_range_pct=0.20, stable_minutes=20, take_profit_pct=1.5, stop_loss_pct=1.0),
    DipLevers("asymmetric", dip_pct=2.5, dip_minutes=45, stable_range_pct=0.35, stable_minutes=20, take_profit_pct=2.5, stop_loss_pct=1.0),
)


@dataclass(frozen=True, slots=True)
class DipResult:
    symbol: str
    group: str
    combo: str
    dip_pct: float
    dip_minutes: int
    stable_range_pct: float
    stable_minutes: int
    take_profit_pct: float
    stop_loss_pct: float
    timeout_minutes: int
    candidates: int
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


def is_dip(klines: Sequence[Kline], i: int, *, dip_pct: float, dip_minutes: int) -> bool:
    start = i - dip_minutes + 1
    if start < 0:
        return False
    close = float(klines[i].close)
    if close <= 0:
        return False
    high = max(float(klines[j].high) for j in range(start, i + 1))
    if (high / close - 1.0) * 100.0 < dip_pct:
        return False
    high_cutoff = start + max(1, int(dip_minutes * 0.8))
    high_at = start
    for j in range(start, i + 1):
        if abs(float(klines[j].high) - high) < 1e-12:
            high_at = j
            break
    return high_at < high_cutoff


def is_stable(
    klines: Sequence[Kline],
    dip_index: int,
    *,
    stable_range_pct: float,
    stable_minutes: int,
) -> bool:
    start = dip_index + 1
    end = dip_index + stable_minutes
    if start >= len(klines) or end >= len(klines):
        return False
    ref = float(klines[dip_index].close)
    if ref <= 0:
        return False
    hi = max(float(klines[j].high) for j in range(start, end + 1))
    lo = min(float(klines[j].low) for j in range(start, end + 1))
    return (hi - lo) / ref * 100.0 <= stable_range_pct


def find_entries(klines: Sequence[Kline], levers: DipLevers) -> tuple[list[int], int]:
    """Return (entry indexes, dip-then-stable candidate count). One open trade at a time."""
    entries: list[int] = []
    candidates = 0
    i = levers.dip_minutes - 1
    last_bar = len(klines) - 1
    while i < last_bar - levers.stable_minutes:
        if not is_dip(klines, i, dip_pct=levers.dip_pct, dip_minutes=levers.dip_minutes):
            i += 1
            continue
        if not is_stable(
            klines,
            i,
            stable_range_pct=levers.stable_range_pct,
            stable_minutes=levers.stable_minutes,
        ):
            i += 1
            continue
        candidates += 1
        entries.append(i + levers.stable_minutes)
        i += levers.stable_minutes + 1
    return entries, candidates


def _exit_trade(
    klines: Sequence[Kline],
    entry_index: int,
    levers: DipLevers,
    *,
    notional: float,
    fee_rate: float,
) -> tuple[str, float, int, bool]:
    entry = float(klines[entry_index].close)
    tp = entry * (1.0 + levers.take_profit_pct / 100.0)
    sl = entry * (1.0 - levers.stop_loss_pct / 100.0)
    end_j = min(len(klines) - 1, entry_index + levers.timeout_minutes)
    j = entry_index + 1
    while j <= end_j:
        high = float(klines[j].high)
        low = float(klines[j].low)
        hit_tp = high >= tp
        hit_sl = low <= sl
        hold = j - entry_index
        if hit_tp and hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, True
        if hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, False
        if hit_tp:
            return "win", (levers.take_profit_pct / 100.0 - fee_rate) * notional, hold, False
        j += 1
    move = (float(klines[end_j].close) / entry) - 1.0
    return "timeout", (move - fee_rate) * notional, end_j - entry_index, False


def simulate(
    klines: Sequence[Kline],
    levers: DipLevers,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> dict[str, float | int | None]:
    raw_entries, candidates = find_entries(klines, levers)
    wins = losses = timeouts = both_hit = 0
    pnls: list[float] = []
    holds: list[int] = []
    last_exit = -1
    for entry_index in raw_entries:
        if entry_index <= last_exit or entry_index >= len(klines) - 1:
            continue
        outcome, pnl, hold, both = _exit_trade(
            klines, entry_index, levers, notional=notional, fee_rate=fee_rate
        )
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
            if both:
                both_hit += 1
        else:
            timeouts += 1
        pnls.append(pnl)
        holds.append(hold)
        last_exit = entry_index + hold

    trades = len(pnls)
    resolved_n = wins + losses
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    be = breakeven_win_rate_pct(levers.take_profit_pct, levers.stop_loss_pct, fee_rate)
    resolved_wr = (100.0 * wins / resolved_n) if resolved_n else None
    return {
        "candidates": candidates,
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


def scan_symbol(
    symbol: str,
    klines: Sequence[Kline],
    combos: Sequence[DipLevers] = DEFAULT_COMBOS,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> list[DipResult]:
    group = COIN_GROUPS.get(symbol, "other")
    rows: list[DipResult] = []
    for levers in combos:
        stats = simulate(klines, levers, notional=notional, fee_rate=fee_rate)
        row = DipResult(
            symbol=symbol,
            group=group,
            combo=levers.name,
            dip_pct=levers.dip_pct,
            dip_minutes=levers.dip_minutes,
            stable_range_pct=levers.stable_range_pct,
            stable_minutes=levers.stable_minutes,
            take_profit_pct=levers.take_profit_pct,
            stop_loss_pct=levers.stop_loss_pct,
            timeout_minutes=levers.timeout_minutes,
            **stats,  # type: ignore[arg-type]
        )
        rows.append(row)
        log.info(
            "%s  %s  dip %.2f%%/%sm  stable %.2f%%/%sm  TP/SL %.2f/%.2f  "
            "cand=%s n=%s W/L/T=%s/%s/%s  res=%s  be=%.1f  net=$%.2f",
            symbol,
            levers.name,
            levers.dip_pct,
            levers.dip_minutes,
            levers.stable_range_pct,
            levers.stable_minutes,
            levers.take_profit_pct,
            levers.stop_loss_pct,
            row.candidates,
            row.trades,
            row.wins,
            row.losses,
            row.timeouts,
            f"{row.resolved_win_rate_pct:.1f}" if row.resolved_win_rate_pct is not None else "n/a",
            row.breakeven_win_rate_pct,
            row.net_usd,
        )
    return rows


def write_csv(path, rows: Sequence[DipResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def print_summary(rows: Sequence[DipResult]) -> None:
    if not rows:
        log.info("No results.")
        return
    live = [r for r in rows if r.trades >= 5]
    green = [r for r in live if r.net_usd > 0]
    be = [
        r
        for r in live
        if r.resolved_win_rate_pct is not None and r.resolved_win_rate_pct >= r.breakeven_win_rate_pct
    ]
    log.info("")
    log.info("Rows %s  with at least 5 trades %s  net>0 %s  at/above breakeven %s", len(rows), len(live), len(green), len(be))

    def table(title: str, picked: Sequence[DipResult]) -> None:
        log.info("")
        log.info(title)
        if not picked:
            log.info("  (none)")
            return
        log.info(
            "  %s %s %s %s %s %s %s %s %s %s",
            f"{'coin':10}",
            f"{'combo':14}",
            f"{'dip':>10}",
            f"{'stable':>12}",
            f"{'exit':>11}",
            f"{'n':>4}",
            f"{'W/L/T':>9}",
            f"{'res%':>6}",
            f"{'be%':>6}",
            f"{'net$':>9}",
        )
        for row in picked:
            log.info(
                "  %s %s %s %s %s %4d %9s %6s %6.1f %9.2f",
                f"{row.symbol:10}",
                f"{row.combo:14}",
                f"{row.dip_pct:.1f}%/{row.dip_minutes}m",
                f"{row.stable_range_pct:.2f}%/{row.stable_minutes}m",
                f"{row.take_profit_pct:.1f}/{row.stop_loss_pct:.1f}",
                row.trades,
                f"{row.wins}/{row.losses}/{row.timeouts}",
                f"{row.resolved_win_rate_pct:.1f}" if row.resolved_win_rate_pct is not None else "n/a",
                row.breakeven_win_rate_pct,
                row.net_usd,
            )

    table("Best 12 by net (any trade count)", sorted(rows, key=lambda r: r.net_usd, reverse=True)[:12])
    table("Best among n>=5", sorted(live, key=lambda r: r.net_usd, reverse=True)[:10])
    table("Worst 8 by net", sorted(rows, key=lambda r: r.net_usd)[:8])
    for symbol in sorted({r.symbol for r in rows}):
        coin = [r for r in rows if r.symbol == symbol]
        best = max(coin, key=lambda r: r.net_usd)
        log.info(
            "Best %s: %s  n=%s  net=$%.2f  res=%s",
            symbol,
            best.combo,
            best.trades,
            best.net_usd,
            f"{best.resolved_win_rate_pct:.1f}%" if best.resolved_win_rate_pct is not None else "n/a",
        )
