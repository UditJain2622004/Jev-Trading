from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from jev_trading.binance.market import parse_kline
from jev_trading.binance.tp_sl_scan import ROUND_TRIP_FEE
from jev_trading.binance.types import Kline
from jev_trading.sol_jev.snapshots import HISTORY_BARS, build_snapshot, compact_example


@dataclass(slots=True)
class ReplayTrade:
    entry_index: int
    exit_index: int
    entry_ms: int
    exit_ms: int
    outcome: str  # win | loss | timeout
    both_hit: bool
    pnl_usd: float
    hold_minutes: int
    snapshot: dict[str, object]
    example: dict[str, object]


def load_kline_file(path: Path, symbol: str, interval: str = "1m") -> list[Kline]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [parse_kline(symbol, interval, row) for row in raw]


def latest_matching_caches(cache_dir: Path, symbol: str, benchmark: str = "BTCUSDT") -> tuple[Path, Path]:
    matches = list(cache_dir.glob(f"{symbol}_1m_*.json"))
    if not matches:
        raise FileNotFoundError(f"No cached 1m klines for {symbol} in {cache_dir}")

    def end_ts(path: Path) -> int:
        return int(path.stem.split("_")[-1])

    sol_path = max(matches, key=end_ts)
    start, end = sol_path.stem.split("_")[-2:]
    btc_path = cache_dir / f"{benchmark}_1m_{start}_{end}.json"
    if not btc_path.exists():
        btc_hits = list(cache_dir.glob(f"{benchmark}_1m_*.json"))
        if not btc_hits:
            raise FileNotFoundError(f"No cached 1m klines for {benchmark}")
        btc_path = max(btc_hits, key=end_ts)
    return sol_path, btc_path


def replay_trades(
    sol: Sequence[Kline],
    *,
    btc: Sequence[Kline] | None = None,
    take_profit_pct: float = 0.5,
    stop_pct: float = 0.35,
    timeout_bars: int = 60,
    notional: float = 1000.0,
    fee_rate: float = ROUND_TRIP_FEE,
) -> list[ReplayTrade]:
    """Same rules as the 7-day scan, plus an entry snapshot for Jev."""
    btc_by_time = {k.open_time: k for k in btc} if btc else {}
    tp_frac = take_profit_pct / 100.0
    sl_frac = stop_pct / 100.0
    trades: list[ReplayTrade] = []
    i = HISTORY_BARS
    last_entry = len(sol) - 1

    while i < last_entry:
        entry_px = float(sol[i].close)
        tp = entry_px * (1.0 + tp_frac)
        sl = entry_px * (1.0 - sl_frac)
        end_j = min(len(sol) - 1, i + timeout_bars)
        snapshot = build_snapshot(
            sol,
            i,
            btc_by_time=btc_by_time,
            take_profit_pct=take_profit_pct,
            stop_pct=stop_pct,
            timeout_minutes=timeout_bars,
        )
        outcome = None
        both_hit = False
        pnl = 0.0
        exit_index = end_j
        j = i + 1
        while j <= end_j:
            high = float(sol[j].high)
            low = float(sol[j].low)
            hit_tp = high >= tp
            hit_sl = low <= sl
            hold = j - i
            if hit_tp and hit_sl:
                outcome = "loss"
                both_hit = True
                pnl = (-sl_frac - fee_rate) * notional
                exit_index = j
                break
            if hit_sl:
                outcome = "loss"
                pnl = (-sl_frac - fee_rate) * notional
                exit_index = j
                break
            if hit_tp:
                outcome = "win"
                pnl = (tp_frac - fee_rate) * notional
                exit_index = j
                break
            j += 1
        if outcome is None:
            last_close = float(sol[end_j].close)
            move = (last_close / entry_px) - 1.0
            pnl = (move - fee_rate) * notional
            outcome = "timeout"
            hold = end_j - i
            exit_index = end_j
        else:
            hold = exit_index - i

        trades.append(
            ReplayTrade(
                entry_index=i,
                exit_index=exit_index,
                entry_ms=sol[i].open_time,
                exit_ms=sol[exit_index].open_time,
                outcome=outcome,
                both_hit=both_hit,
                pnl_usd=pnl,
                hold_minutes=hold,
                snapshot=snapshot,
                example=compact_example(snapshot, outcome, hold),
            )
        )
        i = exit_index + 1
    return trades


def split_examples(
    trades: Sequence[ReplayTrade],
    *,
    wins_needed: int = 10,
    losses_needed: int = 10,
) -> tuple[list[ReplayTrade], list[ReplayTrade], list[ReplayTrade]]:
    """First 10 wins and 10 losses in time order become examples. The rest is the test set."""
    wins: list[ReplayTrade] = []
    losses: list[ReplayTrade] = []
    filled_at: int | None = None
    for idx, trade in enumerate(trades):
        if trade.outcome == "win" and len(wins) < wins_needed:
            wins.append(trade)
        elif trade.outcome == "loss" and len(losses) < losses_needed:
            losses.append(trade)
        if len(wins) >= wins_needed and len(losses) >= losses_needed:
            filled_at = idx
            break
    if filled_at is None:
        raise ValueError(
            f"Need {wins_needed} wins and {losses_needed} losses; "
            f"got {len(wins)} wins and {len(losses)} losses"
        )
    test = list(trades[filled_at + 1 :])
    return wins, losses, test


def trade_public_dict(trade: ReplayTrade) -> dict[str, object]:
    data = asdict(trade)
    return data
