from __future__ import annotations

from decimal import Decimal

from jev_trading.binance.types import BookLevel, D, Kline, OrderBook, Side


def pessimistic_taker_price(
    kline: Kline,
    side: Side,
    *,
    extra_slippage_bps: Decimal | str | float = 0,
) -> Decimal:
    """Worst-in-bar taker fill: buy the high, sell the low, plus extra bps.

    Kline backtests cannot see the book. This is intentionally harsh so a
    $1-edge idea has to survive a pessimistic replay before we trust it.
    """
    px = kline.high if side is Side.BUY else kline.low
    slip = D(extra_slippage_bps)
    if slip:
        if side is Side.BUY:
            px *= Decimal(1) + slip / Decimal(10000)
        else:
            px *= Decimal(1) - slip / Decimal(10000)
    return px


def close_plus_spread_price(
    kline: Kline,
    side: Side,
    *,
    spread_bps: Decimal | str | float = 1,
    extra_slippage_bps: Decimal | str | float = 0,
) -> Decimal:
    """Fill at close, crossing half-spread plus extra latency slippage."""
    half = D(spread_bps) / Decimal(2)
    slip = D(extra_slippage_bps)
    adj = (half + slip) / Decimal(10000)
    if side is Side.BUY:
        return kline.close * (Decimal(1) + adj)
    return kline.close * (Decimal(1) - adj)


def synthetic_book_from_kline(
    kline: Kline,
    *,
    spread_bps: Decimal | str | float = 1,
    size: Decimal | str | float = 10,
) -> OrderBook:
    """Build a one-level book around close so PaperBroker can walk it."""
    mid = kline.close
    half = D(spread_bps) / Decimal(20000)
    bid = mid * (Decimal(1) - half)
    ask = mid * (Decimal(1) + half)
    qty = D(size)
    return OrderBook(
        symbol=kline.symbol,
        bids=(BookLevel(price=bid, quantity=qty),),
        asks=(BookLevel(price=ask, quantity=qty),),
        last_update_id=kline.close_time,
    )


def synthetic_book_from_trade(
    symbol: str,
    price: Decimal,
    quantity: Decimal,
    *,
    is_buyer_maker: bool,
    tick: Decimal,
) -> OrderBook:
    """Treat a public trade as the only available opposing liquidity."""
    if is_buyer_maker:
        bids = (BookLevel(price=price, quantity=quantity),)
        asks = (BookLevel(price=price + tick, quantity=Decimal(0)),)
    else:
        asks = (BookLevel(price=price, quantity=quantity),)
        bids = (BookLevel(price=price - tick, quantity=Decimal(0)),)
    return OrderBook(symbol=symbol.upper(), bids=bids, asks=asks)
