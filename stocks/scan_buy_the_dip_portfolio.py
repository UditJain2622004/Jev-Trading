"""Portfolio-wise buy-the-dip combo sweep.

Mode A (all-in): start equity, at most 1 open trade, each entry uses 100% of cash.
Mode B (3-slot): start equity, up to 3 concurrent opens; size = cash / slots_left
  (0 open → 1/3 cash, 1 open → 1/2 cash, 2 open → all remaining cash).

Same combo grid as scan_buy_the_dip_combo.py (dip 4/5%, 4 sessions, waits 30..300,
±1.5%, TP5/SL4, 2 calendar day timeout). Fees: ROUND_TRIP_FEE (0.60% RT).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stocks.bars import DEFAULT_CACHE_DIR, DEFAULT_SYMBOLS, download_symbol
from stocks.dip import (
    BAR_MINUTES,
    ROUND_TRIP_FEE,
    SESSION_MINUTES,
    DipLevers,
    _exit_trade_trail,
    find_entries,
    find_entries_confirm,
)
import stocks.dip as dip

log = logging.getLogger("stocks")

TIMEOUT_CALENDAR_MS = 2 * 24 * 60 * 60 * 1000
MIN_SIZE = 1.0  # skip entry if sized notional below this


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
                    timeout_minutes=2 * 24 * 60,
                )
            )

    return tuple(out)


# Best single-stock combo by net_usd from combo_d4d5_4sess_2day.csv (baseline entry).
BEST_PER_STOCK_COMBO_NAMES: dict[str, str] = {
    "AMD": "d5_w120",
    "ARM": "d5_w120",
    "AVGO": "d4_w60",
    "DELL": "d5_w60",
    "HPE": "d5_w30",
    "IBM": "d4_w240",
    "INTC": "d5_w300",
    "MRVL": "d5_w180",
    "MU": "d5_w30",
    "ORCL": "d4_w30",
    "QCOM": "d5_w30",
    "SNDK": "d4_w180",
    "TXN": "d5_w60",
}


def combo_index(combos: tuple[DipLevers, ...] | None = None) -> dict[str, DipLevers]:
    if combos is None:
        combos = build_combos()
    return {c.name: c for c in combos}


def levers_for_combo_name(name: str, combos: tuple[DipLevers, ...] | None = None) -> DipLevers:
    by_name = combo_index(combos)
    if name not in by_name:
        raise KeyError(f"Unknown combo name: {name!r}; known={sorted(by_name)}")
    return by_name[name]


def build_best_per_stock_levers(
    symbols: list[str] | None = None,
    *,
    name_map: dict[str, str] | None = None,
    combos: tuple[DipLevers, ...] | None = None,
) -> dict[str, DipLevers]:
    """Map each symbol to its DipLevers from BEST_PER_STOCK_COMBO_NAMES (or override)."""
    names = name_map if name_map is not None else BEST_PER_STOCK_COMBO_NAMES
    by_name = combo_index(combos)
    syms = symbols if symbols is not None else list(names.keys())
    out: dict[str, DipLevers] = {}
    for sym in syms:
        key = sym.upper()
        if key not in names:
            raise KeyError(f"No best combo mapped for symbol {key!r}")
        cname = names[key]
        if cname not in by_name:
            raise KeyError(f"Unknown combo {cname!r} for {key}")
        out[key] = by_name[cname]
    return out


def dump_perstock_levers_json(
    levers_by_symbol: dict[str, DipLevers],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        sym: {
            "combo": lev.name,
            "dip_pct": lev.dip_pct,
            "dip_minutes": lev.dip_minutes,
            "stable_range_pct": lev.stable_range_pct,
            "stable_minutes": lev.stable_minutes,
            "take_profit_pct": lev.take_profit_pct,
            "stop_loss_pct": lev.stop_loss_pct,
            "timeout_minutes": lev.timeout_minutes,
        }
        for sym, lev in sorted(levers_by_symbol.items())
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

def _exit_trade_calendar(bars, entry_index: int, levers: DipLevers, *, notional: float, fee_rate: float):
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
            ret = -levers.stop_loss_pct / 100.0 - fee_rate
            return "loss", ret * notional, hold, True, ret, bars[j].open_time
        if hit_sl:
            ret = -levers.stop_loss_pct / 100.0 - fee_rate
            return "loss", ret * notional, hold, False, ret, bars[j].open_time
        if hit_tp:
            ret = levers.take_profit_pct / 100.0 - fee_rate
            return "win", ret * notional, hold, False, ret, bars[j].open_time
        if timed_out:
            move = (bars[j].close / entry) - 1.0
            ret = move - fee_rate
            return "timeout", ret * notional, hold, False, ret, bars[j].open_time
        j += 1
    hold = last - entry_index
    move = (bars[last].close / entry) - 1.0
    ret = move - fee_rate
    return "timeout", ret * notional, hold, False, ret, bars[last].open_time


def _exit_trade_calendar_trail(bars, entry_index: int, levers: DipLevers, *, notional: float, fee_rate: float):
    """Trailing soft-TP exit with 2-calendar-day wall-clock timeout."""
    return _exit_trade_trail(
        bars,
        entry_index,
        levers,
        notional=notional,
        fee_rate=fee_rate,
        timeout_calendar_ms=TIMEOUT_CALENDAR_MS,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    entry_index: int
    entry_time: int
    entry_price: float


@dataclass(slots=True)
class OpenPosition:
    symbol: str
    combo: str
    entry_time: int
    exit_time: int
    outcome: str
    ret: float
    pnl: float
    notional: float
    cash_before: float
    open_count_at_entry: int
    equity_before: float
    hold_minutes: float


@dataclass(frozen=True, slots=True)
class TakenTrade:
    symbol: str
    combo: str
    entry_time: int
    exit_time: int
    outcome: str
    ret: float
    pnl: float
    notional: float
    cash_before: float
    open_count_at_entry: int
    equity_before: float
    equity_after: float
    hold_minutes: float


def collect_candidates(
    symbol: str,
    bars,
    levers: DipLevers,
    *,
    entry_mode: str = "baseline",
) -> list[Candidate]:
    if entry_mode == "confirm_trail":
        raw, _ = find_entries_confirm(bars, levers)
    else:
        raw, _ = find_entries(bars, levers)
    out: list[Candidate] = []
    for idx in raw:
        if idx >= len(bars) - 1:
            continue
        out.append(
            Candidate(
                symbol=symbol,
                entry_index=idx,
                entry_time=bars[idx].open_time,
                entry_price=bars[idx].close,
            )
        )
    return out


def _equity(cash: float, opens: list[OpenPosition]) -> float:
    return cash + sum(o.notional for o in opens)


def _close_due_ordered(
    opens: list[OpenPosition],
    cash: float,
    as_of: int,
    taken: list[TakenTrade],
) -> float:
    """Close all positions with exit_time <= as_of, earliest exit first (then symbol)."""
    due = [o for o in opens if o.exit_time <= as_of]
    keep = [o for o in opens if o.exit_time > as_of]
    due.sort(key=lambda o: (o.exit_time, o.symbol))
    for pos in due:
        cash = cash + pos.notional + pos.pnl
        equity_after = cash + sum(o.notional for o in keep)
        taken.append(
            TakenTrade(
                symbol=pos.symbol,
                combo=pos.combo,
                entry_time=pos.entry_time,
                exit_time=pos.exit_time,
                outcome=pos.outcome,
                ret=pos.ret,
                pnl=pos.pnl,
                notional=pos.notional,
                cash_before=pos.cash_before,
                open_count_at_entry=pos.open_count_at_entry,
                equity_before=pos.equity_before,
                equity_after=equity_after,
                hold_minutes=pos.hold_minutes,
            )
        )
    opens[:] = keep
    return cash


def _pick_exit_fn(entry_mode: str):
    if entry_mode == "confirm_trail":
        return _exit_trade_calendar_trail
    return _exit_trade_calendar


def simulate_portfolio(
    bars_by_symbol: dict[str, list],
    levers: DipLevers,
    *,
    start_equity: float = 1000.0,
    max_positions: int = 1,
    fee_rate: float = ROUND_TRIP_FEE,
    min_size: float = MIN_SIZE,
    entry_mode: str = "baseline",
) -> tuple[list[TakenTrade], dict]:
    if max_positions < 1:
        raise ValueError("max_positions must be >= 1")

    candidates: list[Candidate] = []
    for symbol, bars in bars_by_symbol.items():
        candidates.extend(collect_candidates(symbol, bars, levers, entry_mode=entry_mode))
    # Same-ms ties: alphabetical symbol (stable, deterministic)
    candidates.sort(key=lambda c: (c.entry_time, c.symbol))

    exit_fn = _pick_exit_fn(entry_mode)

    # Mode A (1 slot): preserve original free_at = exit_time + 1 semantics.
    if max_positions == 1:
        return _simulate_allin(
            bars_by_symbol, levers, candidates,
            start_equity=start_equity, fee_rate=fee_rate, exit_fn=exit_fn,
        )

    cash = start_equity
    opens: list[OpenPosition] = []
    taken: list[TakenTrade] = []
    skipped_busy = 0
    skipped_cash = 0

    for cand in candidates:
        # Multi-slot: free any position with exit_time <= candidate.entry_time
        cash = _close_due_ordered(opens, cash, cand.entry_time, taken)

        if len(opens) >= max_positions:
            skipped_busy += 1
            continue

        slots_left = max_positions - len(opens)
        size = cash / slots_left
        if size < min_size or cash < min_size:
            skipped_cash += 1
            continue

        bars = bars_by_symbol[cand.symbol]
        outcome, pnl, hold, _both, ret, exit_time = exit_fn(
            bars, cand.entry_index, levers, notional=size, fee_rate=fee_rate
        )
        equity_before = _equity(cash, opens)
        open_count = len(opens)
        cash_before = cash
        cash -= size
        opens.append(
            OpenPosition(
                symbol=cand.symbol,
                combo=levers.name,
                entry_time=cand.entry_time,
                exit_time=exit_time,
                outcome=outcome,
                ret=ret,
                pnl=pnl,
                notional=size,
                cash_before=cash_before,
                open_count_at_entry=open_count,
                equity_before=equity_before,
                hold_minutes=hold * BAR_MINUTES,
            )
        )

    if opens:
        last_exit = max(o.exit_time for o in opens)
        cash = _close_due_ordered(opens, cash, last_exit, taken)

    end_equity = cash
    wins = sum(1 for t in taken if t.outcome == "win")
    losses = sum(1 for t in taken if t.outcome == "loss")
    timeouts = sum(1 for t in taken if t.outcome == "timeout")
    summary = {
        "mode": f"{max_positions}slot" if max_positions > 1 else "allin",
        "max_positions": max_positions,
        "combo": levers.name,
        "candidates": len(candidates),
        "skipped_busy": skipped_busy,
        "skipped_cash": skipped_cash,
        "trades": len(taken),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "net_usd": end_equity - start_equity,
        "return_pct": 100.0 * (end_equity / start_equity - 1.0) if start_equity else 0.0,
        "max_equity": max((t.equity_after for t in taken), default=start_equity),
        "min_equity": min((t.equity_after for t in taken), default=start_equity),
        "worst_trade_ret_pct": 100.0 * min((t.ret for t in taken), default=0.0),
        "best_trade_ret_pct": 100.0 * max((t.ret for t in taken), default=0.0),
    }
    return taken, summary


def _simulate_allin(
    bars_by_symbol: dict[str, list],
    levers: DipLevers,
    candidates: list[Candidate],
    *,
    start_equity: float,
    fee_rate: float,
    exit_fn=_exit_trade_calendar,
) -> tuple[list[TakenTrade], dict]:
    """Original 1-trade-at-a-time compound path (free_at = exit_time + 1)."""
    equity = start_equity
    free_at = 0
    taken: list[TakenTrade] = []
    skipped_busy = 0

    for cand in candidates:
        if cand.entry_time < free_at:
            skipped_busy += 1
            continue
        if equity <= 0:
            break
        bars = bars_by_symbol[cand.symbol]
        outcome, pnl, hold, _both, ret, exit_time = exit_fn(
            bars, cand.entry_index, levers, notional=equity, fee_rate=fee_rate
        )
        before = equity
        equity = equity + pnl
        taken.append(
            TakenTrade(
                symbol=cand.symbol,
                combo=levers.name,
                entry_time=cand.entry_time,
                exit_time=exit_time,
                outcome=outcome,
                ret=ret,
                pnl=pnl,
                notional=before,
                cash_before=before,
                open_count_at_entry=0,
                equity_before=before,
                equity_after=equity,
                hold_minutes=hold * BAR_MINUTES,
            )
        )
        free_at = exit_time + 1

    wins = sum(1 for t in taken if t.outcome == "win")
    losses = sum(1 for t in taken if t.outcome == "loss")
    timeouts = sum(1 for t in taken if t.outcome == "timeout")
    summary = {
        "mode": "allin",
        "max_positions": 1,
        "combo": levers.name,
        "candidates": len(candidates),
        "skipped_busy": skipped_busy,
        "skipped_cash": 0,
        "trades": len(taken),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "start_equity": start_equity,
        "end_equity": equity,
        "net_usd": equity - start_equity,
        "return_pct": 100.0 * (equity / start_equity - 1.0) if start_equity else 0.0,
        "max_equity": max((t.equity_after for t in taken), default=start_equity),
        "min_equity": min((t.equity_after for t in taken), default=start_equity),
        "worst_trade_ret_pct": 100.0 * min((t.ret for t in taken), default=0.0),
        "best_trade_ret_pct": 100.0 * max((t.ret for t in taken), default=0.0),
    }
    return taken, summary



def _simulate_allin_per_stock(
    bars_by_symbol: dict[str, list],
    levers_by_symbol: dict[str, DipLevers],
    candidates: list[Candidate],
    *,
    start_equity: float,
    fee_rate: float,
    exit_fn=_exit_trade_calendar,
) -> tuple[list[TakenTrade], dict]:
    """1-slot all-in path using each symbol's own DipLevers."""
    equity = start_equity
    free_at = 0
    taken: list[TakenTrade] = []
    skipped_busy = 0

    for cand in candidates:
        if cand.entry_time < free_at:
            skipped_busy += 1
            continue
        if equity <= 0:
            break
        levers = levers_by_symbol[cand.symbol]
        bars = bars_by_symbol[cand.symbol]
        outcome, pnl, hold, _both, ret, exit_time = exit_fn(
            bars, cand.entry_index, levers, notional=equity, fee_rate=fee_rate
        )
        before = equity
        equity = equity + pnl
        taken.append(
            TakenTrade(
                symbol=cand.symbol,
                combo=levers.name,
                entry_time=cand.entry_time,
                exit_time=exit_time,
                outcome=outcome,
                ret=ret,
                pnl=pnl,
                notional=before,
                cash_before=before,
                open_count_at_entry=0,
                equity_before=before,
                equity_after=equity,
                hold_minutes=hold * BAR_MINUTES,
            )
        )
        free_at = exit_time + 1

    wins = sum(1 for t in taken if t.outcome == "win")
    losses = sum(1 for t in taken if t.outcome == "loss")
    timeouts = sum(1 for t in taken if t.outcome == "timeout")
    summary = {
        "mode": "allin",
        "max_positions": 1,
        "combo": "perstock_best",
        "candidates": len(candidates),
        "skipped_busy": skipped_busy,
        "skipped_cash": 0,
        "trades": len(taken),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "start_equity": start_equity,
        "end_equity": equity,
        "net_usd": equity - start_equity,
        "return_pct": 100.0 * (equity / start_equity - 1.0) if start_equity else 0.0,
        "max_equity": max((t.equity_after for t in taken), default=start_equity),
        "min_equity": min((t.equity_after for t in taken), default=start_equity),
        "worst_trade_ret_pct": 100.0 * min((t.ret for t in taken), default=0.0),
        "best_trade_ret_pct": 100.0 * max((t.ret for t in taken), default=0.0),
    }
    return taken, summary


