"""Classic geometric long grid for US stock 5-minute bars. Paper / backtest only.

Reference price is the first bar's close and is fixed for the whole window
(no re-centering). Buy levels i=1..levels sit at
``ref * (1 - step_pct/100)**i``. Each level spends ``notional / levels``
quote when its buy fills.

On a buy fill (bar low <= buy price) we open one lot and arm a sell at
``fill * (1 + step_pct/100)``. On a sell fill (bar high >= sell price) we
realize PnL, free that level, and re-arm the same buy. Same-bar rule:
buys first, then sells if the high also reaches the sell target.

Fees: ``ROUND_TRIP_FEE`` (0.60% = 0.3% + 0.3%) is the full round-trip; half
is charged on each fill's notional. Accounting:

- ``realized_usd``: sum of (exit_notional - entry_notional) for closed
  lots (grid sells and stop liquidations), before fees.
- ``fees_usd``: fees actually paid on fills that happened.
- ``unrealized_usd``: open lots marked to last close, minus an estimated
  exit half-fee (leftover fee honesty).
- ``net_usd`` = realized_usd + unrealized_usd - fees_usd.

Optional ``stop_pct``: if a bar's low falls that far below the lowest buy
level, liquidate all open lots at that bar's close and halt further
trading for the combo.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from stocks.bars import Bar

log = logging.getLogger("stocks")

ROUND_TRIP_FEE = 0.006  # 0.3% buy + 0.3% sell


@dataclass(frozen=True, slots=True)
class GridLevers:
    name: str
    step_pct: float
    levels: int
    stop_pct: float | None = None


# Wider steps than crypto 1m grids — stock 5m bars move less tick-to-tick
# but overnight gaps and session swings need room above the 0.60% fee.
DEFAULT_COMBOS: tuple[GridLevers, ...] = (
    GridLevers("grid_0.8pct_8lvl", step_pct=0.8, levels=8),
    GridLevers("grid_1.0pct_8lvl", step_pct=1.0, levels=8),
    GridLevers("grid_1.5pct_8lvl", step_pct=1.5, levels=8),
    GridLevers("grid_2.0pct_6lvl", step_pct=2.0, levels=6),
    GridLevers("grid_1.0pct_10lvl", step_pct=1.0, levels=10),
    GridLevers("grid_1.5pct_10lvl_stop10", step_pct=1.5, levels=10, stop_pct=10.0),
)


@dataclass(frozen=True, slots=True)
class GridResult:
    symbol: str
    combo: str
    step_pct: float
    levels: int
    stop_pct: float | None
    notional: float
    buy_fills: int
    sell_fills: int
    round_trips: int
    fees_usd: float
    realized_usd: float
    unrealized_usd: float
    net_usd: float
    max_inventory_lots: int
    max_drawdown_usd: float
    ending_cash: float
    ending_inventory_qty: float
    ending_mark: float
    stopped: bool


@dataclass(slots=True)
class _Lot:
    level: int
    buy_price: float
    qty: float
    entry_cost: float  # quote paid for the base (ex-fee)
    sell_price: float


def buy_level_price(ref: float, step_pct: float, level: int) -> float:
    """Geometric buy price for 1-indexed level ``level`` below ``ref``."""
    if level < 1:
        raise ValueError("level must be >= 1")
    factor = 1.0 - step_pct / 100.0
    return ref * (factor**level)


def sell_target(fill_price: float, step_pct: float) -> float:
    return fill_price * (1.0 + step_pct / 100.0)


def grid_buy_prices(ref: float, step_pct: float, levels: int) -> list[float]:
    return [buy_level_price(ref, step_pct, i) for i in range(1, levels + 1)]


def _empty_stats(notional: float) -> dict[str, float | int | bool]:
    return {
        "notional": notional,
        "buy_fills": 0,
        "sell_fills": 0,
        "round_trips": 0,
        "fees_usd": 0.0,
        "realized_usd": 0.0,
        "unrealized_usd": 0.0,
        "net_usd": 0.0,
        "max_inventory_lots": 0,
        "max_drawdown_usd": 0.0,
        "ending_cash": float(notional),
        "ending_inventory_qty": 0.0,
        "ending_mark": 0.0,
        "stopped": False,
    }


def simulate(
    bars: Sequence[Bar],
    levers: GridLevers,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> dict[str, float | int | bool]:
    """Run the geometric long grid over ``bars``.

    Returns a dict matching :class:`GridResult` metric fields (no symbol/combo).
    """
    if not bars:
        return _empty_stats(notional)
    if levers.levels < 1:
        raise ValueError("levels must be >= 1")
    if notional <= 0 or levers.step_pct <= 0:
        raise ValueError("notional and step_pct must be positive")

    half_fee = fee_rate / 2.0
    quote_per_level = notional / levers.levels
    ref = float(bars[0].close)
    if ref <= 0:
        return _empty_stats(notional)

    buy_prices = grid_buy_prices(ref, levers.step_pct, levers.levels)
    lowest_buy = buy_prices[-1]
    stop_trigger: float | None = None
    if levers.stop_pct is not None and levers.stop_pct > 0:
        stop_trigger = lowest_buy * (1.0 - levers.stop_pct / 100.0)

    # Per-level: None = buy armed; _Lot = holding with sell armed.
    slots: list[_Lot | None] = [None] * levers.levels
    cash = float(notional)
    fees_usd = 0.0
    realized_gross = 0.0
    buy_fills = 0
    sell_fills = 0
    round_trips = 0
    max_inventory_lots = 0
    peak_equity = float(notional)
    max_drawdown_usd = 0.0
    stopped = False

    def inventory_value(mark: float) -> float:
        return sum(lot.qty * mark for lot in slots if lot is not None)

    def equity_at(mark: float) -> float:
        return cash + inventory_value(mark)

    def update_dd(mark: float) -> None:
        nonlocal peak_equity, max_drawdown_usd
        eq = equity_at(mark)
        if eq > peak_equity:
            peak_equity = eq
        dd = peak_equity - eq
        if dd > max_drawdown_usd:
            max_drawdown_usd = dd

    for bar in bars:
        if stopped:
            break
        low = float(bar.low)
        high = float(bar.high)
        close = float(bar.close)

        # Buys first (closest-to-market / highest buy price first).
        for i, buy_px in enumerate(buy_prices):
            if slots[i] is not None:
                continue
            if low <= buy_px:
                qty = quote_per_level / buy_px
                fee = half_fee * quote_per_level
                cash -= quote_per_level + fee
                fees_usd += fee
                slots[i] = _Lot(
                    level=i + 1,
                    buy_price=buy_px,
                    qty=qty,
                    entry_cost=quote_per_level,
                    sell_price=sell_target(buy_px, levers.step_pct),
                )
                buy_fills += 1

        open_n = sum(1 for s in slots if s is not None)
        if open_n > max_inventory_lots:
            max_inventory_lots = open_n

        # Then sells if high reaches target (including same-bar after buy).
        for i, lot in enumerate(list(slots)):
            if lot is None:
                continue
            if high >= lot.sell_price:
                proceeds = lot.qty * lot.sell_price
                fee = half_fee * proceeds
                cash += proceeds - fee
                fees_usd += fee
                realized_gross += proceeds - lot.entry_cost
                sell_fills += 1
                round_trips += 1
                slots[i] = None

        # Optional stop: price fell stop_pct below lowest buy → liquidate at close.
        if stop_trigger is not None and low <= stop_trigger:
            for i, lot in enumerate(list(slots)):
                if lot is None:
                    continue
                proceeds = lot.qty * close
                fee = half_fee * proceeds
                cash += proceeds - fee
                fees_usd += fee
                realized_gross += proceeds - lot.entry_cost
                sell_fills += 1
                slots[i] = None
            stopped = True

        update_dd(close)

    last_close = float(bars[-1].close)
    open_now = [lot for lot in slots if lot is not None]
    ending_inventory_qty = sum(lot.qty for lot in open_now)
    unrealized_gross = sum(lot.qty * last_close - lot.entry_cost for lot in open_now)
    leftover_exit_fee = (
        half_fee * ending_inventory_qty * last_close if ending_inventory_qty else 0.0
    )
    unrealized_usd = unrealized_gross - leftover_exit_fee
    net_usd = realized_gross + unrealized_usd - fees_usd

    return {
        "notional": notional,
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "round_trips": round_trips,
        "fees_usd": fees_usd,
        "realized_usd": realized_gross,
        "unrealized_usd": unrealized_usd,
        "net_usd": net_usd,
        "max_inventory_lots": max_inventory_lots,
        "max_drawdown_usd": max_drawdown_usd,
        "ending_cash": cash,
        "ending_inventory_qty": ending_inventory_qty,
        "ending_mark": last_close,
        "stopped": stopped,
    }


def scan_symbol(
    symbol: str,
    bars: Sequence[Bar],
    combos: Sequence[GridLevers] = DEFAULT_COMBOS,
    *,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> list[GridResult]:
    rows: list[GridResult] = []
    for levers in combos:
        stats = simulate(bars, levers, notional=notional, fee_rate=fee_rate)
        row = GridResult(
            symbol=symbol,
            combo=levers.name,
            step_pct=levers.step_pct,
            levels=levers.levels,
            stop_pct=levers.stop_pct,
            **stats,  # type: ignore[arg-type]
        )
        rows.append(row)
        stop_txt = f" stop={levers.stop_pct:g}%" if levers.stop_pct else ""
        log.info(
            "%s  %s  step %.2f%% x%d%s  buys=%s sells=%s rt=%s  "
            "fees=$%.2f  real=$%.2f  unrl=$%.2f  net=$%.2f  maxLots=%s%s",
            symbol,
            levers.name,
            levers.step_pct,
            levers.levels,
            stop_txt,
            row.buy_fills,
            row.sell_fills,
            row.round_trips,
            row.fees_usd,
            row.realized_usd,
            row.unrealized_usd,
            row.net_usd,
            row.max_inventory_lots,
            "  STOPPED" if row.stopped else "",
        )
    return rows


def write_csv(path: Path, rows: Sequence[GridResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else list(GridResult.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def print_summary(rows: Sequence[GridResult]) -> None:
    if not rows:
        log.info("No results.")
        return
    live = [r for r in rows if r.round_trips >= 5]
    green = [r for r in live if r.net_usd > 0]
    log.info("")
    log.info(
        "Rows %s  with at least 5 round-trips %s  net>0 among those %s",
        len(rows),
        len(live),
        len(green),
    )

    def table(title: str, picked: Sequence[GridResult]) -> None:
        log.info("")
        log.info(title)
        if not picked:
            log.info("  (none)")
            return
        log.info(
            "  %s %s %s %s %s %s %s %s %s",
            f"{'stock':6}",
            f"{'combo':28}",
            f"{'step':>6}",
            f"{'lvl':>3}",
            f"{'rt':>5}",
            f"{'fees$':>8}",
            f"{'real$':>9}",
            f"{'unrl$':>9}",
            f"{'net$':>9}",
        )
        for row in picked:
            log.info(
                "  %s %s %6.2f %3d %5d %8.2f %9.2f %9.2f %9.2f",
                f"{row.symbol:6}",
                f"{row.combo:28}",
                row.step_pct,
                row.levels,
                row.round_trips,
                row.fees_usd,
                row.realized_usd,
                row.unrealized_usd,
                row.net_usd,
            )

    ranked = sorted(rows, key=lambda r: r.net_usd, reverse=True)
    table("Best 12 by net_usd", ranked[:12])
    table("Best among rt>=5", sorted(live, key=lambda r: r.net_usd, reverse=True)[:10])
    table("Worst 8 by net_usd", sorted(rows, key=lambda r: r.net_usd)[:8])
    for symbol in sorted({r.symbol for r in rows}):
        coin = [r for r in rows if r.symbol == symbol]
        best = max(coin, key=lambda r: r.net_usd)
        log.info(
            "Best %s: %s  rt=%s  net=$%.2f%s",
            symbol,
            best.combo,
            best.round_trips,
            best.net_usd,
            "  STOPPED" if best.stopped else "",
        )
