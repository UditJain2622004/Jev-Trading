from __future__ import annotations

import os
from dataclasses import dataclass

from jev_trading.binance.types import EnvName


@dataclass(frozen=True, slots=True)
class BinanceEndpoints:
    name: EnvName
    rest_base: str
    ws_stream_base: str
    ws_api_base: str


LIVE = BinanceEndpoints(
    name=EnvName.LIVE,
    rest_base="https://api.binance.com",
    ws_stream_base="wss://stream.binance.com:9443",
    ws_api_base="wss://ws-api.binance.com:443",
)

DEMO = BinanceEndpoints(
    name=EnvName.DEMO,
    rest_base="https://demo-api.binance.com",
    ws_stream_base="wss://demo-stream.binance.com",
    ws_api_base="wss://demo-ws-api.binance.com",
)

TESTNET = BinanceEndpoints(
    name=EnvName.TESTNET,
    rest_base="https://testnet.binance.vision",
    ws_stream_base="wss://stream.testnet.binance.vision",
    ws_api_base="wss://ws-api.testnet.binance.vision",
)

_ENVS = {
    "live": LIVE,
    "prod": LIVE,
    "production": LIVE,
    "demo": DEMO,
    "testnet": TESTNET,
    "spot-testnet": TESTNET,
}


def endpoints_for(name: str | EnvName | None = None) -> BinanceEndpoints:
    """Resolve live / demo / testnet URLs.

    Demo Mode is the official paper environment that tracks production
    features and has prices similar to live. Spot Testnet is a separate
    sandbox whose books are independent of production.
    """
    if name is None:
        name = os.getenv("BINANCE_ENV", "demo")
    if isinstance(name, EnvName):
        key = name.value
    else:
        key = name.strip().lower()
    try:
        env = _ENVS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown BINANCE_ENV {name!r}. Use live, demo, or testnet.") from exc

    rest_override = os.getenv("BINANCE_REST_BASE")
    ws_override = os.getenv("BINANCE_WS_STREAM_BASE")
    if rest_override or ws_override:
        return BinanceEndpoints(
            name=env.name,
            rest_base=rest_override or env.rest_base,
            ws_stream_base=ws_override or env.ws_stream_base,
            ws_api_base=env.ws_api_base,
        )
    return env


def combined_stream_url(env: BinanceEndpoints, streams: list[str]) -> str:
    joined = "/".join(stream.lower() for stream in streams)
    return f"{env.ws_stream_base}/stream?streams={joined}"


def raw_stream_url(env: BinanceEndpoints, stream: str) -> str:
    return f"{env.ws_stream_base}/ws/{stream.lower()}"
