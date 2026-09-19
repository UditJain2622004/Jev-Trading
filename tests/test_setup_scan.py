from decimal import Decimal

import pytest

from jev_trading.binance.setup_scan import (
    Bar15,
    breakeven_win_rate_pct,
    build_15m_bars,
    is_dump_reclaim,
    setup_entries,
    _simulate_entries,
)
from jev_trading.binance.types import Kline


def _bar(i: int, open_px: str, high: str, low: str, close: str) -> Kline:
    return Kline(
        symbol="TESTUSDT",
        interval="1m",
        open_time=i * 60_000,
        close_time=i * 60_000 + 59_999,
        open=Decimal(open_px),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        quote_volume=Decimal("1"),
        trades=1,
        taker_buy_base=Decimal("0"),
        taker_buy_quote=Decimal("0"),
    )


def test_breakeven_is_below_fifty_on_asymmetric_wider_targets():
    # +2% / -1% after 0.20% fees: $18 win vs $12 loss → 40%
    assert abs(breakeven_win_rate_pct(2.0, 1.0) - 40.0) < 0.01


def test_is_dump_reclaim():
    prev = Bar15(0, 14, open=100.0, high=100.2, low=98.0, close=98.2)
    curr = Bar15(15, 29, open=98.2, high=99.6, low=98.1, close=99.4)
    assert is_dump_reclaim(prev, curr, 1.2)
    assert not is_dump_reclaim(prev, curr, 2.5)
    faded = Bar15(15, 29, open=98.2, high=98.5, low=97.8, close=98.0)
    assert not is_dump_reclaim(prev, faded, 1.2)


def test_build_15m_and_setup_entry():
    klines = []
    # 12h warmup at 100, with a 105 high so room exists later
    for i in range(720):
        high = "105" if i == 400 else "100.1"
        klines.append(_bar(i, "100", high, "99.9", "100"))
    # dump 15m: 100 → 98.5
    for i in range(720, 735):
        close = 100.0 - (i - 719) * (1.5 / 15.0)
        klines.append(_bar(i, str(close + 0.1), str(close + 0.1), str(close), str(close)))
    # reclaim 15m: green back through dump midpoint
    for i in range(735, 750):
        close = 98.5 + (i - 734) * (1.0 / 15.0)
        klines.append(_bar(i, str(close - 0.05), str(close), str(close - 0.1), str(close)))
    bars = build_15m_bars(klines)
    assert bars
    entries = setup_entries(klines, bars, dump_pct=1.2, room_need_pct=1.5)
    assert entries
    assert entries[0] == 749


def test_take_profit_on_setup_entry():
    klines = [_bar(0, "100", "100", "100", "100"), _bar(1, "100", "102.1", "99.9", "102.0")]
    stats = _simulate_entries(klines, [0], 2.0, 1.0, timeout_minutes=10, notional=1000, fee_rate=0.002)
    assert stats["wins"] == 1
    assert stats["net_usd"] == pytest.approx(18.0)


def test_same_bar_tp_and_sl_counts_as_stop():
    klines = [_bar(0, "100", "100", "100", "100"), _bar(1, "102.1", "102.1", "98.9", "101.0")]
    stats = _simulate_entries(klines, [0], 2.0, 1.0, timeout_minutes=10, notional=1000, fee_rate=0.002)
    assert stats["losses"] == 1
    assert stats["both_hit"] == 1
    assert stats["net_usd"] == pytest.approx(-12.0)
