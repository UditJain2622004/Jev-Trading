from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class JevDecision:
    take: bool
    reason: str
    hit_tp_first: str | None
    looks_like: str | None


def _choice(answers: Any, key: str) -> str | None:
    item = _get(answers, key)
    if item is None:
        return None
    if hasattr(item, "choice"):
        return str(item.choice)
    if isinstance(item, dict) and "choice" in item:
        return str(item["choice"])
    return None


def _get(answers: Any, key: str) -> Any:
    if answers is None:
        return None
    if hasattr(answers, "get"):
        return answers.get(key)
    return getattr(answers, key, None)


def decide_from_answers(answers: Any) -> JevDecision:
    """Skip unless Jev's checks line up. Code owns the yes/no, not the model."""
    hit = _choice(answers, "hit_tp_first")
    looks = _choice(answers, "looks_like")

    if looks == "more_like_losses":
        return JevDecision(False, "looks like the losing examples", hit, looks)
    if hit is None:
        return JevDecision(False, "missing hit_tp_first answer", hit, looks)
    if hit != "1":
        return JevDecision(False, "hit_tp_first is 0", hit, looks)
    return JevDecision(True, "passed filters", hit, looks)
