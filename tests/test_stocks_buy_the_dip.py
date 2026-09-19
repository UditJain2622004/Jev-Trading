from pytest import approx

from stocks.bars import Bar
from stocks.dip import DipLevers, find_entries, is_dip, is_stable, minutes_to_bars, simulate


def _bar(i: int, open_px: float, high: float, low: float, close: float) -> Bar:
    return Bar("TEST", i * 5 * 60_000, open_px, high, low, close, 1.0)


def test_minutes_to_bars():
    assert minutes_to_bars(30) == 6
    assert minutes_to_bars(390) == 78


def test_stable_only_last_close():
    dip = [_bar(0, 100, 100, 90, 90)]
    wander = [_bar(i, 90, 95, 85, 85) for i in range(1, 6)]
    wander.append(_bar(6, 90, 91, 89, 90.5))
    assert is_stable(dip + wander, 0, stable_range_pct=1.0, stable_bars=6)
    falling = [_bar(i, 90, 90, 80, 80) for i in range(1, 7)]
    assert not is_stable(dip + falling, 0, stable_range_pct=1.0, stable_bars=6)


def test_dip_then_wait_then_take_profit():
    bars = [_bar(0, 100, 100, 100, 100)]
    bars += [_bar(i, 96, 96, 94, 94) for i in range(1, 12)]
    bars += [_bar(i, 94, 94.2, 93.8, 94) for i in range(12, 18)]
    bars += [_bar(18, 94, 100, 94, 99)]
    assert is_dip(bars, 11, dip_pct=5.0, dip_bars=12)
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,
        stable_range_pct=1.0,
        stable_minutes=30,
        take_profit_pct=5.0,
        stop_loss_pct=3.0,
        timeout_minutes=120,
    )
    entries, candidates = find_entries(bars, levers)
    assert candidates == 1
    stats = simulate(bars, levers, notional=1000, fee_rate=0.006)
    assert stats["wins"] == 1
    assert stats["net_usd"] == approx(44.0)
