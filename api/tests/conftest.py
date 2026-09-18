from datetime import datetime, timezone, timedelta

import pytest

from capacity import LocalCapacity
from providers import fingerprint


def free_accounts():
    return {p: {"tier": "free", "billing_disabled": True, "topups_disabled": True,
                "verified_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
            for p in ("groq", "mistral")}


def qualification(model, **changes):
    return {"id": model["id"], "fingerprint": fingerprint(model), "suite": "bailout-setup-v1",
            "trials": 20, "passed": 20, "runs": 2, "critical_failures": 0, "native_tools": True,
            "evaluated_at": datetime.now(timezone.utc).isoformat(), **changes}


@pytest.fixture(autouse=True)
def test_qualifications(monkeypatch):
    # Explicit synthetic qualifications only. Production LocalCapacity has no
    # qualifications, so running without a registry always fails closed.
    models = [{"id": f"test/{id}:free", "source": "openrouter", "upstream_id": f"test/{id}:free",
               "context_length": 128000, "max_output": 8192, "revision": None, "parameters": {}}
              for id in ("a", "b", "c", "d", "coder")]
    models.append({"id": "groq/openai/gpt-oss-120b:free", "source": "groq", "upstream_id": "openai/gpt-oss-120b",
                   "context_length": 131072, "max_output": 4096, "revision": None, "parameters": {"reasoning_effort": "low"}})
    rows = [qualification(m) for m in models]
    # Keep the established recovery fixtures in A -> Groq -> B order, based on
    # measured test evidence rather than provider special-casing.
    rows[-1].update(trials=100, passed=99)
    for row in rows[1:4]:
        row.update(trials=100, passed=98)
    async def rankings(self):
        return {"models": rows, "health": {}}
    monkeypatch.setattr(LocalCapacity, "rankings", rankings)
    return rows
