from __future__ import annotations

import itertools
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from jev_trading.binance.book import require_full_fill, walk_book
from jev_trading.binance.errors import InsufficientBalance, OrderRejected
from jev_trading.binance.fees import FeeSchedule, apply_receive_fee
from jev_trading.binance.market import MarketClient
from jev_trading.binance.rules import SymbolRules, qty_from_quote
from jev_trading.binance.types import (
    AggTrade,
    D,
    Fill,
    Kline,
    Liquidity,
    OrderBook,
    OrderType,
    Side,
    TimeInForce,
)


@dataclass(slots=True)
class PaperOrder:
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    filled_qty: Decimal = Decimal(0)
    price: Decimal | None = None
    quote_qty: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    status: str = "NEW"
    created_ms: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_qty


@dataclass(slots=True)
class PaperBroker:
    """Local live-like paper account.

    Uses production market snapshots you pass in. It does **not** send orders
    to Binance. Fees, lot-size, min-notional, book impact, and extra latency
    slippage are applied locally so a $1-target strategy can be scored before
    Demo or live wiring.
    """

    quote_asset: str = "USDT"
    starting_quote: Decimal | str | float = 1000
    fees: FeeSchedule = field(default_factory=FeeSchedule.vip0)
    extra_slippage_bps: Decimal | str | float = Decimal(1)
    allow_partial_market: bool = False
    maker_fill: str = "through"
    latency_ms: int = 50
    sleep_on_latency: bool = False
    clock_ms: int | None = None
    _balances: dict[str, Decimal] = field(default_factory=dict, init=False)
    _locked: dict[str, Decimal] = field(default_factory=dict, init=False)
    _orders: dict[str, PaperOrder] = field(default_factory=dict, init=False)
    _fills: list[Fill] = field(default_factory=list, init=False)
    _id: itertools.count = field(default_factory=lambda: itertools.count(1), init=False)

    def __post_init__(self) -> None:
        self._balances = defaultdict(lambda: Decimal(0))
        self._locked = defaultdict(lambda: Decimal(0))
        self._balances[self.quote_asset] = D(self.starting_quote)
        self.extra_slippage_bps = D(self.extra_slippage_bps)
        if self.maker_fill not in {"through", "touch"}:
            raise ValueError("maker_fill must be 'through' or 'touch'")

    def now_ms(self) -> int:
        return self.clock_ms if self.clock_ms is not None else int(time.time() * 1000)

    def balance(self, asset: str) -> Decimal:
        return self._balances[asset.upper()]

    def balances(self) -> dict[str, Decimal]:
        return {asset: qty for asset, qty in self._balances.items() if qty != 0}

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def open_orders(self, symbol: str | None = None) -> list[PaperOrder]:
        orders = [order for order in self._orders.values() if order.status in {"NEW", "PARTIALLY_FILLED"}]
        if symbol:
            orders = [order for order in orders if order.symbol == symbol.upper()]
        return orders

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal(0)
        for asset, qty in self._balances.items():
            if asset == self.quote_asset:
                total += qty
            elif asset in marks:
                total += qty * marks[asset]
        return total

    def market(
        self,
        rules: SymbolRules,
        book: OrderBook,
        side: Side,
        *,
        quantity: Decimal | str | float | None = None,
        quote_qty: Decimal | str | float | None = None,
        liquidity: Liquidity = Liquidity.TAKER,
    ) -> Fill:
        if self.sleep_on_latency and self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        qty = D(quantity) if quantity is not None else None
        quote = D(quote_qty) if quote_qty is not None else None
        if qty is not None:
            qty = rules.round_qty(qty, market=True)
        if quote is not None:
            quote = rules.round_quote(quote)
        ref = book.best_ask.price if side is Side.BUY and book.best_ask else None
        if side is Side.SELL and book.best_bid:
            ref = book.best_bid.price
        rules.validate_order(
            side,
            OrderType.MARKET,
            quantity=qty,
            quote_qty=quote,
            ref_price=ref,
        )
        walk = walk_book(
            book,
            side,
            quantity=qty,
            quote_qty=quote,
            extra_slippage_bps=self.extra_slippage_bps,
        )
        if not self.allow_partial_market:
            require_full_fill(walk)
        filled_qty = rules.round_qty(walk.filled_qty, market=True)
        if filled_qty <= 0:
            raise OrderRejected("Rounded fill quantity is zero")
        avg = walk.avg_price
        filled_quote = filled_qty * avg
        if side is Side.BUY and quote is not None and filled_quote > quote:
            filled_qty = rules.round_qty(quote / avg, market=True)
            filled_quote = filled_qty * avg
        return self._settle(
            rules,
            side,
            filled_qty,
            filled_quote,
            avg,
            liquidity,
            OrderType.MARKET,
            walk.levels_consumed,
        )

    def limit(
        self,
        rules: SymbolRules,
        book: OrderBook,
        side: Side,
        quantity: Decimal | str | float,
        price: Decimal | str | float,
        *,
        time_in_force: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
    ) -> PaperOrder | Fill:
        qty = rules.round_qty(D(quantity), market=False)
        px = rules.round_price(D(price), side)
        rules.validate_order(side, OrderType.LIMIT, quantity=qty, price=px)
        crosses = _crosses_spread(side, px, book)
        if post_only and crosses:
            raise OrderRejected("LIMIT_MAKER would immediately take liquidity")
        if crosses:
            walk = walk_book(
                book,
                side,
                quantity=qty,
                limit_price=px,
                extra_slippage_bps=self.extra_slippage_bps,
            )
            if time_in_force is TimeInForce.FOK and walk.exhausted:
                raise OrderRejected("FOK limit could not fill in full")
            fill_qty = rules.round_qty(min(qty, walk.filled_qty))
            if fill_qty <= 0:
                raise OrderRejected("Crossing limit rounded to zero fill")
            fill = self._settle(
                rules,
                side,
                fill_qty,
                fill_qty * walk.avg_price,
                walk.avg_price,
                Liquidity.TAKER,
                OrderType.LIMIT,
                walk.levels_consumed,
            )
            remaining = qty - fill_qty
            if remaining <= 0 or time_in_force is TimeInForce.IOC:
                return fill
            qty = remaining
        order = PaperOrder(
            order_id=str(next(self._id)),
            symbol=rules.symbol,
            side=side,
            order_type=OrderType.LIMIT_MAKER if post_only else OrderType.LIMIT,
            quantity=qty,
            price=px,
            time_in_force=time_in_force,
            created_ms=self.now_ms(),
        )
        self._lock_for_limit(rules, order)
        self._orders[order.order_id] = order
        return order

    def cancel(self, order_id: str, rules: SymbolRules) -> PaperOrder:
        order = self._orders[order_id]
        if order.status not in {"NEW", "PARTIALLY_FILLED"}:
            raise OrderRejected(f"Cannot cancel {order.status} order")
        self._unlock_remaining(rules, order)
        order.status = "CANCELED"
        return order

    def on_book(self, rules: SymbolRules, book: OrderBook) -> list[Fill]:
        fills: list[Fill] = []
        for order in self.open_orders(rules.symbol):
            if order.price is None:
                continue
            if _crosses_spread(order.side, order.price, book):
                fills.extend(self._fill_resting(rules, order, book.best_ask.price if order.side is Side.BUY else book.best_bid.price, order.remaining, Liquidity.TAKER))
        return fills

    def on_trade(self, rules: SymbolRules, trade: AggTrade) -> list[Fill]:
        fills: list[Fill] = []
        tick = rules.tick_size
        for order in self.open_orders(trade.symbol):
            if order.price is None:
                continue
            if not _maker_hit(order.side, order.price, trade.price, tick, self.maker_fill):
                continue
            take = min(order.remaining, trade.quantity)
            fills.extend(self._fill_resting(rules, order, order.price, take, Liquidity.MAKER))
        return fills

    def on_kline(self, rules: SymbolRules, kline: Kline) -> list[Fill]:
        """Conservative maker fills: buy if bar trades through the bid, sell through the ask."""
        fills: list[Fill] = []
        tick = rules.tick_size
        for order in self.open_orders(kline.symbol):
            if order.price is None:
                continue
            hit_price = kline.low if order.side is Side.BUY else kline.high
            if not _maker_hit(order.side, order.price, hit_price, tick, self.maker_fill):
                continue
            fills.extend(self._fill_resting(rules, order, order.price, order.remaining, Liquidity.MAKER))
        return fills

    def _fill_resting(
        self,
        rules: SymbolRules,
        order: PaperOrder,
        price: Decimal,
        quantity: Decimal,
        liquidity: Liquidity,
    ) -> list[Fill]:
        qty = min(order.remaining, quantity)
        qty = rules.round_qty(qty)
        if qty <= 0:
            return []
        self._unlock_fill(rules, order, qty, price)
        fill = self._settle(
            rules,
            order.side,
            qty,
            qty * price,
            price,
            liquidity,
            order.order_type,
            1,
            order_id=order.order_id,
        )
        order.filled_qty += qty
        order.status = "FILLED" if order.remaining <= 0 else "PARTIALLY_FILLED"
        return [fill]

    def _settle(
        self,
        rules: SymbolRules,
        side: Side,
        base_qty: Decimal,
        quote_qty: Decimal,
        price: Decimal,
        liquidity: Liquidity,
        order_type: OrderType,
        levels: int,
        order_id: str | None = None,
    ) -> Fill:
        rate = self.fees.rate(liquidity)
        base_delta, quote_delta, fee, fee_asset = apply_receive_fee(
            side,
            base_qty,
            quote_qty,
            rate,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
            base_precision=rules.base_precision,
            quote_precision=rules.quote_precision,
        )
        if side is Side.BUY:
            needed = -quote_delta
            if self._balances[rules.quote_asset] < needed:
                raise InsufficientBalance(
                    f"Need {needed} {rules.quote_asset}, have {self._balances[rules.quote_asset]}"
                )
        else:
            needed = -base_delta
            if self._balances[rules.base_asset] < needed:
                raise InsufficientBalance(
                    f"Need {needed} {rules.base_asset}, have {self._balances[rules.base_asset]}"
                )
        self._balances[rules.base_asset] += base_delta
        self._balances[rules.quote_asset] += quote_delta
        fill = Fill(
            symbol=rules.symbol,
            side=side,
            quantity=base_qty,
            price=price,
            quote_qty=quote_qty,
            fee=fee,
            fee_asset=fee_asset,
            liquidity=liquidity,
            order_id=order_id or str(next(self._id)),
            timestamp_ms=self.now_ms(),
            extra_slippage_bps=self.extra_slippage_bps,
            levels_consumed=levels,
        )
        self._fills.append(fill)
        return fill

    def _lock_for_limit(self, rules: SymbolRules, order: PaperOrder) -> None:
        if order.price is None:
            raise OrderRejected("Limit order missing price")
        if order.side is Side.BUY:
            needed = order.quantity * order.price
            asset = rules.quote_asset
        else:
            needed = order.quantity
            asset = rules.base_asset
        if self._balances[asset] < needed:
            raise InsufficientBalance(f"Need {needed} {asset} to rest limit")
        self._balances[asset] -= needed
        self._locked[asset] += needed

    def _unlock_remaining(self, rules: SymbolRules, order: PaperOrder) -> None:
        if order.price is None:
            return
        remaining = order.remaining
        if remaining <= 0:
            return
        if order.side is Side.BUY:
            amount = remaining * order.price
            asset = rules.quote_asset
        else:
            amount = remaining
            asset = rules.base_asset
        self._locked[asset] -= amount
        self._balances[asset] += amount

    def _unlock_fill(self, rules: SymbolRules, order: PaperOrder, qty: Decimal, price: Decimal) -> None:
        if order.side is Side.BUY:
            amount = qty * (order.price or price)
            asset = rules.quote_asset
        else:
            amount = qty
            asset = rules.base_asset
        self._locked[asset] -= amount
        self._balances[asset] += amount


