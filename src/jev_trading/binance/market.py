from __future__ import annotations

from typing import Any, Iterator

from jev_trading.binance.endpoints import LIVE
from jev_trading.binance.rest import BinanceRestClient
from jev_trading.binance.rules import SymbolRules
from jev_trading.binance.types import AggTrade, BookTicker, D, Kline, OrderBook, book_from_depth


class MarketClient:
    """Public market-data helpers.

    Always defaults to production. Paper fills should see live prices even
    when Demo/Testnet is used for signed order plumbing.
    """

    def __init__(self, client: BinanceRestClient | None = None) -> None:
        self.client = client or BinanceRestClient(env=LIVE)
        self._rules: dict[str, SymbolRules] = {}

    def ping(self) -> None:
        self.client.ping()

    def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self.client.public("GET", "/api/v3/exchangeInfo", params)

    def symbol_rules(self, symbol: str, *, refresh: bool = False) -> SymbolRules:
        key = symbol.upper()
        if refresh or key not in self._rules:
            info = self.exchange_info(key)
            self._rules[key] = SymbolRules.from_exchange_info_symbol(info["symbols"][0])
        return self._rules[key]

    def depth(self, symbol: str, limit: int = 100) -> OrderBook:
        payload = self.client.public(
            "GET",
            "/api/v3/depth",
            {"symbol": symbol.upper(), "limit": limit},
        )
        return book_from_depth(symbol.upper(), payload)

    def book_ticker(self, symbol: str) -> BookTicker:
        payload = self.client.public("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol.upper()})
        return BookTicker(
            symbol=payload["symbol"],
            bid_price=D(payload["bidPrice"]),
            bid_qty=D(payload["bidQty"]),
            ask_price=D(payload["askPrice"]),
            ask_qty=D(payload["askQty"]),
        )

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> list[Kline]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        rows = self.client.public("GET", "/api/v3/klines", params)
        return [parse_kline(symbol, interval, row) for row in rows]

    def iter_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        limit: int = 1000,
    ) -> Iterator[Kline]:
        cursor = start_time
        while cursor < end_time:
            batch = self.klines(
                symbol,
                interval,
                start_time=cursor,
                end_time=end_time,
                limit=limit,
            )
            if not batch:
                break
            for kline in batch:
                if kline.open_time > end_time:
                    return
                yield kline
            next_cursor = batch[-1].open_time + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

    def agg_trades(
        self,
        symbol: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[AggTrade]:
        params: dict[str, Any] = {"symbol": symbol.upper(), "limit": min(limit, 1000)}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if from_id is not None:
            params["fromId"] = from_id
        rows = self.client.public("GET", "/api/v3/aggTrades", params)
        return [parse_agg_trade(symbol, row) for row in rows]


def parse_kline(symbol: str, interval: str, row: list[object]) -> Kline:
    return Kline(
        symbol=symbol.upper(),
        interval=interval,
        open_time=int(row[0]),
        open=D(row[1]),
        high=D(row[2]),
        low=D(row[3]),
        close=D(row[4]),
        volume=D(row[5]),
        close_time=int(row[6]),
        quote_volume=D(row[7]),
        trades=int(row[8]),
        taker_buy_base=D(row[9]),
        taker_buy_quote=D(row[10]),
    )


def parse_agg_trade(symbol: str, row: dict[str, object]) -> AggTrade:
    return AggTrade(
        symbol=symbol.upper(),
        agg_id=int(row["a"]),
        price=D(row["p"]),
        quantity=D(row["q"]),
        first_trade_id=int(row["f"]),
        last_trade_id=int(row["l"]),
        timestamp=int(row["T"]),
        is_buyer_maker=bool(row["m"]),
    )
