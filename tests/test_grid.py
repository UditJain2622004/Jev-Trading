from decimal import Decimal

from jev_trading.binance.grid import (
    GridLevers,
    buy_level_price,
    grid_buy_prices,
    sell_target,
    simulate,
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


def test_buy_level_price_geometric():
    ref = 100.0
    step = 1.0
    assert abs(buy_level_price(ref, step, 1) - 99.0) < 1e-12
    assert abs(buy_level_price(ref, step, 2) - 99.0 * 0.99) < 1e-12
    prices = grid_buy_prices(ref, step, 3)
    assert len(prices) == 3
    assert prices[0] > prices[1] > prices[2]
    assert abs(sell_target(99.0, step) - 99.0 * 1.01) < 1e-12


def test_oscillating_bars_produce_round_trips():
    """Price dips to level-1 buy then rises through the sell target, repeatedly."""
    # ref = 100. Level-1 buy @ 99 (1% step). Sell @ 99 * 1.01 = 99.99.
    klines = [_bar(0, "100", "100", "100", "100")]
    for cycle in range(5):
        base = 1 + cycle * 2
        # Dip through buy
        klines.append(_bar(base, "100", "100", "98.5", "99.0"))
        # Rally through sell target (~99.99)
        klines.append(_bar(base + 1, "99.0", "100.5", "99.0", "100.0"))
    levers = GridLevers("unit_osc", step_pct=1.0, levels=2)
    stats = simulate(klines, levers, notional=1000.0, fee_rate=0.002)
    assert stats["buy_fills"] >= 1
    assert stats["round_trips"] > 0
    assert stats["sell_fills"] >= stats["round_trips"]


def test_same_bar_buy_then_sell():
    """One bar that tags the buy low and the sell high completes a round-trip."""
    # ref=100, step=1%, level-1 buy=99, sell=99.99
    klines = [
        _bar(0, "100", "100", "100", "100"),
        _bar(1, "100", "100.5", "98.5", "100.0"),
    ]
    levers = GridLevers("unit_same", step_pct=1.0, levels=1)
    stats = simulate(klines, levers, notional=1000.0, fee_rate=0.002)
    assert stats["buy_fills"] == 1
    assert stats["round_trips"] == 1
    assert stats["ending_inventory_qty"] == 0.0


def test_downtrend_stop_liquidates():
    """Price crashes through the stop below the lowest buy → halt with stopped=True."""
    # ref=100, step=1%, 2 levels → buys at 99 and 98.01. stop_pct=5 →
    # trigger = 98.01 * 0.95 ≈ 93.11
    klines = [_bar(0, "100", "100", "100", "100")]
    # Fill both buys without reaching sell targets (high stays below ~99.09)
    klines.append(_bar(1, "100", "98.5", "97.5", "98.0"))
    buys_before_crash = simulate(
        klines,
        GridLevers("probe", step_pct=1.0, levels=2, stop_pct=5.0),
        notional=1000.0,
        fee_rate=0.002,
    )["buy_fills"]
    assert buys_before_crash == 2

    # Crash through stop
    klines.append(_bar(2, "98", "98", "90", "91"))
    # Bounce bars must not produce more buys after halt
    klines.append(_bar(3, "91", "95", "91", "94"))
    klines.append(_bar(4, "94", "96", "93", "95"))
    levers = GridLevers("unit_stop", step_pct=1.0, levels=2, stop_pct=5.0)
    stats = simulate(klines, levers, notional=1000.0, fee_rate=0.002)
    assert stats["stopped"] is True
    assert stats["buy_fills"] == 2
    assert stats["ending_inventory_qty"] == 0.0
    assert stats["sell_fills"] == 2  # stop liquidations
    assert stats["round_trips"] == 0  # stop exits are not grid round-trips
