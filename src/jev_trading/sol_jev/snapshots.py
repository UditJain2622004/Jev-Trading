from __future__ import annotations

from statistics import median
from typing import Mapping, Sequence

from jev_trading.binance.types import Kline

HISTORY_BARS = 60


def _close(klines: Sequence[Kline], i: int) -> float:
    return float(klines[i].close)


def _pct(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return round((end / start - 1.0) * 100.0, 3)


def _change(klines: Sequence[Kline], i: int, bars: int) -> float | None:
    j = i - bars
    if j < 0:
        return None
    return _pct(_close(klines, j), _close(klines, i))


def _by_time(klines: Sequence[Kline]) -> dict[int, Kline]:
    return {k.open_time: k for k in klines}


def build_snapshot(
    sol: Sequence[Kline],
    i: int,
    *,
    btc_by_time: Mapping[int, Kline] | None = None,
    take_profit_pct: float = 0.5,
    stop_pct: float = 0.35,
    timeout_minutes: int = 60,
) -> dict[str, object]:
    """Compact 'now' card at bar i (entry = that bar's close)."""
    if i < HISTORY_BARS:
        raise ValueError("Need an hour of bars before entry so Jev has context")
    window = sol[i - HISTORY_BARS : i + 1]
    last = sol[i]
    entry = float(last.close)
    hour_high = max(float(k.high) for k in window)
    hour_low = min(float(k.low) for k in window)
    vols = [float(k.quote_volume) for k in window]
    typical = median(vols) if vols else 0.0
    last_vol = float(last.quote_volume)
    taker_quote = float(last.taker_buy_quote)
    taker_buy_share = round(taker_quote / last_vol, 3) if last_vol else None

    btc_moves: dict[str, float | None] = {"5m": None, "15m": None, "1h": None}
    if btc_by_time:
        aligned: list[Kline] = []
        for k in window:
            hit = btc_by_time.get(k.open_time)
            if hit is not None:
                aligned.append(hit)
        if len(aligned) >= 2:
            btc_i = len(aligned) - 1
            btc_moves = {
                "5m": _change(aligned, btc_i, 5),
                "15m": _change(aligned, btc_i, 15),
                "1h": _change(aligned, btc_i, min(60, btc_i)),
            }

    return {
        "entry": round(entry, 4),
        "take_profit_pct": take_profit_pct,
        "stop_pct": stop_pct,
        "timeout_minutes": timeout_minutes,
        "sol_moves_pct": {
            "1m": _change(sol, i, 1),
            "5m": _change(sol, i, 5),
            "15m": _change(sol, i, 15),
            "1h": _change(sol, i, 60),
        },
        "sol_last_hour": {
            "high": round(hour_high, 4),
            "low": round(hour_low, 4),
            "range_pct": _pct(hour_low, hour_high),
            "room_to_hour_high_pct": _pct(entry, hour_high),
        },
        "volume": {
            "last_1m": round(last_vol, 2),
            "typical_1h_1m": round(float(typical), 2),
            "vs_typical": round(last_vol / typical, 2) if typical else None,
            "taker_buy_share": taker_buy_share,
        },
        "btc_moves_pct": btc_moves,
    }


def compact_example(snapshot: dict[str, object], outcome: str, hold_minutes: int) -> dict[str, object]:
    return {
        "outcome": outcome,
        "hold_minutes": hold_minutes,
        "sol_moves_pct": snapshot["sol_moves_pct"],
        "sol_last_hour": snapshot["sol_last_hour"],
        "volume": snapshot["volume"],
        "btc_moves_pct": snapshot["btc_moves_pct"],
    }
