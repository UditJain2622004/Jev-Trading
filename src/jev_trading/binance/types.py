from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping


def D(value: Decimal | int | float | str) -> Decimal:
    """Convert to Decimal without float binary artifacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class Liquidity(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class EnvName(str, Enum):
    LIVE = "live"
    DEMO = "demo"
    TESTNET = "testnet"


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    last_update_id: int | None = None

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price

    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        spread = self.spread
        if mid is None or spread is None or mid == 0:
            return None
        return (spread / mid) * Decimal(10000)


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    update_id: int | None = None

    def mid(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal(2)

    def spread_bps(self) -> Decimal:
        mid = self.mid()
        if mid == 0:
            return Decimal(0)
        return ((self.ask_price - self.bid_price) / mid) * Decimal(10000)


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    taker_buy_base: Decimal
    taker_buy_quote: Decimal
    is_closed: bool = True


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    agg_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    timestamp: int
    is_buyer_maker: bool


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    quote_qty: Decimal
    fee: Decimal
    fee_asset: str
    liquidity: Liquidity
    order_id: str
    timestamp_ms: int
    extra_slippage_bps: Decimal = Decimal(0)
    levels_consumed: int = 0


@dataclass(frozen=True, slots=True)
class FeePayment:
    asset: str
    amount: Decimal
    rate: Decimal
    liquidity: Liquidity


def levels_from_raw(raw: list[list[str]] | tuple[list[str], ...]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=D(price), quantity=D(qty)) for price, qty in raw)


def book_from_depth(symbol: str, payload: Mapping[str, object]) -> OrderBook:
    return OrderBook(
        symbol=symbol.upper(),
        bids=levels_from_raw(payload["bids"]),  # type: ignore[arg-type]
        asks=levels_from_raw(payload["asks"]),  # type: ignore[arg-type]
        last_update_id=int(payload["lastUpdateId"]) if "lastUpdateId" in payload else None,
    )