def simulate_portfolio_per_stock(
    bars_by_symbol: dict[str, list],
    levers_by_symbol: dict[str, DipLevers],
    *,
    start_equity: float = 1000.0,
    max_positions: int = 1,
    fee_rate: float = ROUND_TRIP_FEE,
    min_size: float = MIN_SIZE,
    entry_mode: str = "baseline",
) -> tuple[list[TakenTrade], dict]:
    """Portfolio sim where each symbol uses its own DipLevers for entry + exit."""
    if max_positions < 1:
        raise ValueError("max_positions must be >= 1")
    missing = [s for s in bars_by_symbol if s not in levers_by_symbol]
    if missing:
        raise KeyError(f"levers_by_symbol missing symbols: {missing}")

    candidates: list[Candidate] = []
    for symbol, bars in bars_by_symbol.items():
        candidates.extend(
            collect_candidates(
                symbol, bars, levers_by_symbol[symbol], entry_mode=entry_mode
            )
        )
    candidates.sort(key=lambda c: (c.entry_time, c.symbol))

    exit_fn = _pick_exit_fn(entry_mode)

    if max_positions == 1:
        return _simulate_allin_per_stock(
            bars_by_symbol,
            levers_by_symbol,
            candidates,
            start_equity=start_equity,
            fee_rate=fee_rate,
            exit_fn=exit_fn,
        )

    cash = start_equity
    opens: list[OpenPosition] = []
    taken: list[TakenTrade] = []
    skipped_busy = 0
    skipped_cash = 0

    for cand in candidates:
        cash = _close_due_ordered(opens, cash, cand.entry_time, taken)

        if len(opens) >= max_positions:
            skipped_busy += 1
            continue

        slots_left = max_positions - len(opens)
        size = cash / slots_left
        if size < min_size or cash < min_size:
            skipped_cash += 1
            continue

        levers = levers_by_symbol[cand.symbol]
        bars = bars_by_symbol[cand.symbol]
        outcome, pnl, hold, _both, ret, exit_time = exit_fn(
            bars, cand.entry_index, levers, notional=size, fee_rate=fee_rate
        )
        equity_before = _equity(cash, opens)
        open_count = len(opens)
        cash_before = cash
        cash -= size
        opens.append(
            OpenPosition(
                symbol=cand.symbol,
                combo=levers.name,
                entry_time=cand.entry_time,
                exit_time=exit_time,
                outcome=outcome,
                ret=ret,
                pnl=pnl,
                notional=size,
                cash_before=cash_before,
                open_count_at_entry=open_count,
                equity_before=equity_before,
                hold_minutes=hold * BAR_MINUTES,
            )
        )

    if opens:
        last_exit = max(o.exit_time for o in opens)
        cash = _close_due_ordered(opens, cash, last_exit, taken)

    end_equity = cash
    wins = sum(1 for t in taken if t.outcome == "win")
    losses = sum(1 for t in taken if t.outcome == "loss")
    timeouts = sum(1 for t in taken if t.outcome == "timeout")
    summary = {
        "mode": f"{max_positions}slot" if max_positions > 1 else "allin",
        "max_positions": max_positions,
        "combo": "perstock_best",
        "candidates": len(candidates),
        "skipped_busy": skipped_busy,
        "skipped_cash": skipped_cash,
        "trades": len(taken),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "net_usd": end_equity - start_equity,
        "return_pct": 100.0 * (end_equity / start_equity - 1.0) if start_equity else 0.0,
        "max_equity": max((t.equity_after for t in taken), default=start_equity),
        "min_equity": min((t.equity_after for t in taken), default=start_equity),
        "worst_trade_ret_pct": 100.0 * min((t.ret for t in taken), default=0.0),
        "best_trade_ret_pct": 100.0 * max((t.ret for t in taken), default=0.0),
    }
    return taken, summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Portfolio-wise buy-the-dip combo sweep")
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--period", default="60d")
    p.add_argument("--interval", default="5m")
    p.add_argument(
        "--mode",
        choices=("allin", "3slot", "both"),
        default="both",
        help="allin=$1000/1pos; 3slot=$1500/3pos; both=run both (default)",
    )
    p.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help="Override max concurrent positions (implies single run, not --mode both)",
    )
    p.add_argument(
        "--start-equity",
        type=float,
        default=None,
        help="Override starting cash/equity",
    )
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument(
        "--out",
        default=None,
        help="Summary CSV path (default depends on mode)",
    )
    p.add_argument(
        "--trades-out",
        default=None,
        help="Trades CSV path (default depends on mode)",
    )
    p.add_argument("--force-download", action="store_true")
    p.add_argument(
        "--entry",
        choices=("baseline", "confirm_trail"),
        default="baseline",
        help="baseline=stable wait + first-touch TP; confirm_trail=bounce confirm + soft TP trail",
    )
    p.add_argument(
        "--per-stock",
        action="store_true",
        help="Use best combo per symbol (BEST_PER_STOCK_COMBO_NAMES); runs Mode A $1000 + Mode B $1500",
    )
    return p.parse_args()


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
    for path in (log_dir / f"portfolio_{stamp}.log", log_dir / "portfolio_scan.log"):
        fh = logging.FileHandler(path, mode="w", encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)
    root.addHandler(stream)
    root.propagate = False


