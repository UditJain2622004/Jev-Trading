"""Experiment O: logistic filter on experiment I's saved Jev probabilities.

No new Jev calls. Train on the first half of I's test window, apply on the
second half. Features are P(hit=1), P(wins), P(losses), both confidences.
Label is profitable (pnl > 0). Buy when predicted P(profit) >= 0.50.

    python -u scripts/jev_sol_backtest_exp_o.py
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "jev_backtest_exp_i" / "test_calls.jsonl"
OUT = ROOT / "data" / "jev_backtest_exp_o"


def _p(answers: dict, key: str, option: str | None = None) -> float:
    item = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(item, dict):
        return 0.0
    if option is None:
        return float(item.get("noul") or 0.0)
    probs = item.get("probabilities") if isinstance(item.get("probabilities"), dict) else {}
    if option in probs:
        return float(probs[option])
    return 1.0 if item.get("choice") == option else 0.0


def _conf(answers: dict, key: str) -> float:
    item = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(item, dict):
        return 0.0
    return float(item.get("confidence") or 0.0)


def features(row: dict) -> list[float]:
    ans = row.get("jev_answers") if isinstance(row.get("jev_answers"), dict) else {}
    return [
        _p(ans, "hit_tp_first", "1"),
        _p(ans, "looks_like", "more_like_wins"),
        _p(ans, "looks_like", "more_like_losses"),
        _conf(ans, "hit_tp_first"),
        _conf(ans, "looks_like"),
        1.0,
    ]


def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def fit_logreg(x: list[list[float]], y: list[int], steps: int = 400, lr: float = 0.25) -> list[float]:
    w = [0.0] * len(x[0])
    n = float(len(x))
    for _ in range(steps):
        grad = [0.0] * len(w)
        for row, label in zip(x, y):
            z = sum(wi * xi for wi, xi in zip(w, row))
            err = sigmoid(z) - label
            for j, xi in enumerate(row):
                grad[j] += err * xi
        for j in range(len(w)):
            w[j] -= lr * (grad[j] / n + 0.02 * w[j])
    return w


def predict(w: list[float], row: list[float]) -> float:
    return sigmoid(sum(wi * xi for wi, xi in zip(w, row)))


def summarize(rows: list[dict], takes: list[bool]) -> dict:
    selected = [r for r, take in zip(rows, takes) if take]
    by = Counter(str(r["outcome"]) for r in selected)
    resolved = by.get("win", 0) + by.get("loss", 0)
    return {
        "takes": len(selected),
        "net_usd": round(sum(float(r["pnl_usd"]) for r in selected), 4),
        "wins": by.get("win", 0),
        "losses": by.get("loss", 0),
        "timeouts": by.get("timeout", 0),
        "resolved_win_rate_pct": round(100.0 * by.get("win", 0) / resolved, 2) if resolved else None,
    }


def main() -> None:
    rows = [json.loads(line) for line in SRC.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) < 40:
        raise SystemExit(f"Need experiment I results at {SRC}")
    split = len(rows) // 2
    train, test = rows[:split], rows[split:]
    x_train = [features(r) for r in train]
    y_train = [1 if float(r["pnl_usd"]) > 0 else 0 for r in train]
    weights = fit_logreg(x_train, y_train)
    OUT.mkdir(parents=True, exist_ok=True)

    def takes_for(subset: list[dict], thr: float) -> list[bool]:
        return [predict(weights, features(r)) >= thr for r in subset]

    live = summarize(test, takes_for(test, 0.50))
    sweep = []
    for thr in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
        item = {"threshold": thr, "split": "test_second_half"}
        item.update(summarize(test, takes_for(test, thr)))
        sweep.append(item)
        item_tr = {"threshold": thr, "split": "train_first_half"}
        item_tr.update(summarize(train, takes_for(train, thr)))
        sweep.append(item_tr)

    always_test = summarize(test, [True] * len(test))
    summary = {
        "source": str(SRC),
        "train_n": len(train),
        "test_n": len(test),
        "weights": {
            "p_hit_1": weights[0],
            "p_wins": weights[1],
            "p_losses": weights[2],
            "conf_hit": weights[3],
            "conf_looks": weights[4],
            "bias": weights[5],
        },
        "train_base_win_rate_pct": round(100.0 * sum(y_train) / len(y_train), 2),
        "test_brute": always_test,
        "live_p_profit_ge_0.50_on_test": live,
        "sweep": sweep,
    }
    scored = []
    for row in test:
        p = predict(weights, features(row))
        scored.append(
            {
                "entry_ms": row["entry_ms"],
                "outcome": row["outcome"],
                "pnl_usd": row["pnl_usd"],
                "p_profit": round(p, 4),
                "take": p >= 0.50,
            }
        )
    (OUT / "test_calls.jsonl").write_text("\n".join(json.dumps(r) for r in scored) + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("Experiment O: logistic on I probabilities")
    print(f"Train n={len(train)}  test n={len(test)}")
    print("Weights", {k: round(v, 3) for k, v in summary["weights"].items()})
    print(
        "Test brute  takes=%s net=$%.2f W/L/T=%s/%s/%s wr=%s"
        % (
            always_test["takes"],
            always_test["net_usd"],
            always_test["wins"],
            always_test["losses"],
            always_test["timeouts"],
            always_test["resolved_win_rate_pct"],
        )
    )
    print(
        "Live P>=0.50  takes=%s net=$%.2f W/L/T=%s/%s/%s wr=%s"
        % (live["takes"], live["net_usd"], live["wins"], live["losses"], live["timeouts"], live["resolved_win_rate_pct"])
    )
    for item in sweep:
        if item["split"] != "test_second_half":
            continue
        print(
            "test thr=%.2f takes=%s net=$%.2f W/L/T=%s/%s/%s wr=%s"
            % (
                item["threshold"],
                item["takes"],
                item["net_usd"],
                item["wins"],
                item["losses"],
                item["timeouts"],
                item["resolved_win_rate_pct"],
            )
        )
    print("Wrote", OUT / "summary.json")


if __name__ == "__main__":
    main()
