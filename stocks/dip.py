"""Buy-the-dip on US stock 5-minute bars.

Fee is 0.3% on the buy and 0.3% on the sell (0.60% round trip), including
timeouts. One $1000 long at a time. Regular-hours bars only.

A dip is: in the last N minutes of trading, price dropped at least X% from
the high of that stretch to the close, and the high was earlier in the stretch.
Then we wait. Price may swing. The last close of the wait must still be within
the stable range of the dip close. Then we buy.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from typing import Sequence

from stocks.bars import BAR_MINUTES, Bar

log = logging.getLogger("stocks")

ROUND_TRIP_FEE = 0.006  # 0.3% buy + 0.3% sell
SESSION_MINUTES = 390  # 6.5 hours of regular trading


@dataclass(frozen=True, slots=True)
class DipLevers:
    name: str
    dip_pct: float
    dip_minutes: int
    stable_range_pct: float
    stable_minutes: int
    take_profit_pct: float
    stop_loss_pct: float
    timeout_minutes: int


# Ten semiconductor-sized combinations. Windows are trading minutes, not
# wall-clock (a session is 390 minutes). Targets are large vs 0.60% fees.
DEFAULT_COMBOS: tuple[DipLevers, ...] = (
    DipLevers("dip4_1d", dip_pct=4.0, dip_minutes=SESSION_MINUTES, stable_range_pct=1.2, stable_minutes=30, take_profit_pct=5.0, stop_loss_pct=3.0, timeout_minutes=2 * SESSION_MINUTES),
    DipLevers("dip5_1d", dip_pct=5.0, dip_minutes=SESSION_MINUTES, stable_range_pct=1.5, stable_minutes=30, take_profit_pct=6.0, stop_loss_pct=3.0, timeout_minutes=2 * SESSION_MINUTES),
    DipLevers("dip5_2d", dip_pct=5.0, dip_minutes=2 * SESSION_MINUTES, stable_range_pct=1.5, stable_minutes=45, take_profit_pct=6.0, stop_loss_pct=3.5, timeout_minutes=3 * SESSION_MINUTES),
    DipLevers("dip6_1d", dip_pct=6.0, dip_minutes=SESSION_MINUTES, stable_range_pct=1.5, stable_minutes=30, take_profit_pct=6.0, stop_loss_pct=3.0, timeout_minutes=2 * SESSION_MINUTES),
    DipLevers("dip6_2d", dip_pct=6.0, dip_minutes=2 * SESSION_MINUTES, stable_range_pct=2.0, stable_minutes=60, take_profit_pct=7.0, stop_loss_pct=3.5, timeout_minutes=3 * SESSION_MINUTES),
    DipLevers("dip7_2d", dip_pct=7.0, dip_minutes=2 * SESSION_MINUTES, stable_range_pct=2.0, stable_minutes=45, take_profit_pct=7.0, stop_loss_pct=4.0, timeout_minutes=3 * SESSION_MINUTES),
    DipLevers("dip8_2d", dip_pct=8.0, dip_minutes=2 * SESSION_MINUTES, stable_range_pct=2.0, stable_minutes=60, take_profit_pct=8.0, stop_loss_pct=4.0, timeout_minutes=3 * SESSION_MINUTES),
    DipLevers("dip8_3d", dip_pct=8.0, dip_minutes=3 * SESSION_MINUTES, stable_range_pct=2.5, stable_minutes=90, take_profit_pct=8.0, stop_loss_pct=4.0, timeout_minutes=4 * SESSION_MINUTES),
    DipLevers("dip4_2d", dip_pct=4.0, dip_minutes=2 * SESSION_MINUTES, stable_range_pct=1.2, stable_minutes=45, take_profit_pct=5.0, stop_loss_pct=3.0, timeout_minutes=3 * SESSION_MINUTES),
    DipLevers("dip10_3d", dip_pct=10.0, dip_minutes=3 * SESSION_MINUTES, stable_range_pct=2.5, stable_minutes=60, take_profit_pct=10.0, stop_loss_pct=5.0, timeout_minutes=5 * SESSION_MINUTES),
)


@dataclass(frozen=True, slots=True)
class DipResult:
    symbol: str
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


def minutes_to_bars(minutes: int) -> int:
    return max(1, minutes // BAR_MINUTES)


def breakeven_win_rate_pct(take_profit_pct: float, stop_loss_pct: float, fee_rate: float = ROUND_TRIP_FEE) -> float:
    win = take_profit_pct / 100.0 - fee_rate
    loss = stop_loss_pct / 100.0 + fee_rate
    if win + loss <= 0:
        return 100.0
    return 100.0 * loss / (win + loss)


def is_dip(bars: Sequence[Bar], i: int, *, dip_pct: float, dip_bars: int) -> bool:
    start = i - dip_bars + 1
    if start < 0:
        return False
    close = bars[i].close
    if close <= 0:
        return False
    high = max(bars[j].high for j in range(start, i + 1))
    if (high / close - 1.0) * 100.0 < dip_pct:
        return False
    high_cutoff = start + max(1, int(dip_bars * 0.8))
    high_at = start
    for j in range(start, i + 1):
        if abs(bars[j].high - high) < 1e-12:
            high_at = j
            break
    return high_at < high_cutoff


def is_stable(
    bars: Sequence[Bar],
    dip_index: int,
    *,
    stable_range_pct: float,
    stable_bars: int,
) -> bool:
    end = dip_index + stable_bars
    if end >= len(bars) or stable_bars < 1:
        return False
    ref = bars[dip_index].close
    if ref <= 0:
        return False
    last = bars[end].close
    return abs(last / ref - 1.0) * 100.0 <= stable_range_pct


def find_entries(bars: Sequence[Bar], levers: DipLevers) -> tuple[list[int], int]:
    dip_bars = minutes_to_bars(levers.dip_minutes)
    stable_bars = minutes_to_bars(levers.stable_minutes)
    entries: list[int] = []
    candidates = 0
    i = dip_bars - 1
    last_bar = len(bars) - 1
    while i < last_bar - stable_bars:
        if not is_dip(bars, i, dip_pct=levers.dip_pct, dip_bars=dip_bars):
            i += 1
            continue
        if not is_stable(bars, i, stable_range_pct=levers.stable_range_pct, stable_bars=stable_bars):
            i += 1
            continue
        candidates += 1
        entries.append(i + stable_bars)
        i += stable_bars + 1
    return entries, candidates


def _exit_trade(
    bars: Sequence[Bar],
    entry_index: int,
    levers: DipLevers,
    *,
    notional: float,
    fee_rate: float,
) -> tuple[str, float, int, bool]:
    entry = bars[entry_index].close
    tp = entry * (1.0 + levers.take_profit_pct / 100.0)
    sl = entry * (1.0 - levers.stop_loss_pct / 100.0)
    timeout_bars = minutes_to_bars(levers.timeout_minutes)
    end_j = min(len(bars) - 1, entry_index + timeout_bars)
    j = entry_index + 1
    while j <= end_j:
        hit_tp = bars[j].high >= tp
        hit_sl = bars[j].low <= sl
        hold = j - entry_index
        if hit_tp and hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, True
        if hit_sl:
            return "loss", (-levers.stop_loss_pct / 100.0 - fee_rate) * notional, hold, False
        if hit_tp:
            return "win", (levers.take_profit_pct / 100.0 - fee_rate) * notional, hold, False
        j += 1
    move = (bars[end_j].close / entry) - 1.0
    return "timeout", (move - fee_rate) * notional, end_j - entry_index, False


def simulate(
    bars: Sequence[Bar],
    levers: DipLevers,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> dict[str, float | int | None]:
    raw_entries, candidates = find_entries(bars, levers)
    wins = losses = timeouts = both_hit = 0
    pnls: list[float] = []
    holds: list[int] = []
    last_exit = -1
    for entry_index in raw_entries:
        if entry_index <= last_exit or entry_index >= len(bars) - 1:
            continue
        outcome, pnl, hold, both = _exit_trade(
            bars, entry_index, levers, notional=notional, fee_rate=fee_rate
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
        "avg_hold_minutes": (sum(holds) * BAR_MINUTES / trades) if trades else 0.0,
        "avg_win_usd": (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0,
        "avg_loss_usd": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0,
        "worst_trade_usd": min(pnls) if pnls else 0.0,
        "best_trade_usd": max(pnls) if pnls else 0.0,
    }


def scan_symbol(
    symbol: str,
    bars: Sequence[Bar],
    combos: Sequence[DipLevers] = DEFAULT_COMBOS,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> list[DipResult]:
    rows: list[DipResult] = []
    for levers in combos:
        stats = simulate(bars, levers, notional=notional, fee_rate=fee_rate)
        row = DipResult(
            symbol=symbol,
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
            "%s  %s  dip %.1f%% / %.1fsess  wait %.1f%%/%sm  TP/SL %.1f/%.1f  "
            "cand=%s n=%s W/L/T=%s/%s/%s  res=%s  be=%.1f  net=$%.2f",
            symbol,
            levers.name,
            levers.dip_pct,
            levers.dip_minutes / SESSION_MINUTES,
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
    log.info("Rows %s  n>=5 %s  net>0 %s  at/above breakeven %s", len(rows), len(live), len(green), len(be))

    def table(title: str, picked: Sequence[DipResult]) -> None:
        log.info("")
        log.info(title)
        if not picked:
            log.info("  (none)")
            return
        log.info(
            "  %s %s %s %s %s %s %s %s %s %s",
            f"{'stock':6}",
            f"{'combo':10}",
            f"{'dip':>10}",
            f"{'wait':>11}",
            f"{'exit':>9}",
            f"{'n':>4}",
            f"{'W/L/T':>9}",
            f"{'res%':>6}",
            f"{'be%':>6}",
            f"{'net$':>9}",
        )
        for row in picked:
            log.info(
                "  %s %s %s %s %s %4d %9s %6s %6.1f %9.2f",
                f"{row.symbol:6}",
                f"{row.combo:10}",
                f"{row.dip_pct:.0f}%/{row.dip_minutes // SESSION_MINUTES}d",
                f"{row.stable_range_pct:.1f}%/{row.stable_minutes}m",
                f"{row.take_profit_pct:.0f}/{row.stop_loss_pct:.0f}",
                row.trades,
                f"{row.wins}/{row.losses}/{row.timeouts}",
                f"{row.resolved_win_rate_pct:.1f}" if row.resolved_win_rate_pct is not None else "n/a",
                row.breakeven_win_rate_pct,
                row.net_usd,
            )

    table("Best 15 by net (any trade count)", sorted(rows, key=lambda r: r.net_usd, reverse=True)[:15])
    table("Best among n>=5", sorted(live, key=lambda r: r.net_usd, reverse=True)[:12])
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
