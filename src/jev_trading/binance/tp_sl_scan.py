from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from jev_trading.binance.market import MarketClient, parse_kline
from jev_trading.binance.types import Kline

log = logging.getLogger("tp_sl_scan")

# One trade at a time, $1000 each time. Binance round-trip taker fee.
ROUND_TRIP_FEE = 0.002

COIN_GROUPS: dict[str, str] = {
    "BTCUSDT": "major",
    "ETHUSDT": "major",
    "BNBUSDT": "major",
    "XRPUSDT": "large_alt",
    "SOLUSDT": "large_alt",
    "ADAUSDT": "large_alt",
    "AVAXUSDT": "large_alt",
    "LINKUSDT": "large_alt",
    "LTCUSDT": "large_alt",
    "DOTUSDT": "large_alt",
    "BCHUSDT": "large_alt",
    "ATOMUSDT": "large_alt",
    "DOGEUSDT": "memecoin",
    "SHIBUSDT": "memecoin",
    "PEPEUSDT": "memecoin",
    "WIFUSDT": "memecoin",
    "NEARUSDT": "fast_alt",
    "SUIUSDT": "fast_alt",
    "APTUSDT": "fast_alt",
    "ARBUSDT": "fast_alt",
    "TIAUSDT": "fast_alt",
    "SEIUSDT": "fast_alt",
    "FILUSDT": "fast_alt",
    "OPUSDT": "fast_alt",
}

DEFAULT_SYMBOLS = tuple(COIN_GROUPS.keys())

# Take-profit %, stop-loss %. Includes the original 0.50 / 0.08 idea.
DEFAULT_PAIRS: tuple[tuple[float, float], ...] = (
    (0.50, 0.08),
    (0.50, 0.15),
    (0.50, 0.25),
    (0.50, 0.35),
    (0.50, 0.50),
    (0.30, 0.15),
    (0.30, 0.30),
    (0.40, 0.20),
    (0.40, 0.35),
    (0.25, 0.12),
    (0.20, 0.10),
    (0.60, 0.25),
    (0.80, 0.30),
    (0.80, 0.40),
    (1.00, 0.40),
    (1.00, 0.50),
)


@dataclass(frozen=True, slots=True)
class PairResult:
    symbol: str
    group: str
    take_profit_pct: float
    stop_loss_pct: float
    trades: int
    wins: int
    losses: int
    timeouts: int
    both_hit: int
    win_rate_pct: float
    net_usd: float
    fees_usd: float
    avg_hold_minutes: float
    avg_win_usd: float
    avg_loss_usd: float
    worst_trade_usd: float
    best_trade_usd: float