def _default_paths(
    mode_label: str,
    start_equity: float,
    max_positions: int,
    *,
    entry_mode: str = "baseline",
) -> tuple[str, str]:
    suffix = "confirm_trail_d4d5_4sess_2day" if entry_mode == "confirm_trail" else "d4d5_4sess_2day"
    if mode_label == "allin" or max_positions == 1:
        tag = f"portfolio_allin_{int(start_equity)}_{suffix}"
    elif mode_label == "3slot" or max_positions == 3:
        tag = f"portfolio_3slot_{int(start_equity)}_{suffix}"
    else:
        tag = f"portfolio_{max_positions}slot_{int(start_equity)}_{suffix}"
    base = f"data/stocks/buy_the_dip/{tag}"
    return f"{base}.csv", f"{base}_trades.csv"



def _perstock_paths(
    mode_label: str,
    start_equity: float,
    *,
    entry_mode: str = "baseline",
) -> tuple[str, str]:
    suffix = (
        "confirm_trail_perstock_best_d4d5_4sess_2day"
        if entry_mode == "confirm_trail"
        else "perstock_best_d4d5_4sess_2day"
    )
    if mode_label == "allin":
        tag = f"portfolio_allin_{int(start_equity)}_{suffix}"
    else:
        tag = f"portfolio_3slot_{int(start_equity)}_{suffix}"
    base = f"data/stocks/buy_the_dip/{tag}"
    return f"{base}.csv", f"{base}_trades.csv"


