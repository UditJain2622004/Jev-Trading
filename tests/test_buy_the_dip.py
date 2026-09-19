from decimal import Decimal

from jev_trading.binance.buy_the_dip import DipLevers, find_entries, is_dip, is_stable, simulate
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


def test_is_dip_from_window_high():
    klines = [_bar(0, "100", "100", "100", "100")]
    klines += [_bar(i, "99", "99", "97.5", "97.5") for i in range(1, 15)]
    assert is_dip(klines, 14, dip_pct=2.0, dip_minutes=15)
    assert not is_dip(klines, 14, dip_pct=4.0, dip_minutes=15)


def test_stable_only_cares_about_the_last_close():
    dip = [_bar(0, "100", "100", "98", "98")]
    wander = [_bar(i, "98", "100", "96", "96.5") for i in range(1, 10)]
    wander.append(_bar(10, "98.0", "98.3", "97.8", "98.2"))
    assert is_stable(dip + wander, 0, stable_range_pct=1.0, stable_minutes=10)
    falling = [_bar(i, "98", "98", "96", "96") for i in range(1, 11)]
    assert not is_stable(dip + falling, 0, stable_range_pct=1.0, stable_minutes=10)


def test_dip_then_base_then_take_profit():
    klines = [_bar(0, "100", "100", "100", "100")]
    klines += [_bar(i, "99", "99", "97.5", "97.5") for i in range(1, 15)]
    klines += [_bar(i, "97.5", "97.6", "97.4", "97.5") for i in range(15, 25)]
    klines += [_bar(25, "97.5", "99.0", "97.4", "98.8")]
    levers = DipLevers(
        "unit",
        dip_pct=2.0,
        dip_minutes=15,
        stable_range_pct=0.30,
        stable_minutes=10,
        take_profit_pct=1.2,
        stop_loss_pct=0.7,
        timeout_minutes=30,
    )
    entries, candidates = find_entries(klines, levers)
    assert candidates == 1
    assert entries == [24]
    stats = simulate(klines, levers, notional=1000, fee_rate=0.002)
    assert stats["wins"] == 1
    assert stats["net_usd"] == 10.0
