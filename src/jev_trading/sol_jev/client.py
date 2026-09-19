from __future__ import annotations

from typing import Any


def build_state(
    now: dict[str, object],
    wins: list[dict[str, object]],
    losses: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "plan": {
            "coin": "SOLUSDT",
            "side": "buy",
            "take_profit_pct": 0.5,
            "stop_pct": 0.35,
            "timeout_minutes": 60,
            "note": "Examples are balanced on purpose (10 wins and 10 losses). They show shape, not the real win rate.",
        },
        "now": now,
        "recent_wins": wins,
        "recent_losses": losses,
    }


def build_questions() -> dict[str, Any]:
    from typesafe_sdk import Choice

    return {
        "looks_like": Choice(
            instructions=(
                "Compare `now` to `recent_wins` and `recent_losses`. "
                "Those examples used the same 0.5% take-profit and 0.35% stop. "
                "Does `now` look more like the wins or the losses?"
            ),
            criteria={
                "more_like_wins": "Closer to the win examples",
                "more_like_losses": "Closer to the loss examples",
                "unclear": "Not a clear match either way",
            },
        ),
        "hit_tp_first": Choice(
            instructions=(
                "If we buy SOL now at `now.entry` with take-profit +0.5% and stop -0.35%, "
                "will price hit the take-profit before the stop within 60 minutes? "
                "Answer 0 if SOL has already spiked so a long is late and likely to fade "
                "(stretched into the hour high after a sharp run-up). "
                "Use `recent_wins` and `recent_losses` as same-shape examples. "
                "Pick exactly 0 or 1."
            ),
            criteria={
                "0": "No. Stop is more likely, or this long is already late.",
                "1": "Yes. Take-profit is more likely, and this is not a late spike.",
            },
        ),
    }


def ask_jev(client: Any, state: dict[str, object]) -> Any:
    return client.system_one(state=state, questions=build_questions())


def response_answers(response: Any) -> Any:
    merged: dict[str, Any] = {}
    for name in ("nouls", "choices", "scores"):
        part = getattr(response, name, None)
        if part:
            merged.update(dict(part))
    if merged:
        return merged
    if hasattr(response, "answers") and response.answers is not None:
        return response.answers
    return response