def _write_summary_and_trades(
    summaries: list[dict],
    all_trades: list[TakenTrade],
    out_path: str,
    trades_path: str,
) -> None:
    out = ROOT / out_path
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(summaries[0].keys()) if summaries else []
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in summaries:
            w.writerow(row)

    trades_out = ROOT / trades_path
    tfields = [
        "combo",
        "symbol",
        "entry_time",
        "exit_time",
        "outcome",
        "ret",
        "pnl",
        "notional",
        "cash_before",
        "open_count_at_entry",
        "equity_before",
        "equity_after",
        "hold_minutes",
    ]
    with trades_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tfields)
        w.writeheader()
        for t in all_trades:
            w.writerow(
                {
                    "combo": t.combo,
                    "symbol": t.symbol,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "outcome": t.outcome,
                    "ret": t.ret,
                    "pnl": t.pnl,
                    "notional": t.notional,
                    "cash_before": t.cash_before,
                    "open_count_at_entry": t.open_count_at_entry,
                    "equity_before": t.equity_before,
                    "equity_after": t.equity_after,
                    "hold_minutes": t.hold_minutes,
                }
            )
    log.info("Wrote %s", out)
    log.info("Wrote %s", trades_out)


def run_per_stock_mode(
    bars_by: dict[str, list],
    levers_by_symbol: dict[str, DipLevers],
    *,
    start_equity: float,
    max_positions: int,
    mode_label: str,
    out_path: str,
    trades_path: str,
    entry_mode: str = "baseline",
) -> list[dict]:
    log.info("")
    log.info(
        "=== Per-stock Mode %s [%s]: $%.0f start, max %d open ===",
        mode_label,
        entry_mode,
        start_equity,
        max_positions,
    )
    for sym in sorted(levers_by_symbol):
        log.info("  %s → %s", sym, levers_by_symbol[sym].name)

    taken, summary = simulate_portfolio_per_stock(
        bars_by,
        levers_by_symbol,
        start_equity=start_equity,
        max_positions=max_positions,
        fee_rate=ROUND_TRIP_FEE,
        entry_mode=entry_mode,
    )
    summaries = [summary]
    log.info(
        "%-10s  cand=%4d skip_busy=%4d skip_cash=%3d n=%3d W/L/T=%2d/%2d/%2d  "
        "$%.0f → $%.2f  net=$%+.2f  (%+.1f%%)",
        summary["combo"],
        summary["candidates"],
        summary["skipped_busy"],
        summary["skipped_cash"],
        summary["trades"],
        summary["wins"],
        summary["losses"],
        summary["timeouts"],
        summary["start_equity"],
        summary["end_equity"],
        summary["net_usd"],
        summary["return_pct"],
    )
    _write_summary_and_trades(summaries, taken, out_path, trades_path)

    # PnL by symbol for quick review
    by_sym: dict[str, float] = {}
    for t in taken:
        by_sym[t.symbol] = by_sym.get(t.symbol, 0.0) + t.pnl
    log.info("PnL by symbol (%s):", mode_label)
    for sym, pnl in sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True):
        n = sum(1 for t in taken if t.symbol == sym)
        combo = levers_by_symbol[sym].name
        log.info("  %-5s %-10s n=%3d pnl=$%+.2f", sym, combo, n, pnl)
    return summaries


