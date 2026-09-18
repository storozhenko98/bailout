"""Only independently qualified routes may serve production requests."""
from datetime import datetime, timezone

SUITE = "bailout-setup-v1"
MIN_TRIALS = 20
MIN_RUNS = 2
MIN_PASS_RATE = .9


def qualified(row, model):
    try:
        checked = datetime.fromisoformat(row["evaluated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return (row["suite"] == SUITE and row["fingerprint"] == model["fingerprint"]
                and row["trials"] >= MIN_TRIALS and row["runs"] >= MIN_RUNS
                and row["passed"] / row["trials"] >= MIN_PASS_RATE
                and row["critical_failures"] == 0 and row["native_tools"] is True
                and 0 <= age <= 30 * 86400)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def rank(models, snapshot, preferred=None, avoid=()):
    rows = {r["id"]: r for r in snapshot.get("models", [])}
    health = snapshot.get("health", {})
    result = []
    for model in models:
        row = rows.get(model["id"], {})
        if model["id"] in avoid or not qualified(row, model):
            continue
        quality = row["passed"] / row["trials"]
        state = health.get(model["id"], {})
        # Quality is dominant. Availability can reorder close contenders, and
        # a successful session stays on its model within the same quality tier.
        reliability = state.get("success_ewma", 1)
        age = max(0, datetime.now(timezone.utc).timestamp() * 1000 - state.get("updated", 0))
        reliability = 1 - (1 - reliability) * max(0, 1 - age / 3600000)
        result.append({**model, "quality": quality, "score": quality * 100 + reliability * 5,
                       "quality_tier": int(quality * 10), "reliability": reliability,
                       "trials": row["trials"]})
    result.sort(key=lambda m: (-m["quality_tier"], m["id"] != preferred, -m["score"], m["id"]))
    return result
