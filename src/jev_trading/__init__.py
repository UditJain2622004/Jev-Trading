"""Helpers for a live-like Binance paper environment.

The trading strategy and Jev wiring live elsewhere. This package is the
exchange/paper layer those pieces can call later.
"""

from jev_trading.binance import (
    DEMO,
    LIVE,
    TESTNET,
    BinanceRestClient,
    FeeSchedule,
    MarketClient,
    PaperBroker,
    RoundTripEconomics,
    SymbolRules,
    TradingClient,
    VIP0,
    VIP0_BNB,
    walk_book,
)

__all__ = [
    "DEMO",
    "LIVE",
    "TESTNET",
    "BinanceRestClient",
    "FeeSchedule",
    "MarketClient",
    "PaperBroker",
    "RoundTripEconomics",
    "SymbolRules",
    "TradingClient",
    "VIP0",
    "VIP0_BNB",
    "walk_book",
]