def run_mode(
    bars_by: dict[str, list],
    combos: tuple[DipLevers, ...],
    *,
    start_equity: float,
    max_positions: int,
    mode_label: str,
    out_path: str,
    trades_path: str,
    entry_mode: str = "baseline",
) -> list[dict]:
    log.info("")
    log.info(
        "=== Mode %s [%s]: $%.0f start, max %d open, size=cash/(slots_left) ===",
        mode_label,
        entry_mode,
        start_equity,
        max_positions,
    )
    summaries: list[dict] = []
    all_trades: list[TakenTrade] = []
    for levers in combos:
        taken, summary = simulate_portfolio(
            bars_by,
            levers,
            start_equity=start_equity,
            max_positions=max_positions,
            fee_rate=ROUND_TRIP_FEE,
            entry_mode=entry_mode,
        )
        summaries.append(summary)
        all_trades.extend(taken)
        log.info(
            "%-10s  cand=%4d skip_busy=%4d skip_cash=%3d n=%3d W/L/T=%2d/%2d/%2d  "
            "$%.0f → $%.2f  net=$%+.2f  (%+.1f%%)",
            summary["combo"],
            summary["candidates"],
            summary["skipped_busy"],
            summary["skipped_cash"],
            summary["trades"],
            summary["wins"],
            summary["losses"],
            summary["timeouts"],
            summary["start_equity"],
            summary["end_equity"],
            summary["net_usd"],
            summary["return_pct"],
        )

    out = ROOT / out_path
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(summaries[0].keys()) if summaries else []
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in sorted(summaries, key=lambda r: r["end_equity"], reverse=True):
            w.writerow(row)

    trades_out = ROOT / trades_path
    tfields = [
        "combo",
        "symbol",
        "entry_time",
        "exit_time",
        "outcome",
        "ret",
        "pnl",
        "notional",
        "cash_before",
        "open_count_at_entry",
        "equity_before",
        "equity_after",
        "hold_minutes",
    ]
    with trades_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tfields)
        w.writeheader()
        for t in all_trades:
            w.writerow(
                {
                    "combo": t.combo,
                    "symbol": t.symbol,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "outcome": t.outcome,
                    "ret": t.ret,
                    "pnl": t.pnl,
                    "notional": t.notional,
                    "cash_before": t.cash_before,
                    "open_count_at_entry": t.open_count_at_entry,
                    "equity_before": t.equity_before,
                    "equity_after": t.equity_after,
                    "hold_minutes": t.hold_minutes,
                }
            )

    log.info("=== Ranked by end equity (%s) ===", mode_label)
    log.info(
        "  %-10s %5s %5s %4s %4s %4s %10s %10s %8s",
        "combo",
        "n",
        "skip",
        "W",
        "L",
        "T",
        "end$",
        "net$",
        "ret%",
    )
    for s in sorted(summaries, key=lambda r: r["end_equity"], reverse=True):
        log.info(
            "  %-10s %5d %5d %4d %4d %4d %10.2f %10.2f %7.1f%%",
            s["combo"],
            s["trades"],
            s["skipped_busy"],
            s["wins"],
            s["losses"],
            s["timeouts"],
            s["end_equity"],
            s["net_usd"],
            s["return_pct"],
        )
    log.info("Wrote %s", out)
    log.info("Wrote %s", trades_out)
    return summaries


