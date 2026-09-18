"""Only independently qualified routes may serve production requests."""
from datetime import datetime, timezone

SUITE = "bailout-setup-v1"
MIN_TRIALS = 10
MIN_RUNS = 1
MIN_PASS_RATE = .8

# Synthetic fixture passes cannot override a reproduced production regression.
# See docs/model-qualification.md: three fresh-machine runs on 2026-09-18.
# Keep discovery/evaluation available, but require a reviewed live setup pass
# before lifting this hold. Cover the provider's dated and rolling aliases.
SETUP_HOLD = {"mistral/ministral-8b-2512:free", "mistral/ministral-8b-latest:free"}


def qualified(row, model):
    if model["id"] in SETUP_HOLD or (model.get("source") == "mistral" and model.get("name") == "ministral-8b-2512"):
        return False
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
        # Qualification establishes basic competence. Recent availability can
        # outweigh the remaining quality spread (80-100%); affinity is only a
        # small bonus, so an unreliable preferred route cannot stay on top.
        reliability = state.get("success_ewma", 1)
        age = max(0, datetime.now(timezone.utc).timestamp() * 1000 - state.get("updated", 0))
        reliability = 1 - (1 - reliability) * max(0, 1 - age / 3600000)
        # Equal percentages from one and two complete runs are not equally
        # established. This small, capped bonus never gates a new eligible model.
        evidence = min(2, max(0, row["trials"] - MIN_TRIALS) / 5)
        result.append({**model, "quality": quality, "score": quality * 100 + reliability * 25 + evidence + (2 if model["id"] == preferred else 0),
                       "reliability": reliability,
                       "trials": row["trials"]})
    result.sort(key=lambda m: (-m["score"], m["id"] != preferred, m["id"]))
    return result
