"""Small provider adapters; every returned candidate still needs qualification.

Provider SDKs and LiteLLM are deliberately unnecessary. Pricing, account policy,
protocol normalization, quality, and quota admission remain separate gates.
"""
import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

ORIGINS = {"groq": "https://api.groq.com/openai/v1", "mistral": "https://api.mistral.ai/v1",
           "zai": "https://api.z.ai/api/paas/v4", "vercel": "https://ai-gateway.vercel.sh/v1"}
ZAI_PRICING = "https://docs.z.ai/guides/overview/pricing.md"
# Protocol/model-family mappings, not intelligence rankings. Context is read
# live from the provider's documentation and price rows before each attempt.
ZAI_DOCS = {"glm-4.5-flash": "https://docs.z.ai/guides/llm/glm-4.5.md",
            "glm-4.7-flash": "https://docs.z.ai/guides/llm/glm-4.7.md"}


def fingerprint(model):
    fields = {k: model.get(k) for k in ("id", "source", "upstream_id", "context_length", "max_output", "revision", "parameters")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def account_verified(policy, now=None):
    """Expiring operator attestation, not an inference that a valid key is free."""
    now = now or datetime.now(timezone.utc)
    try:
        checked = datetime.fromisoformat(policy["verified_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
        return (policy["tier"] == "free" and policy["billing_disabled"] is True
                and policy["topups_disabled"] is True and checked <= now < expires
                and 0 < (expires - checked).total_seconds() <= 31 * 86400)
    except (KeyError, TypeError, ValueError):
        return False


def zero_prices(pricing):
    from router import zero
    return (isinstance(pricing, dict) and zero(pricing.get("input")) and zero(pricing.get("output"))
            and all(zero(v) for k, v in pricing.items() if k != "varies_by_provider"))


def zai_free_rows(text):
    """Exact four-column Free rows only. A free cache is not free inference."""
    found = set()
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].lower() in ZAI_DOCS and all(c == "Free" for c in cells[1:]):
            found.add(cells[0].lower())
    return found


def card(text, title):
    match = re.search(r'<Card\s+title="' + re.escape(title) + r'"[^>]*>\s*(\d+)([Kk]?)\s*</Card>', text)
    return int(match[1]) * (1000 if match[2] else 1) if match else 0


class Providers:
    def __init__(self, transport, keys=None, accounts=None):
        self.transport, self.keys, self.accounts = transport, keys or {}, accounts or {}

    async def fetch(self, url, *, provider=None, text=False):
        from router import Failure, read_json
        headers = {"Cache-Control": "no-cache, no-store"}
        if provider:
            headers["Authorization"] = "Bearer " + self.keys[provider]
        async with asyncio.timeout(10):
            response = await self.transport.request(url, headers=headers, timeout=10)
            if response.status != 200:
                await response.close()
                raise Failure(503, "Provider eligibility could not be verified.", code="pricing_unavailable")
            if not text:
                return await read_json(response)
            raw = bytearray()
            try:
                async for chunk in response.chunks():
                    raw.extend(chunk)
                    if len(raw) > 500_000:
                        raise ValueError("Metadata too large")
                return raw.decode()
            finally:
                await response.close()

    async def catalog(self, provider):
        if not self.keys.get(provider):
            return []
        if provider in {"groq", "mistral"} and not account_verified(self.accounts.get(provider, {})):
            return []
        if provider == "zai":
            free = zai_free_rows(await self.fetch(ZAI_PRICING, text=True))
            rows = []
            for id in sorted(free):
                doc = await self.fetch(ZAI_DOCS[id], text=True)
                if "Function Call" not in doc or id.lower() not in doc.lower():
                    continue
                rows.append({"id": id, "context_window": card(doc, "Context Length"),
                             "max_tokens": card(doc, "Maximum Output Tokens")})
        else:
            result = await self.fetch(ORIGINS[provider] + "/models", provider=provider if provider != "vercel" else None)
            rows = result.get("data", []) if isinstance(result, dict) else result
        candidates = []
        if not isinstance(rows, list):
            return []
        for row in rows:
            id = row.get("id", "")
            if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", id) or row.get("active") is False or row.get("archived"):
                continue
            if provider == "vercel" and not (zero_prices(row.get("pricing")) and "free" in row.get("tags", []) and "tools" in row.get("supported_parameters", [])):
                continue
            if provider == "mistral" and not (row.get("capabilities", {}).get("function_calling") and row.get("capabilities", {}).get("completion_chat")):
                continue
            # Groq lacks a tools flag in /models. Discovery is permissive;
            # qualification must actually exercise native Bash tool calls.
            window = row.get("context_window") or row.get("max_context_length") or 0
            if type(window) is not int or window < 32768:
                continue
            params = {"reasoning_effort": "low"} if provider == "groq" and id.startswith("openai/gpt-oss-") else {}
            if provider == 'groq':
                # GPT-OSS and Qwen 3.8 lack parallel tool support. Explicitly
                # request sequential calls instead of the API's true default.
                params['parallel_tool_calls'] = False
            if provider == "zai":
                params = {"thinking": {"type": "disabled"}}
            model = {"id": f"{provider}/{id}:free", "upstream_id": id, "source": provider,
                     "name": row.get("name", id), "context_length": window,
                     "max_output": row.get("max_completion_tokens") or row.get("max_tokens") or 4096,
                     # Mistral's OpenAI-compatible `created` is generated at
                     # catalog request time, not a model revision. Using it
                     # would invalidate qualification on every refresh.
                     "revision": row.get("root") or (row.get("version") if provider == "mistral" else row.get("created")), "parameters": params}
            # Some free accounts expose separate quotas for each model. Only
            # an explicitly verified account configuration can split that pool.
            quota = self.accounts.get(provider, {}).get("limits_by_model", {}).get(id)
            if provider in {"groq", "mistral"} and quota:
                model["quota"] = quota
            model["fingerprint"] = fingerprint(model)
            candidates.append(model)
        return candidates

    async def discover(self):
        names = [p for p in ORIGINS if self.keys.get(p)]
        results = await asyncio.gather(*(self.catalog(p) for p in names), return_exceptions=True)
        return [model for result in results if isinstance(result, list) for model in result]

    async def refresh(self, candidate):
        return next((m for m in await self.catalog(candidate["source"]) if m["id"] == candidate["id"]), None)

    def body(self, model, messages, stream, output):
        # Opaque reasoning fields are never portable. Mistral additionally
        # requires nine-character alphanumeric tool IDs: remap pairs together.
        messages = [{k: v for k, v in m.items() if k != "reasoning_details"} for m in messages]
        if model["source"] == "mistral":
            messages = json.loads(json.dumps(messages))
            ids = {}
            for message in messages:
                for call in message.get("tool_calls", []):
                    ids.setdefault(call["id"], f"b{len(ids):08d}")
                    call["id"] = ids[call["id"]]
                if "tool_call_id" in message:
                    message["tool_call_id"] = ids.get(message["tool_call_id"], message["tool_call_id"])
        from router import BASH_TOOL
        body = {"model": model["upstream_id"], "messages": messages, "tools": [BASH_TOOL],
                "stream": stream, "max_tokens": output, **model.get("parameters", {})}
        if model["source"] == "groq":
            body["max_completion_tokens"] = body.pop("max_tokens")
        if model["source"] == "vercel":
            body["providerOptions"] = {"gateway": {"has": ["free"], "models": [model["upstream_id"]]}}
        return body
