from __future__ import annotations

from decimal import Decimal

from jev_trading.binance.rules import SymbolRules


def btcusdt_rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        status="TRADING",
        base_asset="BTC",
        quote_asset="USDT",
        base_precision=8,
        quote_precision=8,
        tick_size=Decimal("0.01"),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        max_qty=Decimal("9000"),
        market_step_size=None,
        market_min_qty=Decimal("0"),
        market_max_qty=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("9000000"),
        apply_min_notional_to_market=True,
        quote_order_qty_market_allowed=True,
    )
