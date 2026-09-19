from __future__ import annotations

from datetime import datetime, timezone
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


def _slice(klines: Sequence[Kline], i: int, bars: int) -> Sequence[Kline]:
    start = max(0, i - bars + 1)
    return klines[start : i + 1]


def _path_counts(klines: Sequence[Kline], i: int, bars: int) -> dict[str, int]:
    up = down = 0
    start = i - bars + 1
    for j in range(max(start, 1), i + 1):
        prev = float(klines[j - 1].close)
        cur = float(klines[j].close)
        if cur > prev:
            up += 1
        elif cur < prev:
            down += 1
    return {"up_minutes": up, "down_minutes": down}


def _typical_abs_1m(klines: Sequence[Kline], i: int, bars: int) -> float | None:
    moves: list[float] = []
    start = i - bars + 1
    for j in range(max(start, 1), i + 1):
        prev = float(klines[j - 1].close)
        if prev == 0:
            continue
        moves.append(abs(float(klines[j].close) / prev - 1.0) * 100.0)
    if not moves:
        return None
    return round(float(median(moves)), 3)


def _buy_share(klines: Sequence[Kline], i: int, bars: int) -> float | None:
    window = _slice(klines, i, bars)
    quote = sum(float(k.quote_volume) for k in window)
    taker = sum(float(k.taker_buy_quote) for k in window)
    if quote <= 0:
        return None
    return round(taker / quote, 3)


def _quote_vol(klines: Sequence[Kline], i: int, bars: int) -> float:
    return sum(float(k.quote_volume) for k in _slice(klines, i, bars))


def _vwap(klines: Sequence[Kline], i: int, bars: int) -> float | None:
    window = _slice(klines, i, bars)
    base = sum(float(k.volume) for k in window)
    quote = sum(float(k.quote_volume) for k in window)
    if base <= 0:
        return None
    return quote / base


def _last_candle(last: Kline, entry: float) -> dict[str, float]:
    high = float(last.high)
    low = float(last.low)
    open_px = float(last.open)
    close = float(last.close)
    span = high - low
    upper = high - max(open_px, close)
    lower = min(open_px, close) - low
    return {
        "upper_wick_pct": round((upper / entry) * 100.0, 3) if entry else 0.0,
        "lower_wick_pct": round((lower / entry) * 100.0, 3) if entry else 0.0,
        "close_in_range": round((close - low) / span, 3) if span else 0.5,
    }


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
    vol_5m = _quote_vol(sol, i, 5)
    vol_prior_15m = _quote_vol(sol, i - 5, 15) if i >= 5 else 0.0
    vol_hour = _quote_vol(sol, i, 60)
    typical_1m_5m = _typical_abs_1m(sol, i, 5)
    typical_1m_15m = _typical_abs_1m(sol, i, 15)
    typical_1m_1h = _typical_abs_1m(sol, i, 60)
    vwap_15m = _vwap(sol, i, 15)
    vwap_1h = _vwap(sol, i, 60)
    when = datetime.fromtimestamp(last.open_time / 1000.0, tz=timezone.utc)

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
        "path": {
            "5m": _path_counts(sol, i, 5),
            "15m": _path_counts(sol, i, 15),
        },
        "volatility": {
            "typical_1m_move_5m_pct": typical_1m_5m,
            "typical_1m_move_15m_pct": typical_1m_15m,
            "typical_1m_move_1h_pct": typical_1m_1h,
            "hour_move_vs_take_profit": round(typical_1m_1h / take_profit_pct, 3) if typical_1m_1h else None,
            "hour_move_vs_stop": round(typical_1m_1h / stop_pct, 3) if typical_1m_1h else None,
        },
        "buy_pressure": {
            "1m": taker_buy_share,
            "5m": _buy_share(sol, i, 5),
            "15m": _buy_share(sol, i, 15),
        },
        "last_candle": _last_candle(last, entry),
        "volume_accel": {
            "last_5m_vs_prior_15m": round(vol_5m / vol_prior_15m, 2) if vol_prior_15m else None,
            "last_5m_vs_hour_share": round(vol_5m / vol_hour, 3) if vol_hour else None,
        },
        "vwap": {
            "vs_15m_pct": _pct(vwap_15m, entry) if vwap_15m else None,
            "vs_1h_pct": _pct(vwap_1h, entry) if vwap_1h else None,
        },
        "clock_utc": {
            "hour": when.hour,
            "weekday": when.weekday(),
        },
    }


def compact_example(snapshot: dict[str, object], outcome: str, hold_minutes: int) -> dict[str, object]:
    return {
        "outcome": outcome,
        "hold_minutes": hold_minutes,
        "sol_moves_pct": snapshot["sol_moves_pct"],
        "sol_last_hour": snapshot["sol_last_hour"],
        "volume": snapshot["volume"],
        "btc_moves_pct": snapshot["btc_moves_pct"],
        "path": snapshot["path"],
        "volatility": snapshot["volatility"],
        "buy_pressure": snapshot["buy_pressure"],
        "last_candle": snapshot["last_candle"],
        "volume_accel": snapshot["volume_accel"],
        "vwap": snapshot["vwap"],
        "clock_utc": snapshot["clock_utc"],
    }
