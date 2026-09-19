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


def test_confirm_entry_requires_higher_lows_and_bounce():
    from stocks.dip import find_entries_confirm

    # Flat then dump into dip, then bounce with ascending lows + close up from arm low
    bars = [_bar(i, 100, 100, 100, 100) for i in range(12)]
    # dip bar: close well below high of lookback
    bars[11] = _bar(11, 94, 94, 93.5, 94)  # ~6% off 100 high
    # wait: ascending lows and close +0.5% from arm_low 93.5 → need close >= 93.9675
    bars += [
        _bar(12, 94.0, 94.2, 93.6, 94.0),
        _bar(13, 94.1, 94.4, 93.8, 94.2),
        _bar(14, 94.3, 94.8, 94.0, 94.5),  # higher lows 93.6<93.8<94.0, close 94.5 >= bounce
    ]
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,  # 12 bars
        stable_range_pct=1.5,
        stable_minutes=30,  # 6 bar wait window
        take_profit_pct=5.0,
        stop_loss_pct=4.0,
        timeout_minutes=120,
        bounce_bars=3,
        bounce_pct=0.5,
        arm_max_adverse_pct=2.0,
    )
    assert is_dip(bars, 11, dip_pct=5.0, dip_bars=12)
    entries, cands = find_entries_confirm(bars, levers)
    assert cands >= 1
    # First confirm bar: lows of arm+1/+2 form ascending with prior; earliest is bar 13
    assert entries[0] == 13


def test_confirm_entry_killed_by_adverse():
    from stocks.dip import find_entries_confirm

    bars = [_bar(i, 100, 100, 100, 100) for i in range(12)]
    bars[11] = _bar(11, 94, 94, 93.5, 94)
    # dump past arm_low * 0.98 = 91.63
    bars += [_bar(12, 92, 92, 91.0, 91.5)]
    bars += [_bar(13, 92, 95, 92, 94.5)]
    bars += [_bar(14, 94, 95, 94, 95)]
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,
        stable_range_pct=1.5,
        stable_minutes=30,
        take_profit_pct=5.0,
        stop_loss_pct=4.0,
        timeout_minutes=120,
        bounce_bars=3,
        bounce_pct=0.5,
        arm_max_adverse_pct=2.0,
    )
    entries, cands = find_entries_confirm(bars, levers)
    assert cands >= 1
    assert entries == []


def test_trail_exit_soft_tp_then_floor():
    from stocks.dip import _exit_trade_trail

    # Entry at close 100; TP=105; trail 0.75%
    bars = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 101, 106, 101, 105),  # touches TP, HW=106; floor=106*0.9925≈105.205 — low 101 breaks?
        # Wait: on bar 1, high=106 touches TP, high_water=106, sell_floor≈105.205, low=101 <= floor → win same bar
    ]
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,
        stable_range_pct=1.5,
        stable_minutes=30,
        take_profit_pct=5.0,
        stop_loss_pct=4.0,
        timeout_minutes=120,
        trail_pct=0.75,
    )
    outcome, pnl, hold, both, ret, exit_t = _exit_trade_trail(
        bars, 0, levers, notional=1000.0, fee_rate=0.006
    )
    assert outcome == "win"
    assert hold == 1
    floor = 106 * (1 - 0.0075)
    assert ret == approx(floor / 100.0 - 1.0 - 0.006)


def test_trail_exit_hard_sl_before_tp():
    from stocks.dip import _exit_trade_trail

    bars = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 99, 99.5, 95.5, 96),  # SL at 96; low 95.5 hits SL
    ]
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,
        stable_range_pct=1.5,
        stable_minutes=30,
        take_profit_pct=5.0,
        stop_loss_pct=4.0,
        timeout_minutes=120,
        trail_pct=0.75,
    )
    outcome, pnl, hold, both, ret, exit_t = _exit_trade_trail(
        bars, 0, levers, notional=1000.0, fee_rate=0.006
    )
    assert outcome == "loss"
    assert ret == approx(-0.04 - 0.006)
    assert pnl == approx((-0.04 - 0.006) * 1000)


def test_trail_exit_does_not_sell_on_first_tp_touch_alone():
    from stocks.dip import _exit_trade_trail

    # Touch TP with tight range so low stays above trail floor; then later break floor
    bars = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 104, 105.2, 104.5, 105.0),  # TP touched, HW=105.2, floor≈104.41; low 104.5 > floor → hold
        _bar(2, 105, 107.0, 105.0, 106.5),  # HW=107, floor≈106.20; low 105.0 breaks → exit
    ]
    levers = DipLevers(
        "unit",
        dip_pct=5.0,
        dip_minutes=60,
        stable_range_pct=1.5,
        stable_minutes=30,
        take_profit_pct=5.0,
        stop_loss_pct=4.0,
        timeout_minutes=120,
        trail_pct=0.75,
    )
    outcome, pnl, hold, both, ret, exit_t = _exit_trade_trail(
        bars, 0, levers, notional=1000.0, fee_rate=0.006
    )
    assert outcome == "win"
    assert hold == 2
    floor = 107.0 * (1 - 0.0075)
    assert ret == approx(floor / 100.0 - 1.0 - 0.006)


def test_best_per_stock_levers_map():
    from stocks.scan_buy_the_dip_portfolio import (
        BEST_PER_STOCK_COMBO_NAMES,
        build_best_per_stock_levers,
    )

    levers = build_best_per_stock_levers(["AMD", "DELL", "MRVL"])
    assert levers["AMD"].name == "d5_w120"
    assert levers["DELL"].name == "d5_w60"
    assert levers["MRVL"].name == "d5_w180"
    assert levers["AMD"].stable_minutes == 120
    assert levers["DELL"].stable_minutes == 60
    assert set(BEST_PER_STOCK_COMBO_NAMES) >= {"AMD", "DELL", "MRVL", "TXN"}


def test_simulate_portfolio_per_stock_records_distinct_combos(monkeypatch):
    from stocks.bars import Bar
    from stocks.scan_buy_the_dip_portfolio import (
        Candidate,
        levers_for_combo_name,
        simulate_portfolio_per_stock,
    )
    import stocks.scan_buy_the_dip_portfolio as port

    def make_bars(sym: str, t0: int) -> list[Bar]:
        bars = [Bar(sym, t0, 100, 100, 100, 100, 1.0)]
        # next bar hits TP at 105
        bars.append(Bar(sym, t0 + 300_000, 105, 106, 104, 105.5, 1.0))
        return bars

    bars_by = {
        "AAA": make_bars("AAA", 0),
        "BBB": make_bars("BBB", 10_000_000),  # after AAA exit so all-in can take both
    }
    levers_by = {
        "AAA": levers_for_combo_name("d5_w120"),
        "BBB": levers_for_combo_name("d4_w60"),
    }

    def fake_collect(symbol, bars, levers, *, entry_mode="baseline"):
        return [Candidate(symbol, 0, bars[0].open_time, bars[0].close)]

    monkeypatch.setattr(port, "collect_candidates", fake_collect)

    taken, summary = simulate_portfolio_per_stock(
        bars_by,
        levers_by,
        start_equity=1000.0,
        max_positions=1,
        fee_rate=0.006,
        entry_mode="baseline",
    )
    assert summary["combo"] == "perstock_best"
    assert len(taken) == 2
    by_sym = {t.symbol: t.combo for t in taken}
    assert by_sym["AAA"] == "d5_w120"
    assert by_sym["BBB"] == "d4_w60"
    assert by_sym["AAA"] != by_sym["BBB"]
