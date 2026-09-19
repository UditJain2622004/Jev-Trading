from __future__ import annotations

from typing import Any


def _label_move(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v <= -0.25:
        return "falling"
    if v < 0.10:
        return "flat"
    if v < 0.35:
        return "rising"
    return "sharply_rising"


def _label_room(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v < 0.15:
        return "near_high"
    if v < 0.40:
        return "upper_half"
    if v < 0.80:
        return "middle"
    return "has_room"


def _label_buy(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v < 0.45:
        return "seller_dominant"
    if v < 0.52:
        return "balanced"
    if v < 0.58:
        return "buyer_lean"
    return "buyer_dominant"


def _label_vol(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v < 0.70:
        return "quiet"
    if v < 1.30:
        return "normal"
    if v < 2.00:
        return "elevated"
    return "extreme"


def _label_up(value: Any, bars: int) -> str:
    if value is None:
        return "unknown"
    v = int(value)
    if bars <= 5:
        if v <= 1:
            return "mostly_down"
        if v <= 3:
            return "mixed"
        return "mostly_up"
    if v <= 5:
        return "mostly_down"
    if v <= 8:
        return "mixed"
    if v <= 11:
        return "mostly_up"
    return "almost_all_up"


def _label_close_in_range(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v >= 0.85:
        return "closed_at_highs"
    if v <= 0.20:
        return "closed_at_lows"
    return "closed_mid"


def _label_vwap(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v < -0.10:
        return "below"
    if v < 0.10:
        return "near"
    return "above"


def _label_vol_fit(hour_vs_tp: Any) -> str:
    if hour_vs_tp is None:
        return "unknown"
    v = float(hour_vs_tp)
    if v < 0.25:
        return "too_low"
    if v < 0.80:
        return "suitable"
    return "excessive"


def _label_accel(value: Any) -> str:
    if value is None:
        return "unknown"
    v = float(value)
    if v < 0.70:
        return "slowing"
    if v < 1.20:
        return "steady"
    return "accelerating"


def semantic_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    moves = snap.get("sol_moves_pct") if isinstance(snap.get("sol_moves_pct"), dict) else {}
    hour = snap.get("sol_last_hour") if isinstance(snap.get("sol_last_hour"), dict) else {}
    volume = snap.get("volume") if isinstance(snap.get("volume"), dict) else {}
    path = snap.get("path") if isinstance(snap.get("path"), dict) else {}
    path5 = path.get("5m") if isinstance(path.get("5m"), dict) else {}
    path15 = path.get("15m") if isinstance(path.get("15m"), dict) else {}
    pressure = snap.get("buy_pressure") if isinstance(snap.get("buy_pressure"), dict) else {}
    candle = snap.get("last_candle") if isinstance(snap.get("last_candle"), dict) else {}
    vola = snap.get("volatility") if isinstance(snap.get("volatility"), dict) else {}
    accel = snap.get("volume_accel") if isinstance(snap.get("volume_accel"), dict) else {}
    vwap = snap.get("vwap") if isinstance(snap.get("vwap"), dict) else {}
    btc = snap.get("btc_moves_pct") if isinstance(snap.get("btc_moves_pct"), dict) else {}
    clock = snap.get("clock_utc") if isinstance(snap.get("clock_utc"), dict) else {}
    return {
        "plan": {
            "coin": "SOLUSDT",
            "side": "buy",
            "take_profit": "plus_half_percent",
            "stop": "minus_point_three_five_percent",
            "timeout": "one_hour",
        },
        "momentum": {
            "1m": _label_move(moves.get("1m")),
            "5m": _label_move(moves.get("5m")),
            "15m": _label_move(moves.get("15m")),
            "1h": _label_move(moves.get("1h")),
        },
        "position_in_hour": _label_room(hour.get("room_to_hour_high_pct")),
        "path": {
            "5m": _label_up(path5.get("up_minutes"), 5),
            "15m": _label_up(path15.get("up_minutes"), 15),
        },
        "buy_pressure": {
            "1m": _label_buy(pressure.get("1m")),
            "5m": _label_buy(pressure.get("5m")),
            "15m": _label_buy(pressure.get("15m")),
        },
        "volume": {
            "vs_typical": _label_vol(volume.get("vs_typical")),
            "accel_5m": _label_accel(accel.get("last_5m_vs_prior_15m")),
        },
        "last_candle": _label_close_in_range(candle.get("close_in_range")),
        "volatility": _label_vol_fit(vola.get("hour_move_vs_take_profit")),
        "vwap": {
            "15m": _label_vwap(vwap.get("vs_15m_pct")),
            "1h": _label_vwap(vwap.get("vs_1h_pct")),
        },
        "btc": {
            "15m": _label_move(btc.get("15m")),
            "1h": _label_move(btc.get("1h")),
        },
        "clock_utc": clock,
    }


def semantic_example(snap: dict[str, Any], outcome: str, hold_minutes: int, pnl_usd: float) -> dict[str, Any]:
    card = semantic_snapshot(snap)
    card["outcome"] = outcome
    card["hold"] = "fast" if hold_minutes <= 10 else ("medium" if hold_minutes <= 30 else "slow")
    card["result"] = "profit" if pnl_usd > 0 else "no_profit"
    return card
