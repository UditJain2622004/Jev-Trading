from decimal import Decimal

import pytest

from jev_trading.binance.errors import LiveTradingDisabled
from jev_trading.binance.fees import FeeSchedule
from jev_trading.binance.paper import PaperBroker
from jev_trading.binance.replay import pessimistic_taker_price, synthetic_book_from_kline
from jev_trading.binance.rest import BinanceRestClient
from jev_trading.binance.trading import TradingClient
from jev_trading.binance.types import Kline, Side
from tests.conftest import btcusdt_rules


def _flat_book(bid: str = "100.00", ask: str = "100.01", size: str = "50"):
    from jev_trading.binance.types import BookLevel, OrderBook

    return OrderBook(
        symbol="BTCUSDT",
        bids=(BookLevel(Decimal(bid), Decimal(size)),),
        asks=(BookLevel(Decimal(ask), Decimal(size)),),
    )


def test_paper_round_trip_loses_fees_and_spread():
    rules = btcusdt_rules()
    broker = PaperBroker(
        starting_quote=1000,
        fees=FeeSchedule.vip0(),
        extra_slippage_bps=0,
    )
    book = _flat_book()
    buy = broker.market(rules, book, Side.BUY, quote_qty=Decimal("1000"))
    sell = broker.market(rules, book, Side.SELL, quantity=broker.balance("BTC"))
    leftover = broker.equity({"BTC": book.mid})
    assert leftover < Decimal("1000")
    assert buy.fee > 0
    assert sell.fee > 0
    assert leftover > Decimal("990")


def test_paper_target_one_dollar_needs_move():
    rules = btcusdt_rules()
    broker = PaperBroker(starting_quote=1000, extra_slippage_bps=0)
    entry = _flat_book("100.00", "100.00")
    broker.market(rules, entry, Side.BUY, quote_qty=Decimal("1000"))
    btc = broker.balance("BTC")
    exit_book = _flat_book("103.30", "103.30")
    broker.market(rules, exit_book, Side.SELL, quantity=btc)
    pnl = broker.balance("USDT") - Decimal("1000")
    assert pnl >= Decimal("1")


def test_resting_limit_fills_only_when_trade_goes_through():
    rules = btcusdt_rules()
    broker = PaperBroker(starting_quote=1000, extra_slippage_bps=0, maker_fill="through")
    book = _flat_book("100.00", "100.10")
    order = broker.limit(rules, book, Side.BUY, Decimal("0.1"), Decimal("99.50"))
    assert order.status == "NEW"
    from jev_trading.binance.types import AggTrade

    trade = AggTrade(
        symbol="BTCUSDT",
        agg_id=1,
        price=Decimal("99.50"),
        quantity=Decimal("1"),
        first_trade_id=1,
        last_trade_id=1,
        timestamp=1,
        is_buyer_maker=True,
    )
    assert broker.on_trade(rules, trade) == []
    trade_through = AggTrade(
        symbol="BTCUSDT",
        agg_id=2,
        price=Decimal("99.49"),
        quantity=Decimal("1"),
        first_trade_id=2,
        last_trade_id=2,
        timestamp=2,
        is_buyer_maker=True,
    )
    fills = broker.on_trade(rules, trade_through)
    assert len(fills) == 1
    assert fills[0].liquidity.value == "MAKER"


def test_pessimistic_kline_buy_uses_high():
    kline = Kline(
        symbol="BTCUSDT",
        interval="1s",
        open_time=0,
        close_time=999,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1"),
        quote_volume=Decimal("100"),
        trades=10,
        taker_buy_base=Decimal("0.4"),
        taker_buy_quote=Decimal("40"),
    )
    assert pessimistic_taker_price(kline, Side.BUY) == Decimal("110")
    assert pessimistic_taker_price(kline, Side.SELL) == Decimal("90")
    book = synthetic_book_from_kline(kline, spread_bps=2, size=5)
    assert book.best_ask.price > kline.close
    assert book.best_bid.price < kline.close


def test_trading_client_blocks_live_orders():
    client = TradingClient(client=BinanceRestClient(env="live", api_key="x", api_secret="y"))
    with pytest.raises(LiveTradingDisabled):
        client.new_order("BTCUSDT", Side.BUY, "MARKET", quote_order_qty="10")
