from decimal import Decimal

from jev_trading.binance.types import Kline
from jev_trading.sol_jev.decide import decide_from_answers
from jev_trading.sol_jev.replay import ReplayTrade, replay_trades, split_examples
from jev_trading.sol_jev.snapshots import HISTORY_BARS, build_snapshot


def _bar(i: int, close: str, high: str | None = None, low: str | None = None) -> Kline:
    px = Decimal(close)
    return Kline(
        symbol="SOLUSDT",
        interval="1m",
        open_time=i * 60_000,
        close_time=i * 60_000 + 59_999,
        open=px,
        high=Decimal(high) if high else px,
        low=Decimal(low) if low else px,
        close=px,
        volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        trades=10,
        taker_buy_base=Decimal("5"),
        taker_buy_quote=Decimal("500"),
    )


def test_snapshot_has_moves():
    sol = [_bar(i, "100") for i in range(HISTORY_BARS + 1)]
    sol[-1] = _bar(HISTORY_BARS, "101", high="102", low="99")
    snap = build_snapshot(sol, HISTORY_BARS)
    assert snap["entry"] == 101.0
    assert snap["sol_moves_pct"]["1m"] == 1.0
    assert snap["sol_last_hour"]["range_pct"] > 0


def _trade(outcome: str, i: int) -> ReplayTrade:
    return ReplayTrade(
        entry_index=i,
        exit_index=i + 1,
        entry_ms=i,
        exit_ms=i + 1,
        outcome=outcome,
        both_hit=False,
        pnl_usd=3.0 if outcome == "win" else -5.5,
        hold_minutes=1,
        snapshot={},
        example={"outcome": outcome},
    )


def test_split_waits_for_ten_each_then_tests_the_rest():
    trades = [_trade("loss", i) for i in range(10)]
    trades += [_trade("win", i) for i in range(10, 20)]
    trades += [_trade("timeout", 20), _trade("win", 21), _trade("loss", 22)]
    wins, losses, test = split_examples(trades, wins_needed=10, losses_needed=10)
    assert len(wins) == 10
    assert len(losses) == 10
    assert [t.outcome for t in test] == ["timeout", "win", "loss"]


def test_decide_skips_when_it_looks_like_a_loss():
    decision = decide_from_answers(
        {
            "hit_tp_first": {"choice": "1", "confidence": 0.8},
            "looks_like": {"choice": "more_like_losses", "confidence": 0.8},
        }
    )
    assert decision.take is False
    assert "losing" in decision.reason


def test_decide_skips_when_hit_is_zero():
    decision = decide_from_answers(
        {
            "hit_tp_first": {"choice": "0", "confidence": 0.7},
            "looks_like": {"choice": "more_like_wins", "confidence": 0.7},
        }
    )
    assert decision.take is False
    assert decision.hit_tp_first == "0"


def test_replay_records_win_then_loss():
    bars = [_bar(i, "100") for i in range(HISTORY_BARS + 5)]
    bars[HISTORY_BARS + 1] = _bar(HISTORY_BARS + 1, "100.4", high="100.6", low="100.0")
    bars[HISTORY_BARS + 3] = _bar(HISTORY_BARS + 3, "99.7", high="100.0", low="99.6")
    trades = replay_trades(bars)
    assert [t.outcome for t in trades[:2]] == ["win", "loss"]
    assert trades[0].pnl_usd == 3.0
    assert trades[1].pnl_usd == -5.5


def test_timeouts_are_not_used_as_examples():
    trades = [_trade("timeout", i) for i in range(5)]
    trades += [_trade("win", i) for i in range(5, 15)]
    trades += [_trade("loss", i) for i in range(15, 25)]
    trades += [_trade("win", 25)]
    wins, losses, test = split_examples(trades, wins_needed=10, losses_needed=10)
    assert all(t.outcome == "win" for t in wins)
    assert all(t.outcome == "loss" for t in losses)
    assert test[0].outcome == "win"


def test_decide_takes_clear_win_shape():
    decision = decide_from_answers(
        {
            "hit_tp_first": {"choice": "1", "confidence": 0.7},
            "looks_like": {"choice": "more_like_wins", "confidence": 0.7},
        }
    )
    assert decision.take is True
