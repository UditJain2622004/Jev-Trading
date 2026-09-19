from decimal import Decimal

from jev_trading.binance.tp_sl_scan import simulate_pair
from jev_trading.binance.types import Kline


def _bar(i: int, high: str, low: str, close: str) -> Kline:
    return Kline(
        symbol="TESTUSDT",
        interval="1m",
        open_time=i * 60_000,
        close_time=i * 60_000 + 59_999,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        quote_volume=Decimal("1"),
        trades=1,
        taker_buy_base=Decimal("0"),
        taker_buy_quote=Decimal("0"),
    )


def test_take_profit_hits_before_stop():
    klines = [
        _bar(0, "100", "100", "100"),
        _bar(1, "100.2", "99.95", "100.1"),
        _bar(2, "100.6", "100.1", "100.5"),
    ]
    stats = simulate_pair(klines, 0.50, 0.08, notional=1000, timeout_bars=10, fee_rate=0.002)
    assert stats["trades"] == 1
    assert stats["wins"] == 1
    assert stats["net_usd"] == 3.0


def test_stop_hits_and_same_bar_counts_as_stop():
    klines = [
        _bar(0, "100", "100", "100"),
        _bar(1, "100.6", "99.90", "100.2"),
    ]
    stats = simulate_pair(klines, 0.50, 0.08, notional=1000, timeout_bars=10, fee_rate=0.002)
    assert stats["losses"] == 1
    assert stats["both_hit"] == 1
    assert stats["net_usd"] == -2.8
