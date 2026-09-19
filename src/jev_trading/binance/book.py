from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jev_trading.binance.errors import InsufficientLiquidity
from jev_trading.binance.types import D, OrderBook, Side


@dataclass(frozen=True, slots=True)
class BookWalk:
    side: Side
    filled_qty: Decimal
    filled_quote: Decimal
    avg_price: Decimal
    levels_consumed: int
    exhausted: bool
    remaining_qty: Decimal
    remaining_quote: Decimal

    @property
    def vwap(self) -> Decimal:
        return self.avg_price


def walk_book(
    book: OrderBook,
    side: Side,
    *,
    quantity: Decimal | None = None,
    quote_qty: Decimal | None = None,
    limit_price: Decimal | None = None,
    extra_slippage_bps: Decimal | str | float = 0,
) -> BookWalk:
    """Consume the visible book the way a taker market order would.

    BUY walks asks. SELL walks bids. Optional extra_slippage_bps is applied
    after VWAP to stand in for latency / adverse selection the snapshot
    cannot see.
    """
    if (quantity is None) == (quote_qty is None):
        raise ValueError("Provide exactly one of quantity or quote_qty")
    remaining_qty = D(quantity) if quantity is not None else None
    remaining_quote = D(quote_qty) if quote_qty is not None else None
    levels = book.asks if side is Side.BUY else book.bids
    filled_qty = Decimal(0)
    filled_quote = Decimal(0)
    consumed = 0

    for level in levels:
        if limit_price is not None:
            if side is Side.BUY and level.price > limit_price:
                break
            if side is Side.SELL and level.price < limit_price:
                break
        available = level.quantity
        if available <= 0:
            continue
        if remaining_qty is not None:
            take = min(available, remaining_qty)
        else:
            take = min(available, remaining_quote / level.price)
        if take <= 0:
            break
        cost = take * level.price
        filled_qty += take
        filled_quote += cost
        consumed += 1
        if remaining_qty is not None:
            remaining_qty -= take
            if remaining_qty <= 0:
                remaining_qty = Decimal(0)
                break
        else:
            remaining_quote -= cost
            if remaining_quote <= 0:
                remaining_quote = Decimal(0)
                break

    exhausted = False
    if remaining_qty is not None and remaining_qty > 0:
        exhausted = True
    if remaining_quote is not None and remaining_quote > 0:
        exhausted = True
    if filled_qty <= 0:
        raise InsufficientLiquidity(f"No fillable liquidity on {book.symbol} {side.value} side")

    avg = filled_quote / filled_qty
    slip = D(extra_slippage_bps)
    qty_constrained = quantity is not None
    if slip:
        if side is Side.BUY:
            avg *= Decimal(1) + slip / Decimal(10000)
        else:
            avg *= Decimal(1) - slip / Decimal(10000)
        if qty_constrained or side is Side.SELL:
            filled_quote = avg * filled_qty
        else:
            filled_qty = filled_quote / avg

    return BookWalk(
        side=side,
        filled_qty=filled_qty,
        filled_quote=filled_quote,
        avg_price=avg,
        levels_consumed=consumed,
        exhausted=exhausted,
        remaining_qty=remaining_qty if remaining_qty is not None else Decimal(0),
        remaining_quote=remaining_quote if remaining_quote is not None else Decimal(0),
    )


def require_full_fill(walk: BookWalk) -> BookWalk:
    if walk.exhausted:
        raise InsufficientLiquidity(
            f"Book exhausted after {walk.filled_qty} @ {walk.avg_price} "
            f"(remaining qty={walk.remaining_qty}, remaining quote={walk.remaining_quote})"
        )
    return walk