def cache_path(cache_dir: Path, symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    return cache_dir / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"


def load_klines(
    market: MarketClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    cache_dir: Path,
) -> list[Kline]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, symbol, interval, start_ms, end_ms)
    if path.exists():
        log.info("%s: loading cached prices from %s", symbol, path.name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        klines = [parse_kline(symbol, interval, row) for row in raw]
        log.info("%s: cache hit, %s candles", symbol, f"{len(klines):,}")
        return klines

    log.info("%s: downloading %s prices from Binance (this is the slow part)", symbol, interval)
    rows: list[list[object]] = []
    klines: list[Kline] = []
    for kline in market.iter_klines(symbol, interval, start_ms, end_ms):
        klines.append(kline)
        rows.append(
            [
                kline.open_time,
                str(kline.open),
                str(kline.high),
                str(kline.low),
                str(kline.close),
                str(kline.volume),
                kline.close_time,
                str(kline.quote_volume),
                kline.trades,
                str(kline.taker_buy_base),
                str(kline.taker_buy_quote),
                "0",
            ]
        )
        if len(klines) % 1000 == 0:
            log.info("%s: downloaded %s candles so far...", symbol, f"{len(klines):,}")
    path.write_text(json.dumps(rows), encoding="utf-8")
    log.info("%s: saved %s candles to cache", symbol, f"{len(klines):,}")
    return klines


def simulate_pair(
    klines: Sequence[Kline],
    take_profit_pct: float,
    stop_loss_pct: float,
    *,
    notional: float = 1000.0,
    timeout_bars: int = 60,
    fee_rate: float = ROUND_TRIP_FEE,
) -> dict[str, float | int]:
    """Walk one long at a time. Entry = bar close. Look at later bars only.

    If the same bar hits both the win and the stop, we count it as a stop.
    That is the pessimistic guess (we cannot see which happened first).
    Timeout exits at that bar's close and still pays fees.
    """
    tp_frac = take_profit_pct / 100.0
    sl_frac = stop_loss_pct / 100.0
    wins = losses = timeouts = both_hit = 0
    pnls: list[float] = []
    holds: list[int] = []
    i = 0
    last_entry = len(klines) - 1

    while i < last_entry:
        entry = float(klines[i].close)
        tp = entry * (1.0 + tp_frac)
        sl = entry * (1.0 - sl_frac)
        end_j = min(len(klines) - 1, i + timeout_bars)
        resolved = False
        j = i + 1
        while j <= end_j:
            high = float(klines[j].high)
            low = float(klines[j].low)
            hit_tp = high >= tp
            hit_sl = low <= sl
            hold = j - i
            if hit_tp and hit_sl:
                pnl = (-sl_frac - fee_rate) * notional
                losses += 1
                both_hit += 1
                pnls.append(pnl)
                holds.append(hold)
                i = j + 1
                resolved = True
                break
            if hit_sl:
                pnl = (-sl_frac - fee_rate) * notional
                losses += 1
                pnls.append(pnl)
                holds.append(hold)
                i = j + 1
                resolved = True
                break
            if hit_tp:
                pnl = (tp_frac - fee_rate) * notional
                wins += 1
                pnls.append(pnl)
                holds.append(hold)
                i = j + 1
                resolved = True
                break
            j += 1
        if not resolved:
            last_close = float(klines[end_j].close)
            move = (last_close / entry) - 1.0
            pnl = (move - fee_rate) * notional
            timeouts += 1
            pnls.append(pnl)
            holds.append(end_j - i)
            i = end_j + 1

    trades = len(pnls)
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "both_hit": both_hit,
        "win_rate_pct": (100.0 * wins / trades) if trades else 0.0,
        "net_usd": sum(pnls) if pnls else 0.0,
        "fees_usd": trades * fee_rate * notional,
        "avg_hold_minutes": (sum(holds) / trades) if trades else 0.0,
        "avg_win_usd": (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0,
        "avg_loss_usd": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0,
        "worst_trade_usd": min(pnls) if pnls else 0.0,
        "best_trade_usd": max(pnls) if pnls else 0.0,
    }


def scan_symbol(
    symbol: str,
    klines: Sequence[Kline],
    pairs: Iterable[tuple[float, float]],
    *,
    notional: float,
    timeout_bars: int,
    fee_rate: float,
) -> list[PairResult]:
    group = COIN_GROUPS.get(symbol, "other")
    results: list[PairResult] = []
    for take_profit_pct, stop_loss_pct in pairs:
        stats = simulate_pair(
            klines,
            take_profit_pct,
            stop_loss_pct,
            notional=notional,
            timeout_bars=timeout_bars,
            fee_rate=fee_rate,
        )
        results.append(
            PairResult(
                symbol=symbol,
                group=group,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                **stats,  # type: ignore[arg-type]
            )
        )
        log.info(
            "%s  TP %.2f%%  SL %.2f%%  trades=%s  wins=%s  losses=%s  timeout=%s  win=%.1f%%  net=$%.2f",
            symbol,
            take_profit_pct,
            stop_loss_pct,
            stats["trades"],
            stats["wins"],
            stats["losses"],
            stats["timeouts"],
            stats["win_rate_pct"],
            stats["net_usd"],
        )
    return results


def available_symbols(market: MarketClient, symbols: Sequence[str]) -> list[str]:
    log.info("Fetching Binance Spot symbol list...")
    info = market.exchange_info()
    live = {
        str(item["symbol"])
        for item in info["symbols"]
        if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT"
    }
    keep: list[str] = []
    for symbol in symbols:
        if symbol not in live:
            log.warning("skip %s: not a live USDT Spot pair", symbol)
            continue
        keep.append(symbol)
    log.info("Will scan %s coins: %s", len(keep), ", ".join(keep))
    return keep


def write_csv(path: Path, rows: Sequence[PairResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def print_summary(rows: Sequence[PairResult], *, original: tuple[float, float] = (0.50, 0.08)) -> None:
    if not rows:
        log.info("No results.")
        return
    ranked = sorted(rows, key=lambda r: r.net_usd, reverse=True)
    log.info("")
    log.info("Best 10 (most dollars left on a $1000 clip, sequential trades)")
    _print_table(ranked[:10])
    log.info("")
    log.info("Worst 10")
    _print_table(list(reversed(ranked[-10:])))
    log.info("")
    log.info("Your original idea: take profit %s%% / stop %s%%", original[0], original[1])
    original_rows = [
        r for r in ranked if r.take_profit_pct == original[0] and r.stop_loss_pct == original[1]
    ]
    _print_table(original_rows)

    by_group: dict[str, list[PairResult]] = {}
    for row in ranked:
        by_group.setdefault(row.group, []).append(row)
    log.info("")
    log.info("Best pair per coin group")
    for group, group_rows in by_group.items():
        best = group_rows[0]
        log.info(
            "  %s %s TP %.2f%%  SL %.2f%%  net $%s  win %.1f%%  trades %s",
            f"{group:10}",
            f"{best.symbol:12}",
            best.take_profit_pct,
            best.stop_loss_pct,
            f"{best.net_usd:,.2f}",
            best.win_rate_pct,
            best.trades,
        )


def _print_table(rows: Sequence[PairResult]) -> None:
    if not rows:
        log.info("  (none)")
        return
    log.info(
        "  %s %s %s %s %s %s %s %s %s",
        f"{'coin':12}",
        f"{'group':10}",
        f"{'TP%':>6}",
        f"{'SL%':>6}",
        f"{'trades':>6}",
        f"{'win%':>6}",
        f"{'net$':>10}",
        f"{'fees$':>8}",
        f"{'hold_m':>7}",
    )
    for row in rows:
        log.info(
            "  %s %s %6.2f %6.2f %6d %6.1f %10.2f %8.2f %7.1f",
            f"{row.symbol:12}",
            f"{row.group:10}",
            row.take_profit_pct,
            row.stop_loss_pct,
            row.trades,
            row.win_rate_pct,
            row.net_usd,
            row.fees_usd,
            row.avg_hold_minutes,
        )


def utc_ms_days_ago(days: int) -> tuple[int, int]:
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    end -= end % 60_000
    start = end - days * 24 * 60 * 60 * 1000
    return start, end
