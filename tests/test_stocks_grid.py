from pytest import approx

from stocks.bars import Bar
from stocks.grid import (
    GridLevers,
    ROUND_TRIP_FEE,
    buy_level_price,
    grid_buy_prices,
    sell_target,
    simulate,
)


def _bar(i: int, open_px: float, high: float, low: float, close: float) -> Bar:
    return Bar("TEST", i * 5 * 60_000, open_px, high, low, close, 1.0)


def test_buy_level_price_geometric():
    ref = 100.0
    step = 1.0
    assert abs(buy_level_price(ref, step, 1) - 99.0) < 1e-12
    assert abs(buy_level_price(ref, step, 2) - 99.0 * 0.99) < 1e-12
    prices = grid_buy_prices(ref, step, 3)
    assert len(prices) == 3
    assert prices[0] > prices[1] > prices[2]
    assert abs(sell_target(99.0, step) - 99.0 * 1.01) < 1e-12


def test_forced_round_trip_net_after_fees():
    """One bar tags buy low and sell high → known net after 0.6% round-trip fees.

    ref=100, step=1%, 1 level → buy @ 99, sell @ 99*1.01 = 99.99.
    notional=1000 all on that level.
    qty = 1000/99; proceeds = qty * 99.99 = 1000 * 1.01 = 1010 exactly.
    Gross realize = 10. Buy fee = 3. Sell fee = 0.003 * 1010 = 3.03.
    Net = 10 - 6.03 = 3.97.
    """
    bars = [
        _bar(0, 100.0, 100.0, 100.0, 100.0),
        _bar(1, 100.0, 100.5, 98.5, 100.0),
    ]
    levers = GridLevers("unit_same", step_pct=1.0, levels=1)
    stats = simulate(bars, levers, notional=1000.0, fee_rate=ROUND_TRIP_FEE)
    assert stats["buy_fills"] == 1
    assert stats["round_trips"] == 1
    assert stats["ending_inventory_qty"] == 0.0

    buy_px = 99.0
    sell_px = 99.0 * 1.01
    qty = 1000.0 / buy_px
    proceeds = qty * sell_px
    gross = proceeds - 1000.0
    half = ROUND_TRIP_FEE / 2.0
    fees = half * 1000.0 + half * proceeds
    expected_net = gross - fees
    assert stats["realized_usd"] == approx(gross, abs=1e-9)
    assert stats["fees_usd"] == approx(fees, abs=1e-9)
    assert stats["net_usd"] == approx(expected_net, abs=1e-9)
    assert stats["net_usd"] == approx(3.97, abs=1e-6)


def test_downtrend_stop_liquidates():
    """Price crashes through the stop below the lowest buy → halt with stopped=True."""
    # ref=100, step=1%, 2 levels → buys at 99 and 98.01. stop_pct=5 →
    # trigger = 98.01 * 0.95 ≈ 93.11
    bars = [_bar(0, 100.0, 100.0, 100.0, 100.0)]
    bars.append(_bar(1, 100.0, 98.5, 97.5, 98.0))
    buys_before_crash = simulate(
        bars,
        GridLevers("probe", step_pct=1.0, levels=2, stop_pct=5.0),
        notional=1000.0,
        fee_rate=ROUND_TRIP_FEE,
    )["buy_fills"]
    assert buys_before_crash == 2

    bars.append(_bar(2, 98.0, 98.0, 90.0, 91.0))
    bars.append(_bar(3, 91.0, 95.0, 91.0, 94.0))
    bars.append(_bar(4, 94.0, 96.0, 93.0, 95.0))
    levers = GridLevers("unit_stop", step_pct=1.0, levels=2, stop_pct=5.0)
    stats = simulate(bars, levers, notional=1000.0, fee_rate=ROUND_TRIP_FEE)
    assert stats["stopped"] is True
    assert stats["buy_fills"] == 2
    assert stats["ending_inventory_qty"] == 0.0
    assert stats["sell_fills"] == 2  # stop liquidations
    assert stats["round_trips"] == 0  # stop exits are not grid round-trips
