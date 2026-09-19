from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Mapping

from jev_trading.binance.errors import OrderRejected
from jev_trading.binance.types import D, OrderType, Side


def _filter_map(filters: list[Mapping[str, str]]) -> dict[str, Mapping[str, str]]:
    return {item["filterType"]: item for item in filters}


def _step_decimals(step: Decimal) -> Decimal:
    return step.normalize()


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    base_precision: int
    quote_precision: int
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal | None
    market_min_qty: Decimal | None
    market_max_qty: Decimal | None
    min_notional: Decimal
    max_notional: Decimal | None
    apply_min_notional_to_market: bool
    quote_order_qty_market_allowed: bool

    @classmethod
    def from_exchange_info_symbol(cls, payload: Mapping[str, object]) -> SymbolRules:
        filters = _filter_map(payload["filters"])  # type: ignore[arg-type]
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
        if notional is None:
            raise ValueError(f"{payload['symbol']} is missing NOTIONAL/MIN_NOTIONAL filters")
        market_lot = filters.get("MARKET_LOT_SIZE")
        min_notional = D(notional.get("minNotional", "0"))
        max_raw = notional.get("maxNotional")
        apply_min = notional.get("applyMinToMarket")
        if apply_min is None:
            apply_min = notional.get("applyToMarket", True)
        return cls(
            symbol=str(payload["symbol"]).upper(),
            status=str(payload["status"]),
            base_asset=str(payload["baseAsset"]),
            quote_asset=str(payload["quoteAsset"]),
            base_precision=int(payload.get("baseAssetPrecision", 8)),
            quote_precision=int(payload.get("quoteAssetPrecision", 8)),
            tick_size=D(price["tickSize"]),
            min_price=D(price["minPrice"]),
            max_price=D(price["maxPrice"]),
            step_size=D(lot["stepSize"]),
            min_qty=D(lot["minQty"]),
            max_qty=D(lot["maxQty"]),
            market_step_size=D(market_lot["stepSize"]) if market_lot and D(market_lot["stepSize"]) > 0 else None,
            market_min_qty=D(market_lot["minQty"]) if market_lot else None,
            market_max_qty=D(market_lot["maxQty"]) if market_lot else None,
            min_notional=min_notional,
            max_notional=D(max_raw) if max_raw is not None else None,
            apply_min_notional_to_market=bool(apply_min),
            quote_order_qty_market_allowed=bool(payload.get("quoteOrderQtyMarketAllowed", True)),
        )

    def round_price(self, price: Decimal | str | float, side: Side) -> Decimal:
        """Round a limit price to tick size.

        Buys round down so we never bid above the intended price. Sells round up
        so we never offer below it.
        """
        rounding = ROUND_FLOOR if side is Side.BUY else ROUND_CEILING
        return _align_to_step(D(price), self.tick_size, rounding)

    def round_qty(self, qty: Decimal | str | float, *, market: bool = False) -> Decimal:
        step = self.market_step_size if market and self.market_step_size else self.step_size
        return _align_to_step(D(qty), step, ROUND_DOWN)

    def round_quote(self, quote_qty: Decimal | str | float) -> Decimal:
        quantum = Decimal("1").scaleb(-self.quote_precision)
        return D(quote_qty).quantize(quantum, rounding=ROUND_DOWN)

    def validate_order(
        self,
        side: Side,
        order_type: OrderType,
        *,
        quantity: Decimal | None = None,
        quote_qty: Decimal | None = None,
        price: Decimal | None = None,
        ref_price: Decimal | None = None,
    ) -> None:
        if self.status != "TRADING":
            raise OrderRejected(f"{self.symbol} status is {self.status}, not TRADING")
        is_market = order_type is OrderType.MARKET
        if is_market and quote_qty is not None:
            if not self.quote_order_qty_market_allowed:
                raise OrderRejected(f"{self.symbol} does not allow market quoteOrderQty")
            if quote_qty <= 0:
                raise OrderRejected("quote_qty must be positive")
            if self.apply_min_notional_to_market and quote_qty < self.min_notional:
                raise OrderRejected(
                    f"quote_qty {quote_qty} is below minNotional {self.min_notional} on {self.symbol}"
                )
            return
        if quantity is None or quantity <= 0:
            raise OrderRejected("quantity must be positive")
        min_qty = self.market_min_qty if is_market and self.market_min_qty is not None else self.min_qty
        max_qty = self.market_max_qty if is_market and self.market_max_qty is not None else self.max_qty
        if quantity < min_qty:
            raise OrderRejected(f"quantity {quantity} < minQty {min_qty}")
        if max_qty is not None and quantity > max_qty:
            raise OrderRejected(f"quantity {quantity} > maxQty {max_qty}")
        step = self.market_step_size if is_market and self.market_step_size else self.step_size
        if not _on_step(quantity, step):
            raise OrderRejected(f"quantity {quantity} is not aligned to stepSize {step}")
        check_price = price
        if check_price is None:
            check_price = ref_price
        if check_price is None:
            return
        if not is_market:
            if check_price < self.min_price or check_price > self.max_price:
                raise OrderRejected(f"price {check_price} outside [{self.min_price}, {self.max_price}]")
            if not _on_step(check_price, self.tick_size):
                raise OrderRejected(f"price {check_price} is not aligned to tickSize {self.tick_size}")
        notional = quantity * check_price
        apply_min = True if not is_market else self.apply_min_notional_to_market
        if apply_min and notional < self.min_notional:
            raise OrderRejected(
                f"notional {notional} is below minNotional {self.min_notional} on {self.symbol}"
            )
        if self.max_notional is not None and notional > self.max_notional:
            raise OrderRejected(f"notional {notional} exceeds maxNotional {self.max_notional}")


def _align_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    steps = (value / step).to_integral_value(rounding=rounding)
    aligned = steps * step
    return aligned.quantize(_step_decimals(step), rounding=ROUND_HALF_UP)


def _on_step(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return True
    return value % step == 0


def qty_from_quote(quote_qty: Decimal, price: Decimal, rules: SymbolRules, *, market: bool = True) -> Decimal:
    raw = quote_qty / price
    return rules.round_qty(raw, market=market)
