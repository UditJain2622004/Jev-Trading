"""Experiment J: atomic fan-out questions, same C state.

Copied from scripts/jev_sol_backtest_exp_c.py. Keep rolling examples and
feedback. Replace the two broad questions with eight narrow Noul questions.
Code combines the probabilities. Pre-registered buy rule: continuation>=0.55,
exhausted<0.50, late_entry<0.50, and buyers_strengthening or downside_rejected >=0.50.

    python -u scripts/jev_sol_backtest_exp_j.py
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_trading.sol_jev.answers import dump_answers, noul_p
from jev_trading.sol_jev.client import response_answers
from jev_trading.sol_jev.decide import JevDecision
from jev_trading.sol_jev.replay import (
    ReplayTrade,
    latest_matching_caches,
    load_kline_file,
    replay_trades,
    split_examples,
)
from jev_trading.sol_jev.snapshots_exp_c import build_snapshot, compact_example

log = logging.getLogger("jev_sol_backtest_exp_j")
EXAMPLE_GAP_MS = 30 * 60 * 1000
FEEDBACK_PER_BUCKET = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Experiment J: atomic fan-out Noul questions")
    parser.add_argument("--wins", type=int, default=10)
    parser.add_argument("--losses", type=int, default=10)
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--max-test", type=int, default=0, help="0 = all remaining trades")
    parser.add_argument("--dry-run", action="store_true", help="Replay and write examples; do not call Jev")
    parser.add_argument("--resume", action="store_true", help="Skip test trades already in test_calls.jsonl")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Pause between Jev calls")
    parser.add_argument("--log-file", default="data/jev_backtest_exp_j/jev_sol_backtest.log")
    parser.add_argument("--out-dir", default="data/jev_backtest_exp_j")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log = log_file.with_name(f"jev_sol_backtest_exp_j_{stamp}.log")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root = logging.getLogger("jev_sol_backtest_exp_j")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (
        stream,
        _FlushFileHandler(run_log, mode="w", encoding="utf-8"),
        _FlushFileHandler(log_file, mode="w", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.propagate = False
    log.info("Log (this run) %s", run_log)
    log.info("Log (latest)   %s", log_file)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client():
    try:
        from typesafe_sdk import RetryPolicy, TypeSafeClient
    except ImportError as exc:
        raise SystemExit("Install Jev support with: pip install 'jev-trading[jev]'") from exc
    if not os.getenv("TYPESAFE_API_KEY"):
        raise SystemExit("Set TYPESAFE_API_KEY in the environment or in .env")
    return TypeSafeClient(retry=RetryPolicy(max_retries=3, backoff_max=2.0, timeout=30.0))


def trade_row(trade: ReplayTrade, role: str) -> dict[str, object]:
    return {
        "role": role,
        "entry_ms": trade.entry_ms,
        "exit_ms": trade.exit_ms,
        "outcome": trade.outcome,
        "pnl_usd": round(trade.pnl_usd, 4),
        "hold_minutes": trade.hold_minutes,
        "both_hit": trade.both_hit,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_done_entry_ms(path: Path) -> set[int]:
    done: set[int] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done.add(int(row["entry_ms"]))
    return done


def outcome_net(trades: list[ReplayTrade]) -> tuple[Counter[str], float]:
    counts: Counter[str] = Counter(t.outcome for t in trades)
    return counts, sum(t.pnl_usd for t in trades)


def summarize_calls(rows: list[dict[str, object]]) -> dict[str, object]:
    brute = [float(r["pnl_usd"]) for r in rows]
    taken = [r for r in rows if r.get("take")]
    skipped = [r for r in rows if not r.get("take")]
    taken_pnl = [float(r["pnl_usd"]) for r in taken]
    by_take: Counter[str] = Counter(str(r["outcome"]) for r in taken)
    by_skip: Counter[str] = Counter(str(r["outcome"]) for r in skipped)
    right = wrong = 0
    for row in rows:
        predicted_win = bool(row.get("take"))
        actual_win = row.get("outcome") == "win"
        if predicted_win == actual_win:
            right += 1
        else:
            wrong += 1
    return {
        "test_trades": len(rows),
        "brute_net_usd": round(sum(brute), 4),
        "jev_net_usd": round(sum(taken_pnl), 4),
        "jev_takes": len(taken),
        "jev_skips": len(skipped),
        "jev_take_wins": by_take.get("win", 0),
        "jev_take_losses": by_take.get("loss", 0),
        "jev_take_timeouts": by_take.get("timeout", 0),
        "jev_skip_wins": by_skip.get("win", 0),
        "jev_skip_losses": by_skip.get("loss", 0),
        "jev_skip_timeouts": by_skip.get("timeout", 0),
        "jev_right": right,
        "jev_wrong": wrong,
    }


def rolling_bucket(completed: list[ReplayTrade], *, want: int, profitable: bool) -> list[ReplayTrade]:
    """Most recent finished trades in this bucket, spaced 30 minutes apart."""
    picked: list[ReplayTrade] = []
    for trade in reversed(completed):
        if (trade.pnl_usd > 0.0) != profitable:
            continue
        if any(abs(trade.entry_ms - other.entry_ms) < EXAMPLE_GAP_MS for other in picked):
            continue
        picked.append(trade)
        if len(picked) >= want:
            break
    picked.reverse()
    return picked


def pad_examples(
    picked: list[ReplayTrade],
    seed: list[ReplayTrade],
    want: int,
) -> list[ReplayTrade]:
    if len(picked) >= want:
        return picked[:want]
    have = {t.entry_ms for t in picked}
    extra = [t for t in seed if t.entry_ms not in have]
    return picked + extra[: want - len(picked)]


def examples_for_trade(
    all_trades: list[ReplayTrade],
    current: ReplayTrade,
    *,
    seed_wins: list[ReplayTrade],
    seed_losses: list[ReplayTrade],
    wins_needed: int,
    losses_needed: int,
) -> tuple[list[ReplayTrade], list[ReplayTrade]]:
    completed = [t for t in all_trades if t.exit_ms < current.entry_ms]
    profitable = rolling_bucket(completed, want=wins_needed, profitable=True)
    unprofitable = rolling_bucket(completed, want=losses_needed, profitable=False)
    return (
        pad_examples(profitable, seed_wins, wins_needed),
        pad_examples(unprofitable, seed_losses, losses_needed),
    )


@dataclass(slots=True)
class ScoredCall:
    trade: ReplayTrade
    take: bool


def market_payload(trade: ReplayTrade) -> dict[str, object]:
    return {**trade.example, "pnl_usd": round(trade.pnl_usd, 2)}


def feedback_payload(item: ScoredCall) -> dict[str, object]:
    profitable = item.trade.pnl_usd > 0.0
    return {
        **item.trade.example,
        "pnl_usd": round(item.trade.pnl_usd, 2),
        "we_bought": item.take,
        "we_were_right": (item.take and profitable) or ((not item.take) and (not profitable)),
    }


def feedback_bucket(
    history: list[ScoredCall],
    current: ReplayTrade,
    *,
    take: bool,
    profitable: bool,
    want: int = FEEDBACK_PER_BUCKET,
) -> list[ScoredCall]:
    picked: list[ScoredCall] = []
    for item in reversed(history):
        if item.trade.exit_ms >= current.entry_ms:
            continue
        if item.take != take:
            continue
        if (item.trade.pnl_usd > 0.0) != profitable:
            continue
        if any(abs(item.trade.entry_ms - other.trade.entry_ms) < EXAMPLE_GAP_MS for other in picked):
            continue
        picked.append(item)
        if len(picked) >= want:
            break
    picked.reverse()
    return picked


def feedback_for_trade(
    history: list[ScoredCall],
    current: ReplayTrade,
) -> dict[str, list[ScoredCall]]:
    return {
        "correct_buys": feedback_bucket(history, current, take=True, profitable=True),
        "wrong_buys": feedback_bucket(history, current, take=True, profitable=False),
        "correct_skips": feedback_bucket(history, current, take=False, profitable=False),
        "wrong_skips": feedback_bucket(history, current, take=False, profitable=True),
    }


def build_state(
    now: dict[str, object],
    wins: list[dict[str, object]],
    losses: list[dict[str, object]],
    feedback: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    return {
        "plan": {
            "coin": "SOLUSDT",
            "side": "buy",
            "take_profit_pct": 0.5,
            "stop_pct": 0.35,
            "timeout_minutes": 60,
            "note": (
                "`recent_wins` and `recent_losses` are market-shape examples, 10 each, "
                "balanced on purpose. The four feedback lists are this model's own earlier "
                "calls. `we_bought` is whether we took the trade. `we_were_right` is whether "
                "that call matched the later P&L. Do not mix those two facts."
            ),
        },
        "now": now,
        "recent_wins": wins,
        "recent_losses": losses,
        "correct_buys": feedback["correct_buys"],
        "wrong_buys": feedback["wrong_buys"],
        "correct_skips": feedback["correct_skips"],
        "wrong_skips": feedback["wrong_skips"],
    }


def decide_from_nouls(dumped: dict[str, object]) -> JevDecision:
    continuation = noul_p(dumped, "continuation")
    exhausted = noul_p(dumped, "exhausted")
    late = noul_p(dumped, "late_entry")
    buyers = noul_p(dumped, "buyers_strengthening")
    rejected = noul_p(dumped, "downside_rejected")
    if None in (continuation, exhausted, late, buyers, rejected):
        return JevDecision(False, "missing noul answers", None, None)
    if continuation < 0.55:
        return JevDecision(False, f"continuation {continuation:.2f} < 0.55", f"{continuation:.2f}", f"{exhausted:.2f}")
    if exhausted >= 0.50:
        return JevDecision(False, f"exhausted {exhausted:.2f} >= 0.50", f"{continuation:.2f}", f"{exhausted:.2f}")
    if late >= 0.50:
        return JevDecision(False, f"late_entry {late:.2f} >= 0.50", f"{continuation:.2f}", f"{exhausted:.2f}")
    if max(buyers, rejected) < 0.50:
        return JevDecision(
            False,
            f"no buyer/rejection support buyers={buyers:.2f} reject={rejected:.2f}",
            f"{continuation:.2f}",
            f"{exhausted:.2f}",
        )
    return JevDecision(
        True,
        f"continuation={continuation:.2f} exhausted={exhausted:.2f}",
        f"{continuation:.2f}",
        f"{exhausted:.2f}",
    )


def build_questions() -> dict[str, object]:
    from typesafe_sdk import Noul

    return {
        "continuation": Noul(
            instructions={
                "question": "Is upward pressure in `now` still persisting?",
                "use": ["now.path", "now.buy_pressure", "now.sol_moves_pct", "now.vwap", "recent_wins", "recent_losses"],
            },
            criteria={
                "true": "Buyers are still in control and the move has not already finished.",
                "false": "The push has stalled, faded, or there is not enough evidence of persistence.",
            },
        ),
        "exhausted": Noul(
            instructions={
                "question": "Is the recent SOL move already exhausted or stretched into the hour high?",
                "use": ["now.sol_last_hour", "now.last_candle", "now.sol_moves_pct"],
            },
            criteria={
                "true": "Price is late: near the hour high after a sharp run, or the candle closed at the highs with little room left.",
                "false": "There is still room, or the move has not already spent itself.",
            },
        ),
        "buyers_strengthening": Noul(
            instructions={
                "question": "Is buy pressure strengthening rather than fading?",
                "use": ["now.buy_pressure", "now.volume", "now.volume_accel"],
            },
            criteria={
                "true": "Taker-buy share is elevated or rising, and volume is not dying out.",
                "false": "Sellers dominate, or buying is fading.",
            },
        ),
        "downside_rejected": Noul(
            instructions={
                "question": "Does `now.last_candle` or the recent path show downside being rejected?",
                "use": ["now.last_candle", "now.path", "now.vwap"],
            },
            criteria={
                "true": "Lower wicks, a bounce off VWAP, or dips that fail to hold.",
                "false": "No clear rejection; dips are being accepted.",
            },
        ),
        "late_entry": Noul(
            instructions={
                "question": "Would buying SOL now be a late chase?",
                "use": ["now.sol_last_hour.room_to_hour_high_pct", "now.last_candle", "now.sol_moves_pct"],
            },
            criteria={
                "true": "This long is chasing a spike that has already happened.",
                "false": "This is not a late chase; entry still has a reasonable location.",
            },
        ),
        "volatility_suitable": Noul(
            instructions={
                "question": "Is current volatility suitable for a +0.5% take-profit / -0.35% stop?",
                "use": ["now.volatility"],
            },
            criteria={
                "true": "Typical one-minute movement is large enough to reach the target without being wildly oversized versus the stop.",
                "false": "Too quiet to reach +0.5%, or so violent that the stop is noise.",
            },
        ),
        "btc_supportive": Noul(
            instructions={
                "question": "Is Bitcoin's recent path supportive of a SOL long, or at least not fighting it?",
                "use": ["now.btc_moves_pct"],
            },
            criteria={
                "true": "BTC is flat-to-up or not dumping.",
                "false": "BTC is clearly falling and likely dragging SOL with it.",
            },
        ),
        "regime_orderly": Noul(
            instructions={
                "question": "Is the current tape orderly enough to trust a short-horizon long?",
                "use": ["now.path", "now.volatility", "now.volume"],
            },
            criteria={
                "true": "Price is moving with some structure rather than one-minute noise only.",
                "false": "Choppy, two-sided, or too noisy to trust.",
            },
        ),
    }


def ask_jev(client: object, state: dict[str, object]) -> object:
    return client.system_one(state=state, questions=build_questions())


def history_from_jsonl(path: Path, test: list[ReplayTrade]) -> list[ScoredCall]:
    by_entry = {t.entry_ms: t for t in test}
    history: list[ScoredCall] = []
    if not path.exists():
        return history
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        trade = by_entry.get(int(row["entry_ms"]))
        if trade is None:
            continue
        history.append(ScoredCall(trade=trade, take=bool(row["take"])))
    return history


def log_summary(title: str, counts: Counter[str], net: float, n: int) -> None:
    log.info(
        "%s  n=%s  wins=%s  losses=%s  timeouts=%s  net=$%.2f",
        title,
        n,
        counts.get("win", 0),
        counts.get("loss", 0),
        counts.get("timeout", 0),
        net,
    )


def main() -> None:
    args = parse_args()
    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(ROOT / args.log_file)
    load_dotenv(ROOT / ".env")

    cache_dir = ROOT / "data" / "klines"
    sol_path, btc_path = latest_matching_caches(cache_dir, "SOLUSDT")
    log.info("Experiment J: eight atomic Noul questions, code combines probabilities")
    log.info("Last scan totals had no per-trade rows, so replaying cached 1-minute bars")
    log.info("SOL cache %s", sol_path.name)
    log.info("BTC cache %s", btc_path.name)
    sol = load_kline_file(sol_path, "SOLUSDT")
    btc = load_kline_file(btc_path, "BTCUSDT")
    log.info("Replaying SOL +0.5%% / -0.35%% on %s minutes", f"{len(sol):,}")

    trades = replay_trades(sol, btc=btc, notional=args.notional)
    btc_by_time = {k.open_time: k for k in btc}
    for trade in trades:
        trade.snapshot = build_snapshot(sol, trade.entry_index, btc_by_time=btc_by_time)
        trade.example = compact_example(trade.snapshot, trade.outcome, trade.hold_minutes)
    all_counts, all_net = outcome_net(trades)
    log_summary("All sequential trades", all_counts, all_net, len(trades))

    wins, losses, test = split_examples(trades, wins_needed=args.wins, losses_needed=args.losses)
    if args.max_test:
        test = test[: args.max_test]
    example_counts, example_net = outcome_net(wins + losses)
    test_counts, test_net = outcome_net(test)
    log.info("Seed examples (used only until enough later trades have finished)")
    log.info("Example seed wins=%s  seed losses=%s", len(wins), len(losses))
    log_summary("Examples (not scored)", example_counts, example_net, len(wins) + len(losses))
    log_summary("Test window brute force", test_counts, test_net, len(test))
    for label, group in (("win", wins), ("loss", losses)):
        for i, trade in enumerate(group, start=1):
            log.info(
                "example %s %02d  entry=%s  hold=%sm  pnl=$%.2f",
                label,
                i,
                trade.entry_ms,
                trade.hold_minutes,
                trade.pnl_usd,
            )

    examples_path = out_dir / "examples.json"
    examples_payload = {
        "wins": [
            {**trade_row(t, "win_example"), "snapshot": t.example} for t in wins
        ],
        "losses": [
            {**trade_row(t, "loss_example"), "snapshot": t.example} for t in losses
        ],
    }
    examples_path.write_text(json.dumps(examples_payload, indent=2), encoding="utf-8")
    write_csv(
        out_dir / "examples.csv",
        [trade_row(t, "win_example") for t in wins] + [trade_row(t, "loss_example") for t in losses],
    )
    write_csv(out_dir / "test_trades.csv", [trade_row(t, "test") for t in test])
    log.info("Wrote example snapshots to %s", examples_path)
    log.info("Wrote %s and %s", out_dir / "examples.csv", out_dir / "test_trades.csv")

    if args.dry_run:
        first = test[0]
        pos, neg = examples_for_trade(
            trades,
            first,
            seed_wins=wins,
            seed_losses=losses,
            wins_needed=args.wins,
            losses_needed=args.losses,
        )
        log.info(
            "Dry run first-test examples: profitable=%s unprofitable=%s  feedback empty until Jev has prior calls",
            len(pos),
            len(neg),
        )
        for label, group in (("profitable", pos), ("unprofitable", neg)):
            for i, item in enumerate(group, start=1):
                log.info(
                    "rolling %s %02d  entry=%s  outcome=%s  pnl=$%.2f",
                    label,
                    i,
                    item.entry_ms,
                    item.outcome,
                    item.pnl_usd,
                )
        log.info("Dry run: not calling Jev. Test trades waiting: %s", len(test))
        return

    client = make_client()
    results_path = out_dir / "test_calls.jsonl"
    done = load_done_entry_ms(results_path) if args.resume else set()
    history = history_from_jsonl(results_path, test) if args.resume else []
    if done:
        log.info("Resume: %s test trades already scored", len(done))
    mode = "a" if args.resume and results_path.exists() else "w"
    started = time.perf_counter()
    done_n = 0
    pending = [t for t in test if t.entry_ms not in done]
    log.info("Jev calls to make: %s", len(pending))

    try:
        with results_path.open(mode, encoding="utf-8") as handle:
            for n, trade in enumerate(pending, start=1):
                pos, neg = examples_for_trade(
                    trades,
                    trade,
                    seed_wins=wins,
                    seed_losses=losses,
                    wins_needed=args.wins,
                    losses_needed=args.losses,
                )
                buckets = feedback_for_trade(history, trade)
                state = build_state(
                    trade.snapshot,
                    [market_payload(t) for t in pos],
                    [market_payload(t) for t in neg],
                    {name: [feedback_payload(item) for item in items] for name, items in buckets.items()},
                )
                try:
                    response = ask_jev(client, state)
                    answers = response_answers(response)
                    dumped = dump_answers(answers)
                    decision = decide_from_nouls(dumped)
                except Exception as exc:
                    status = getattr(exc, "status", None)
                    if status in {401, 403}:
                        raise
                    log.exception("Jev call failed on test %s: %s", n, exc)
                    dumped = {}
                    decision = JevDecision(False, f"jev_error: {exc}", None, None)

                row = {
                    "n": n,
                    "entry_ms": trade.entry_ms,
                    "exit_ms": trade.exit_ms,
                    "outcome": trade.outcome,
                    "pnl_usd": trade.pnl_usd,
                    "hold_minutes": trade.hold_minutes,
                    "take": decision.take,
                    "reason": decision.reason,
                    "hit_tp_first": decision.hit_tp_first,
                    "looks_like": decision.looks_like,
                    "jev_answers": dumped,
                    "example_profitable_ms": [t.entry_ms for t in pos],
                    "example_unprofitable_ms": [t.entry_ms for t in neg],
                    "correct_buy_ms": [item.trade.entry_ms for item in buckets["correct_buys"]],
                    "wrong_buy_ms": [item.trade.entry_ms for item in buckets["wrong_buys"]],
                    "correct_skip_ms": [item.trade.entry_ms for item in buckets["correct_skips"]],
                    "wrong_skip_ms": [item.trade.entry_ms for item in buckets["wrong_skips"]],
                }
                handle.write(json.dumps(row) + "\n")
                handle.flush()
                history.append(ScoredCall(trade=trade, take=decision.take))
                done_n += 1
                log.info(
                    "[%s/%s] %s  take=%s  hit=%s  like=%s  actual=%s  pnl=$%.2f  (%s)",
                    n,
                    len(pending),
                    trade.entry_ms,
                    decision.take,
                    decision.hit_tp_first,
                    decision.looks_like,
                    trade.outcome,
                    trade.pnl_usd,
                    decision.reason,
                )
                if n % 10 == 0 or n == len(pending):
                    elapsed = time.perf_counter() - started
                    left = (elapsed / n) * (len(pending) - n) if n else 0
                    log.info(
                        "Progress %s/%s  elapsed %.0fs  ~%.0fs left  feedback buys=%s/%s skips=%s/%s",
                        n,
                        len(pending),
                        elapsed,
                        left,
                        len(buckets["correct_buys"]),
                        len(buckets["wrong_buys"]),
                        len(buckets["correct_skips"]),
                        len(buckets["wrong_skips"]),
                    )
                if args.sleep_ms:
                    time.sleep(args.sleep_ms / 1000.0)
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()

    scored: list[dict[str, object]] = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            scored.append(json.loads(line))
    summary = summarize_calls(scored)
    log.info("Wrote per-trade Jev calls to %s", results_path)
    log.info("--- Test window (same trades, two policies) ---")
    log.info(
        "Brute force (always buy)  trades=%s  net=$%.2f",
        summary["test_trades"],
        summary["brute_net_usd"],
    )
    log.info(
        "Jev filter  takes=%s  skips=%s  net=$%.2f",
        summary["jev_takes"],
        summary["jev_skips"],
        summary["jev_net_usd"],
    )
    log.info(
        "Taken trades  wins=%s  losses=%s  timeouts=%s",
        summary["jev_take_wins"],
        summary["jev_take_losses"],
        summary["jev_take_timeouts"],
    )
    log.info(
        "Skipped trades  wins=%s  losses=%s  timeouts=%s",
        summary["jev_skip_wins"],
        summary["jev_skip_losses"],
        summary["jev_skip_timeouts"],
    )
    log.info("Jev take vs actual win  right=%s  wrong=%s", summary["jev_right"], summary["jev_wrong"])
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("Wrote %s", out_dir / "summary.json")
    log.info("This run scored %s new trades", done_n)


if __name__ == "__main__":
    main()
