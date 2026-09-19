"""Experiment M: event-driven primary setup, then Jev as a filter.

Copied from scripts/jev_sol_backtest_exp_c.py. Code only asks Jev when a
long already looks like a setup: 15m still up, room to hour high >= 0.20%,
volume not dead, at least 6 up-minutes in 15m, and not closed at the highs.
Jev then uses C's two questions. Other slots are skipped without a Jev call.

    python -u scripts/jev_sol_backtest_exp_m.py
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

from jev_trading.sol_jev.answers import dump_answers
from jev_trading.sol_jev.client import response_answers
from jev_trading.sol_jev.decide import JevDecision, decide_from_answers
from jev_trading.sol_jev.replay import (
    ReplayTrade,
    latest_matching_caches,
    load_kline_file,
    replay_trades,
    split_examples,
)
from jev_trading.sol_jev.snapshots_exp_c import build_snapshot, compact_example

log = logging.getLogger("jev_sol_backtest_exp_m")
EXAMPLE_GAP_MS = 30 * 60 * 1000
FEEDBACK_PER_BUCKET = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Experiment M: primary setup then Jev filter")
    parser.add_argument("--wins", type=int, default=10)
    parser.add_argument("--losses", type=int, default=10)
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--max-test", type=int, default=0, help="0 = all remaining trades")
    parser.add_argument("--dry-run", action="store_true", help="Replay and write examples; do not call Jev")
    parser.add_argument("--resume", action="store_true", help="Skip test trades already in test_calls.jsonl")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Pause between Jev calls")
    parser.add_argument("--log-file", default="data/jev_backtest_exp_m/jev_sol_backtest.log")
    parser.add_argument("--out-dir", default="data/jev_backtest_exp_m")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log = log_file.with_name(f"jev_sol_backtest_exp_m_{stamp}.log")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root = logging.getLogger("jev_sol_backtest_exp_m")
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


def primary_setup_block(trade: ReplayTrade) -> str | None:
    """Skip without asking Jev unless this already looks like a long setup."""
    snap = trade.snapshot
    hour = snap.get("sol_last_hour") if isinstance(snap.get("sol_last_hour"), dict) else {}
    moves = snap.get("sol_moves_pct") if isinstance(snap.get("sol_moves_pct"), dict) else {}
    volume = snap.get("volume") if isinstance(snap.get("volume"), dict) else {}
    path = snap.get("path") if isinstance(snap.get("path"), dict) else {}
    path15 = path.get("15m") if isinstance(path.get("15m"), dict) else {}
    candle = snap.get("last_candle") if isinstance(snap.get("last_candle"), dict) else {}
    room = hour.get("room_to_hour_high_pct")
    if room is None or float(room) < 0.20:
        return "no setup: little room to hour high"
    move15 = moves.get("15m")
    if move15 is None or float(move15) <= 0:
        return "no setup: 15m not up"
    vs = volume.get("vs_typical")
    if vs is None or float(vs) < 0.80:
        return "no setup: quiet volume"
    up = path15.get("up_minutes")
    if up is None or int(up) < 6:
        return "no setup: weak 15m path"
    close_in = candle.get("close_in_range")
    if close_in is not None and float(close_in) >= 0.90 and float(room) < 0.25:
        return "no setup: closed at highs"
    return None


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


def build_questions() -> dict[str, object]:
    from typesafe_sdk import Choice

    return {
        "looks_like": Choice(
            instructions=(
                "Compare `now` to `recent_wins` and `recent_losses`. "
                "Also look at `wrong_buys` and `wrong_skips` (this model's mistakes) "
                "and `correct_buys` / `correct_skips` (calls that matched later P&L). "
                "Use the extra now-card fields (`path`, `volatility`, `buy_pressure`, "
                "`last_candle`, `volume_accel`, `vwap`, `clock_utc`). "
                "Does `now` look more like the winning market examples or the losing ones?"
            ),
            criteria={
                "more_like_wins": "Closer to the win examples / correct-buy shapes",
                "more_like_losses": "Closer to the loss examples / wrong-buy or correct-skip shapes",
                "unclear": "Not a clear match either way",
            },
        ),
        "hit_tp_first": Choice(
            instructions=(
                "If we buy SOL now at `now.entry` with take-profit +0.5% and stop -0.35%, "
                "will price hit the take-profit before the stop within 60 minutes? "
                "Answer 0 if SOL has already spiked so a long is late and likely to fade. "
                "Use `recent_wins`, `recent_losses`, the four feedback lists, and the extra "
                "now-card fields (`path`, `volatility`, `buy_pressure`, `last_candle`, "
                "`volume_accel`, `vwap`). "
                "Pick exactly 0 or 1."
            ),
            criteria={
                "0": "No. Stop is more likely, or this long is already late.",
                "1": "Yes. Take-profit is more likely, and this is not a late spike.",
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
    log.info("Experiment M: code primary setup, then Jev filter")
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
        setup_n = sum(1 for t in test if primary_setup_block(t) is None)
        log.info("Dry run: not calling Jev. Test trades waiting: %s  setups that would ask Jev: %s", len(test), setup_n)
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
    setup_pending = [t for t in pending if primary_setup_block(t) is None]
    log.info("Slots=%s  Jev calls to make (setups)=%s", len(pending), len(setup_pending))
    asked_n = 0

    try:
        with results_path.open(mode, encoding="utf-8") as handle:
            for n, trade in enumerate(pending, start=1):
                blocked = primary_setup_block(trade)
                dumped: dict[str, object] = {}
                pos: list[ReplayTrade] = []
                neg: list[ReplayTrade] = []
                buckets = {
                    "correct_buys": [],
                    "wrong_buys": [],
                    "correct_skips": [],
                    "wrong_skips": [],
                }
                if blocked:
                    decision = JevDecision(False, blocked, None, None)
                else:
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
                        decision = decide_from_answers(answers)
                        asked_n += 1
                    except Exception as exc:
                        status = getattr(exc, "status", None)
                        if status in {401, 403}:
                            raise
                        log.exception("Jev call failed on test %s: %s", n, exc)
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
                    "asked_jev": blocked is None,
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
                if blocked is None:
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
    summary["asked_jev"] = sum(1 for r in scored if r.get("asked_jev"))
    log.info("Wrote per-trade Jev calls to %s", results_path)
    log.info("Asked Jev on %s setups", summary["asked_jev"])
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
