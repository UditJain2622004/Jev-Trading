"""Yahoo Finance 5-minute bars for US stocks. Cache on disk."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("stocks")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = ROOT / "data" / "stocks" / "klines"

DEFAULT_SYMBOLS = (
    "INTC",
    "MU",
    "MRVL",
    "AMD",
    "SNDK",
    "HPE",
    "DELL",
    "ARM",
    "IBM",
    "TXN",
    "QCOM",
    "AVGO",
    "ORCL",
)

BAR_MINUTES = 5


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def cache_path(cache_dir: Path, symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    return cache_dir / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"


def latest_cache(cache_dir: Path, symbol: str, interval: str = "5m") -> Path | None:
    matches = list(cache_dir.glob(f"{symbol}_{interval}_*.json"))
    if not matches:
        return None

    def end_ts(path: Path) -> int:
        return int(path.stem.split("_")[-1])

    return max(matches, key=end_ts)


def load_bars(path: Path, symbol: str) -> list[Bar]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Bar(
            symbol=symbol,
            open_time=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for row in raw
    ]


def save_bars(path: Path, bars: Sequence[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "open_time": bar.open_time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _to_ms(ts) -> int:
    value = ts.value if hasattr(ts, "value") else int(ts)
    # pandas ns timestamp
    if value > 10_000_000_000_000:
        return value // 1_000_000
    if value > 10_000_000_000:
        return value
    return value * 1000


def download_symbol(
    symbol: str,
    *,
    period: str = "60d",
    interval: str = "5m",
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> tuple[Path, list[Bar]]:
    cached = latest_cache(cache_dir, symbol, interval)
    if cached is not None and not force:
        bars = load_bars(cached, symbol)
        if len(bars) >= 500:
            log.info("%s: cache hit %s (%s bars)", symbol, cached.name, f"{len(bars):,}")
            return cached, bars
        log.info("%s: cache too small (%s bars), downloading again", symbol, len(bars))

    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit("Install stock data support with: pip install yfinance pandas") from exc

    log.info("%s: downloading %s %s from Yahoo (regular hours)", symbol, period, interval)
    frame = yf.download(
        symbol,
        period=period,
        interval=interval,
        auto_adjust=False,
        prepost=False,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"{symbol}: Yahoo returned no {interval} bars")

    columns = frame.columns
    if getattr(columns, "nlevels", 1) > 1:
        levels0 = list(columns.get_level_values(0))
        levels_last = list(columns.get_level_values(-1))
        if symbol in levels0:
            frame = frame[symbol]
        elif symbol in levels_last:
            frame = frame.xs(symbol, axis=1, level=-1)
        else:
            frame.columns = [str(col[0]) for col in columns]

    rename = {str(col): str(col).title() for col in frame.columns}
    frame = frame.rename(columns=rename)
    needed = {"Open", "High", "Low", "Close"}
    missing = needed - set(frame.columns)
    if missing:
        raise RuntimeError(f"{symbol}: missing columns {sorted(missing)} in {list(frame.columns)}")

    bars: list[Bar] = []
    for ts, row in frame.iterrows():
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])
        if high <= 0 or close <= 0:
            continue
        bars.append(
            Bar(
                symbol=symbol,
                open_time=_to_ms(ts),
                open=float(row["Open"]),
                high=high,
                low=low,
                close=close,
                volume=float(row["Volume"]) if "Volume" in row else 0.0,
            )
        )
    if not bars:
        raise RuntimeError(f"{symbol}: parsed zero bars")
    bars.sort(key=lambda bar: bar.open_time)
    path = cache_path(cache_dir, symbol, interval, bars[0].open_time, bars[-1].open_time)
    save_bars(path, bars)
    log.info("%s: saved %s bars to %s", symbol, f"{len(bars):,}", path.name)
    return path, bars
