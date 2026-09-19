from decimal import Decimal

from jev_trading.binance.fees import FeeSchedule, round_trip_economics
from jev_trading.binance.types import Liquidity


def test_vip0_thousand_dollar_one_dollar_net():
    econ = round_trip_economics(
        notional=1000,
        entry_price=76000,
        target_net=1,
        extra_slippage_bps_each_way=1,
    )
    assert econ.buy_fee == Decimal("1")
    assert econ.sell_fee == Decimal("1")
    assert econ.slippage_cost == Decimal("0.2")
    assert econ.total_cost == Decimal("2.2")
    assert econ.required_gross == Decimal("3.2")
    assert econ.required_move_bps == Decimal("32")
    assert econ.required_exit_price == Decimal("76000") * Decimal("1.0032")


def test_maker_maker_with_bnb_is_cheaper():
    taker = round_trip_economics(notional=1000, target_net=1, extra_slippage_bps_each_way=0)
    maker = round_trip_economics(
        notional=1000,
        target_net=1,
        extra_slippage_bps_each_way=0,
        buy_liquidity=Liquidity.MAKER,
        sell_liquidity=Liquidity.MAKER,
        fees=FeeSchedule.vip0(use_bnb_discount=True),
    )
    assert maker.total_cost < taker.total_cost
    assert maker.required_move_bps == Decimal("25")
