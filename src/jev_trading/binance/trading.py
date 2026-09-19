from __future__ import annotations

from typing import Any

from jev_trading.binance.endpoints import DEMO, BinanceEndpoints
from jev_trading.binance.errors import LiveTradingDisabled
from jev_trading.binance.rest import BinanceRestClient
from jev_trading.binance.types import EnvName, OrderType, Side, TimeInForce


class TradingClient:
    """Signed Spot trading helpers for Demo, Testnet, or (explicit) live.

    PaperBroker is the default research environment. Use this client when you
    want Binance's own Demo matching engine.

    Live orders are refused unless allow_live=True on the client or call.
    """

    def __init__(
        self,
        client: BinanceRestClient | None = None,
        *,
        env: str | EnvName | BinanceEndpoints | None = None,
        allow_live: bool = False,
    ) -> None:
        if client is not None:
            self.client = client
        else:
            self.client = BinanceRestClient(env=env or DEMO)
        self.allow_live = allow_live

    @property
    def env_name(self) -> EnvName:
        return self.client.endpoints.name

    def account(self) -> dict[str, Any]:
        return self.client.signed("GET", "/api/v3/account")

    def commission(self, symbol: str) -> dict[str, Any]:
        return self.client.signed("GET", "/api/v3/account/commission", {"symbol": symbol.upper()})

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self.client.signed("GET", "/api/v3/openOrders", params)

    def test_order(
        self,
        symbol: str,
        side: Side | str,
        order_type: OrderType | str,
        *,
        quantity: str | None = None,
        quote_order_qty: str | None = None,
        price: str | None = None,
        time_in_force: TimeInForce | str | None = None,
        compute_commission_rates: bool = True,
    ) -> dict[str, Any]:
        """Validate an order without executing it. Works on live with a key."""
        params = _order_params(
            symbol,
            side,
            order_type,
            quantity=quantity,
            quote_order_qty=quote_order_qty,
            price=price,
            time_in_force=time_in_force,
        )
        if compute_commission_rates:
            params["computeCommissionRates"] = "true"
        return self.client.signed("POST", "/api/v3/order/test", params)

    def new_order(
        self,
        symbol: str,
        side: Side | str,
        order_type: OrderType | str,
        *,
        quantity: str | None = None,
        quote_order_qty: str | None = None,
        price: str | None = None,
        time_in_force: TimeInForce | str | None = None,
        new_client_order_id: str | None = None,
        allow_live: bool | None = None,
    ) -> dict[str, Any]:
        live_ok = self.allow_live if allow_live is None else allow_live
        if self.env_name is EnvName.LIVE and not live_ok:
            raise LiveTradingDisabled(
                "Refusing to place a live Binance order. Use PaperBroker, "
                "BINANCE_ENV=demo, or pass allow_live=True."
            )
        params = _order_params(
            symbol,
            side,
            order_type,
            quantity=quantity,
            quote_order_qty=quote_order_qty,
            price=price,
            time_in_force=time_in_force,
        )
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        return self.client.signed("POST", "/api/v3/order", params)

    def cancel_order(self, symbol: str, order_id: int, *, allow_live: bool | None = None) -> dict[str, Any]:
        live_ok = self.allow_live if allow_live is None else allow_live
        if self.env_name is EnvName.LIVE and not live_ok:
            raise LiveTradingDisabled("Refusing to cancel a live Binance order.")
        return self.client.signed(
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol.upper(), "orderId": order_id},
        )


def _order_params(
    symbol: str,
    side: Side | str,
    order_type: OrderType | str,
    *,
    quantity: str | None,
    quote_order_qty: str | None,
    price: str | None,
    time_in_force: TimeInForce | str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side.value if isinstance(side, Side) else str(side).upper(),
        "type": order_type.value if isinstance(order_type, OrderType) else str(order_type).upper(),
    }
    if quantity is not None:
        params["quantity"] = quantity
    if quote_order_qty is not None:
        params["quoteOrderQty"] = quote_order_qty
    if price is not None:
        params["price"] = price
    if time_in_force is not None:
        params["timeInForce"] = (
            time_in_force.value if isinstance(time_in_force, TimeInForce) else str(time_in_force).upper()
        )
    return params
