from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

from jev_trading.binance.endpoints import LIVE, BinanceEndpoints, combined_stream_url
from jev_trading.binance.types import BookTicker, D, OrderBook, book_from_depth


def book_ticker_stream(symbol: str) -> str:
    return f"{symbol.lower()}@bookTicker"


def partial_depth_stream(symbol: str, levels: int = 20, interval_ms: int = 100) -> str:
    if levels not in {5, 10, 20}:
        raise ValueError("Binance partial depth levels must be 5, 10, or 20")
    suffix = f"@depth{levels}"
    if interval_ms == 100:
        suffix += "@100ms"
    elif interval_ms != 1000:
        raise ValueError("interval_ms must be 100 or 1000")
    return f"{symbol.lower()}{suffix}"


def agg_trade_stream(symbol: str) -> str:
    return f"{symbol.lower()}@aggTrade"


def parse_book_ticker(payload: dict[str, Any]) -> BookTicker:
    data = payload.get("data", payload)
    return BookTicker(
        symbol=str(data["s"]),
        bid_price=D(data["b"]),
        bid_qty=D(data["B"]),
        ask_price=D(data["a"]),
        ask_qty=D(data["A"]),
        update_id=int(data["u"]) if "u" in data else None,
    )


def parse_partial_depth(symbol: str, payload: dict[str, Any]) -> OrderBook:
    data = payload.get("data", payload)
    return book_from_depth(symbol.upper(), data)


async def iter_combined_stream(
    streams: Iterable[str],
    *,
    env: BinanceEndpoints = LIVE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield combined-stream messages. Requires optional extra: websockets."""
    try:
        import websockets
    except ImportError as exc:
        raise ImportError("Install stream support with: pip install 'jev-trading[streams]'") from exc

    url = combined_stream_url(env, list(streams))
    async with websockets.connect(url, ping_interval=20) as ws:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            yield json.loads(raw)
