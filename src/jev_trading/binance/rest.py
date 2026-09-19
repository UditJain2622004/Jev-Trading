from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from jev_trading.binance.endpoints import LIVE, BinanceEndpoints, endpoints_for
from jev_trading.binance.errors import BinanceAPIError
from jev_trading.binance.types import EnvName


class BinanceRestClient:
    """Thin signed/unsigned REST client for live, demo, or testnet.

    Market data should usually hit LIVE even when paper-trading. Demo and
    Testnet books are not the same as production; for a $1-edge hunt we
    want production prices and a local fill model.
    """

    def __init__(
        self,
        env: str | EnvName | BinanceEndpoints | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: float = 10.0,
        recv_window: int = 5000,
    ) -> None:
        if isinstance(env, BinanceEndpoints):
            self.endpoints = env
        else:
            self.endpoints = endpoints_for(env)
        self.api_key = api_key if api_key is not None else os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("BINANCE_API_SECRET", "")
        self.recv_window = recv_window
        self._time_offset_ms = 0
        self._http = httpx.Client(base_url=self.endpoints.rest_base, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BinanceRestClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ping(self) -> dict[str, Any]:
        return self.public("GET", "/api/v3/ping")

    def server_time(self) -> int:
        return int(self.public("GET", "/api/v3/time")["serverTime"])

    def sync_time(self) -> int:
        local = int(time.time() * 1000)
        server = self.server_time()
        self._time_offset_ms = server - local
        return self._time_offset_ms

    def timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def public(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._request(method, path, params or {}, signed=False)

    def signed(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self.api_key or not self.api_secret:
            raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET are required for signed endpoints.")
        payload = dict(params or {})
        payload.setdefault("timestamp", self.timestamp_ms())
        payload.setdefault("recvWindow", self.recv_window)
        query = urlencode(payload, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["signature"] = signature
        return self._request(method, path, payload, signed=True)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        signed: bool,
    ) -> Any:
        headers = {}
        if signed or self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        response = self._http.request(method, path, params=params, headers=headers)
        if response.status_code >= 400:
            try:
                payload: object = response.json()
            except ValueError:
                payload = response.text
            raise BinanceAPIError(response.status_code, payload)
        if not response.content:
            return {}
        return response.json()


def live_market_client(**kwargs: Any) -> BinanceRestClient:
    """Public production market data, regardless of BINANCE_ENV."""
    kwargs.setdefault("env", LIVE)
    return BinanceRestClient(**kwargs)
