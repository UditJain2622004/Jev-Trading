from __future__ import annotations


class BinanceError(Exception):
    """Base error for Binance helpers."""


class BinanceAPIError(BinanceError):
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"Binance HTTP {status}: {payload}")


class OrderRejected(BinanceError):
    """An order failed local exchange-rule or paper-broker checks."""


class InsufficientBalance(OrderRejected):
    pass


class InsufficientLiquidity(OrderRejected):
    pass


class LiveTradingDisabled(BinanceError):
    """Signed live orders are blocked unless explicitly enabled."""
