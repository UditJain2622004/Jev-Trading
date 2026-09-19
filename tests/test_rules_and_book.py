from decimal import Decimal

import pytest

from jev_trading.binance.book import walk_book
from jev_trading.binance.errors import InsufficientLiquidity, OrderRejected
from jev_trading.binance.rules import qty_from_quote
from jev_trading.binance.types import BookLevel, OrderBook, Side
from tests.conftest import btcusdt_rules


def test_round_qty_and_price():
    rules = btcusdt_rules()
    assert rules.round_qty(Decimal("0.012349")) == Decimal("0.01234")
    assert rules.round_price(Decimal("76000.019"), Side.BUY) == Decimal("76000.01")
    assert rules.round_price(Decimal("76000.011"), Side.SELL) == Decimal("76000.02")


def test_min_notional_rejected():
    rules = btcusdt_rules()
    with pytest.raises(OrderRejected):
        rules.validate_order(
            Side.BUY,
            rules_order_market(),
            quote_qty=Decimal("4"),
        )


def rules_order_market():
    from jev_trading.binance.types import OrderType

    return OrderType.MARKET


def test_qty_from_quote_respects_step():
    rules = btcusdt_rules()
    qty = qty_from_quote(Decimal("1000"), Decimal("76000.11"), rules)
    assert qty == Decimal("0.01315")
    rules.validate_order(
        Side.BUY,
        rules_order_market(),
        quantity=qty,
        ref_price=Decimal("76000.11"),
    )


def test_walk_book_consumes_two_levels():
    book = OrderBook(
        symbol="BTCUSDT",
        bids=(
            BookLevel(Decimal("100"), Decimal("1")),
            BookLevel(Decimal("99"), Decimal("2")),
        ),
        asks=(
            BookLevel(Decimal("101"), Decimal("0.4")),
            BookLevel(Decimal("102"), Decimal("5")),
        ),
    )
    walk = walk_book(book, Side.BUY, quantity=Decimal("1"))
    assert walk.levels_consumed == 2
    assert walk.filled_qty == Decimal("1")
    assert walk.avg_price == Decimal("101.6")
    assert walk.exhausted is False


def test_walk_book_extra_slippage_worsens_buy():
    book = OrderBook(
        symbol="BTCUSDT",
        bids=(BookLevel(Decimal("100"), Decimal("10")),),
        asks=(BookLevel(Decimal("101"), Decimal("10")),),
    )
    raw = walk_book(book, Side.BUY, quote_qty=Decimal("101"))
    slipped = walk_book(book, Side.BUY, quote_qty=Decimal("101"), extra_slippage_bps=10)
    assert slipped.avg_price > raw.avg_price
    assert slipped.filled_qty < raw.filled_qty
    assert slipped.filled_quote <= Decimal("101")


def test_insufficient_liquidity():
    book = OrderBook(
        symbol="BTCUSDT",
        bids=(),
        asks=(BookLevel(Decimal("101"), Decimal("0.001")),),
    )
    with pytest.raises(InsufficientLiquidity):
        from jev_trading.binance.book import require_full_fill

        require_full_fill(walk_book(book, Side.BUY, quantity=Decimal("1")))
