from __future__ import annotations

from typing import Any


def dump_answer(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if isinstance(item, dict):
        out = dict(item)
        probs = out.get("probabilities")
        if isinstance(probs, dict):
            out["probabilities"] = {str(k): float(v) for k, v in probs.items()}
        return out
    out: dict[str, Any] = {}
    if hasattr(item, "choice"):
        out["type"] = "choice"
        out["choice"] = str(item.choice)
    elif hasattr(item, "score"):
        out["type"] = "score"
        out["score"] = float(item.score)
    elif hasattr(item, "noul"):
        out["type"] = "noul"
        out["noul"] = float(item.noul)
    if hasattr(item, "confidence"):
        out["confidence"] = float(item.confidence)
    if hasattr(item, "probabilities"):
        out["probabilities"] = {str(k): float(v) for k, v in dict(item.probabilities).items()}
    return out or None


def dump_answers(answers: Any) -> dict[str, Any]:
    if not answers:
        return {}
    if hasattr(answers, "items"):
        return {str(key): dump_answer(value) for key, value in answers.items()}
    return {}


def choice_p(dumped: dict[str, Any], key: str, option: str) -> float | None:
    item = dumped.get(key)
    if not isinstance(item, dict):
        return None
    probs = item.get("probabilities")
    if isinstance(probs, dict) and option in probs:
        return float(probs[option])
    if item.get("choice") == option:
        return 1.0
    if item.get("choice"):
        return 0.0
    return None


def noul_p(dumped: dict[str, Any], key: str) -> float | None:
    item = dumped.get(key)
    if not isinstance(item, dict) or "noul" not in item:
        return None
    return float(item["noul"])


def score_value(dumped: dict[str, Any], key: str) -> float | None:
    item = dumped.get(key)
    if not isinstance(item, dict) or "score" not in item:
        return None
    return float(item["score"])
