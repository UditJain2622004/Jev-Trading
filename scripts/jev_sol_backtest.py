"""Backtest SOL +0.5% / -0.35% with Jev filtering.

The last scan only saved totals, not each trade. This script replays the
same cached 7-day 1-minute SOL (and Bitcoin) bars, takes the first 10 wins
and 10 losses as examples, then asks Jev about every later trade.

    python -u scripts/jev_sol_backtest.py --dry-run
    python -u scripts/jev_sol_backtest.py --max-test 5
    python -u scripts/jev_sol_backtest.py --resume
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_trading.sol_jev.client import ask_jev, build_state, response_answers
from jev_trading.sol_jev.decide import JevDecision, decide_from_answers
from jev_trading.sol_jev.replay import (
    ReplayTrade,
    latest_matching_caches,
    load_kline_file,
    replay_trades,
    split_examples,
)

log = logging.getLogger("jev_sol_backtest")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jev-filtered SOL 0.5/0.35 backtest")
    parser.add_argument("--wins", type=int, default=10)
    parser.add_argument("--losses", type=int, default=10)
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--max-test", type=int, default=0, help="0 = all remaining trades")
    parser.add_argument("--dry-run", action="store_true", help="Replay and write examples; do not call Jev")
    parser.add_argument("--resume", action="store_true", help="Skip test trades already in test_calls.jsonl")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Pause between Jev calls")
    parser.add_argument("--log-file", default="data/jev_backtest/jev_sol_backtest.log")
    parser.add_argument("--out-dir", default="data/jev_backtest")
    return parser.parse_args()


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_log = log_file.with_name(f"jev_sol_backtest_{stamp}.log")
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root = logging.getLogger("jev_sol_backtest")
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
    log.info("Last scan totals had no per-trade rows, so replaying cached 1-minute bars")
    log.info("SOL cache %s", sol_path.name)
    log.info("BTC cache %s", btc_path.name)
    sol = load_kline_file(sol_path, "SOLUSDT")
    btc = load_kline_file(btc_path, "BTCUSDT")
    log.info("Replaying SOL +0.5%% / -0.35%% on %s minutes", f"{len(sol):,}")

    trades = replay_trades(sol, btc=btc, notional=args.notional)
    all_counts, all_net = outcome_net(trades)
    log_summary("All sequential trades", all_counts, all_net, len(trades))

    wins, losses, test = split_examples(trades, wins_needed=args.wins, losses_needed=args.losses)
    if args.max_test:
        test = test[: args.max_test]
    example_counts, example_net = outcome_net(wins + losses)
    test_counts, test_net = outcome_net(test)
    log.info("Example wins=%s  example losses=%s", len(wins), len(losses))
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
        log.info("Dry run: not calling Jev. Test trades waiting: %s", len(test))
        return

    client = make_client()
    win_examples = [t.example for t in wins]
    loss_examples = [t.example for t in losses]
    results_path = out_dir / "test_calls.jsonl"
    done = load_done_entry_ms(results_path) if args.resume else set()
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
                state = build_state(trade.snapshot, win_examples, loss_examples)
                try:
                    response = ask_jev(client, state)
                    answers = response_answers(response)
                    decision = decide_from_answers(answers)
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
                }
                handle.write(json.dumps(row) + "\n")
                handle.flush()
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
                    log.info("Progress %s/%s  elapsed %.0fs  ~%.0fs left", n, len(pending), elapsed, left)
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