def _crosses_spread(side: Side, price: Decimal, book: OrderBook) -> bool:
    if side is Side.BUY:
        return book.best_ask is not None and price >= book.best_ask.price
    return book.best_bid is not None and price <= book.best_bid.price


def _maker_hit(side: Side, limit_price: Decimal, trade_price: Decimal, tick: Decimal, mode: str) -> bool:
    if mode == "touch":
        if side is Side.BUY:
            return trade_price <= limit_price
        return trade_price >= limit_price
    if side is Side.BUY:
        return trade_price <= limit_price - tick
    return trade_price >= limit_price + tick


def live_market_fill(
    broker: PaperBroker,
    market: MarketClient,
    symbol: str,
    side: Side,
    *,
    quantity: Decimal | str | float | None = None,
    quote_qty: Decimal | str | float | None = None,
    depth_limit: int = 100,
) -> Fill:
    """Paper-fill a market order against the live production book."""
    rules = market.symbol_rules(symbol)
    book = market.depth(symbol, limit=depth_limit)
    return broker.market(rules, book, side, quantity=quantity, quote_qty=quote_qty)


def clip_from_quote(rules: SymbolRules, quote_qty: Decimal, ref_price: Decimal) -> Decimal:
    return qty_from_quote(quote_qty, ref_price, rules, market=True)