def main() -> None:
    args = parse_args()
    setup_logging()
    combos = build_combos()
    dip._exit_trade = _exit_trade_calendar  # unused here but keep parity

    symbols = [s.upper() for s in args.symbols]
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    log.info(
        "Combos: dip 4/5%%, 4sess, waits %s, ±1.5%%, TP5/SL4, timeout 2 calendar days",
        [30, 60, 120, 180, 240, 300],
    )
    log.info("Entry/exit mode: %s", args.entry)
    log.info("Fees 0.60%% RT. Symbols %s: %s", len(symbols), ", ".join(symbols))

    bars_by: dict[str, list] = {}
    for n, symbol in enumerate(symbols, start=1):
        path, bars = download_symbol(
            symbol,
            period=args.period,
            interval=args.interval,
            cache_dir=cache_dir,
            force=args.force_download,
        )
        bars_by[symbol] = bars
        log.info("[%s/%s] %s: %s bars from %s", n, len(symbols), symbol, f"{len(bars):,}", path.name)

    started = time.perf_counter()

    if args.per_stock:
        levers_by = build_best_per_stock_levers(symbols, combos=combos)
        map_path = ROOT / "data/stocks/buy_the_dip/perstock_best_levers.json"
        dump_perstock_levers_json(levers_by, map_path)
        log.info("Per-stock levers dumped to %s", map_path)
        runs = [
            ("allin", args.start_equity if args.start_equity is not None else 1000.0, 1),
            ("3slot", args.start_equity if args.start_equity is not None else 1500.0, 3),
        ]
        if args.mode == "allin":
            runs = [("allin", args.start_equity if args.start_equity is not None else 1000.0, 1)]
        elif args.mode == "3slot":
            runs = [("3slot", args.start_equity if args.start_equity is not None else 1500.0, 3)]
        for label, eq, mp in runs:
            out_p, trades_p = _perstock_paths(label, eq, entry_mode=args.entry)
            if args.out and len(runs) == 1:
                out_p = args.out
                trades_p = args.trades_out or args.out.replace(".csv", "_trades.csv")
            run_per_stock_mode(
                bars_by,
                levers_by,
                start_equity=eq,
                max_positions=mp,
                mode_label=label,
                out_path=out_p,
                trades_path=trades_p,
                entry_mode=args.entry,
            )
        log.info("Finished per-stock in %.1fs", time.perf_counter() - started)
        return

    # Resolve which modes to run
    runs: list[tuple[str, float, int]] = []
    if args.max_positions is not None or (args.mode != "both" and args.start_equity is not None and args.mode not in ("allin", "3slot", "both")):
        # Custom single run via flags
        pass

    if args.mode == "both" and args.max_positions is None:
        runs = [
            ("allin", args.start_equity if args.start_equity is not None else 1000.0, 1),
            ("3slot", args.start_equity if args.start_equity is not None else 1500.0, 3),
        ]
    elif args.mode == "allin" or (args.max_positions == 1 and args.mode != "3slot"):
        mp = args.max_positions if args.max_positions is not None else 1
        eq = args.start_equity if args.start_equity is not None else 1000.0
        runs = [("allin", eq, mp)]
    elif args.mode == "3slot":
        mp = args.max_positions if args.max_positions is not None else 3
        eq = args.start_equity if args.start_equity is not None else 1500.0
        runs = [("3slot", eq, mp)]
    else:
        mp = args.max_positions if args.max_positions is not None else 1
        eq = args.start_equity if args.start_equity is not None else 1000.0
        label = "allin" if mp == 1 else f"{mp}slot"
        runs = [(label, eq, mp)]

    all_summaries: list[dict] = []
    for i, (label, eq, mp) in enumerate(runs):
        if args.out and len(runs) == 1:
            out_p, trades_p = args.out, (args.trades_out or args.out.replace(".csv", "_trades.csv"))
        elif args.out and len(runs) > 1:
            # Ignore single --out when running both; use defaults per mode
            out_p, trades_p = _default_paths(label, eq, mp, entry_mode=args.entry)
        else:
            out_p, trades_p = _default_paths(label, eq, mp, entry_mode=args.entry)
            if args.trades_out and len(runs) == 1:
                trades_p = args.trades_out
        all_summaries.extend(
            run_mode(
                bars_by,
                combos,
                start_equity=eq,
                max_positions=mp,
                mode_label=label,
                out_path=out_p,
                trades_path=trades_p,
                entry_mode=args.entry,
            )
        )

    # Also refresh legacy filename for Mode A so old paths stay valid (baseline only)
    if args.entry == "baseline" and any(label == "allin" for label, _, _ in runs):
        legacy = ROOT / "data/stocks/buy_the_dip/portfolio_d4d5_4sess_2day.csv"
        src = ROOT / _default_paths("allin", 1000.0, 1)[0]
        if src.exists() and any(eq == 1000.0 and mp == 1 for _, eq, mp in runs):
            import shutil

            shutil.copy2(src, legacy)
            # trades legacy
            legacy_t = ROOT / "data/stocks/buy_the_dip/portfolio_trades_d4d5_4sess_2day.csv"
            src_t = ROOT / _default_paths("allin", 1000.0, 1)[1]
            if src_t.exists():
                # legacy trades had fewer columns; write compatible subset
                with src_t.open(newline="", encoding="utf-8") as fin:
                    rows = list(csv.DictReader(fin))
                legacy_fields = [
                    "combo",
                    "symbol",
                    "entry_time",
                    "exit_time",
                    "outcome",
                    "ret",
                    "pnl",
                    "equity_before",
                    "equity_after",
                    "hold_minutes",
                ]
                with legacy_t.open("w", newline="", encoding="utf-8") as fout:
                    w = csv.DictWriter(fout, fieldnames=legacy_fields, extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow(r)
            log.info("Also wrote legacy %s", legacy)

    log.info("Finished in %.1fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
