"""Free-only routing policy. Independent of FastAPI and the Workers transport."""
import asyncio
import codecs
import json
import re
import random
from contextlib import aclosing
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from capacity import LocalCapacity
from context import MAX_BODY, MAX_MESSAGES, budget, quota_tokens
from providers import Providers, ORIGINS, fingerprint
from ranking import rank, qualified

ORIGIN = "https://openrouter.ai/api/v1"
MODEL_ID = re.compile(r"^[\w.-]+/(?:[\w.-]+/)*[\w.-]+:free$")
REQUEST_SECONDS = 120
AUTO_ATTEMPT_SECONDS = 40
PINNED_ATTEMPT_SECONDS = 90
COOLDOWN_SECONDS = 300
MAX_ATTEMPTS = 4
MAX_RETRY_WAIT = 10
MAX_CAPACITY_WAIT = 20
GROQ_ORIGIN = "https://api.groq.com/openai/v1"
GROQ_MODEL = "groq/openai/gpt-oss-120b:free"
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a Bash command on the user's machine. Each call starts a fresh shell. Use workdir to choose a directory. Commands run without confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 1800000},
                "interactive": {"type": "boolean", "description": "Hand the real terminal to the user for login, password entry, sudo, or interactive installers. Input and output stay local; only exit status is returned. Requires an interactive bailout session."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


class Failure(Exception):
    def __init__(self, status, message, *, code="request_failed", recoverable=None, scope="model"):
        self.status, self.message = status, message
        self.code = code
        self.scope = scope
        self.recoverable = status in (502, 504) if recoverable is None else recoverable
        self.failed_models = []
        self.retry_after_seconds = None
        self.provider_error_code = None
        self.diagnostic = None  # Evaluation-only, fixed metadata; never bodies.
        super().__init__(message)

    def payload(self):
        result = {"error": self.message, "code": self.code, "failed_models": self.failed_models,
                  "cooldown_seconds": max(COOLDOWN_SECONDS, self.retry_after_seconds or 0),
                  "docs": "https://bailout.dev/docs/#service-limits"}
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
            result["resets_at"] = (datetime.now(timezone.utc) + timedelta(seconds=self.retry_after_seconds)).isoformat()
            # Older clients display only this string, not structured retry fields.
            result["error"] += f" Retry after {self.retry_after_seconds} seconds."
        if self.provider_error_code is not None:
            result["provider_error_code"] = self.provider_error_code
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic
        return result


def upstream_failure(status, result, source=None):
    """Classify without exposing or storing provider bodies (which can echo prompts)."""
    error = result.get("error", {}) if isinstance(result, dict) else {}
    if not isinstance(error, dict):
        error = {}
    metadata = error.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    kind = metadata.get("error_type") or error.get("type")
    code = error.get("code")
    if source == "groq" and (code == "tool_use_failed" or "failed_generation" in error):
        # Groq reports model-generated invalid tool calls as request errors,
        # including inside an otherwise successful HTTP stream. These are model
        # failures, not bad user input or a temporarily unreachable provider.
        failure = Failure(502, "The model could not generate a valid Bash call. No commands were run.",
                          code="invalid_tool_response", recoverable=True)
        failure.provider_error_code = "tool_use_failed"
        return failure
    if source == "zai" and re.fullmatch(r"\d{4}", str(code)):
        # ZAI uses HTTP 429 for balance/plan failures as well as overload.
        # Classify its documented business codes without exposing the body.
        business = str(code)
        if business in {"1113", "1308", "1310"}:
            failure = Failure(429, "ZAI's free allowance is unavailable. No paid fallback was used.", code="upstream_quota", recoverable=True, scope="provider")
            failure.retry_after_seconds = 3600
        elif business == "1311":
            failure = Failure(503, "This ZAI model is unavailable on the service account.", code="provider_unavailable", recoverable=True)
            failure.retry_after_seconds = 3600
        elif business == "1313":
            failure = Failure(503, "ZAI restricted this request under its account policy.", code="upstream_policy", recoverable=False)
        elif business in {"1302", "1305"}:
            failure = Failure(429, "ZAI's free capacity is temporarily busy. No paid fallback was used.", code="provider_rate_limited", recoverable=True, scope="provider")
            failure.retry_after_seconds = 10
        else:
            failure = upstream_failure(status, {"error": {k: v for k, v in error.items() if k != "code"}})
        failure.provider_error_code = business
        return failure
    if isinstance(code, int) and 100 <= code <= 599:
        status = code
    if status == 401 or kind == "authentication":
        return Failure(503, "A model provider could not authenticate the hosted service.", code="upstream_authentication", recoverable=True, scope="provider")
    if status == 402 or kind in {"payment_required", "token_limit_exceeded"}:
        return Failure(503, "A provider's free allowance is exhausted. No paid fallback was used.", code="upstream_quota", recoverable=True, scope="provider")
    if status == 403 or kind in {"permission_denied", "content_policy_violation", "refusal"}:
        return Failure(503, "OpenRouter or the provider blocked this request under its access or content policy. No model switch was attempted.", code="upstream_policy", recoverable=False)
    if status == 429 or kind == "rate_limit_exceeded":
        # An ambiguous 429 is not proof the whole account is exhausted. Allow
        # one delayed retry, then another model, all through the shared meter.
        limit_source = str(metadata.get("limit_source", "")).lower()
        scope = str(metadata.get("scope", "")).lower()
        message = str(error.get("message", "")).lower()
        # Upgrade suggestions can mention credits on a temporary 429. Only
        # explicit exhaustion is evidence of a depleted account. Vercel's free
        # tier throttles individual models; a sibling may still be available.
        depleted_balance = (kind in {"insufficient_quota", "insufficient_credits", "credit_balance_exhausted"}
                            or bool(re.search(r"(?:insufficient|exhausted|no remaining|out of) credits?\b|credits? (?:balance )?(?:is )?(?:exhausted|depleted)", message)))
        exhausted = (any(s in message for s in ("daily", "per day", "per-day"))
                     or "daily" in limit_source or kind == "daily_limit_exceeded" or depleted_balance)
        global_limit = (limit_source.startswith("openrouter") or scope in {"account", "key", "global"}
                        or any(s in message for s in ("account", "api key"))
                        or depleted_balance or exhausted and source != "vercel")
        provider_limit = (bool(metadata.get("provider_name")) and not global_limit
                          and limit_source in {"", "provider", "provider_rate_limit"} and scope in {"", "provider", "model"})
        failure = Failure(429, "Free model capacity is temporarily busy. No paid fallback was used.",
                          code="upstream_quota" if exhausted else "provider_rate_limited" if provider_limit else "upstream_rate_limited",
                          recoverable=True, scope="provider" if global_limit else "model")
        if exhausted:
            failure.retry_after_seconds = 3600
        elif source == "vercel" and kind == "rate_limit_exceeded":
            failure.retry_after_seconds = 60
        return failure
    if kind in {"context_length_exceeded", "string_too_long", "payload_too_large"} or code in {"context_length_exceeded", "context_window_exceeded"}:
        return Failure(400, "This model has insufficient context room. Switching to a larger qualified free model…", code="context_exceeded", recoverable=True, scope="request")
    if kind in {"invalid_request", "invalid_prompt"} or status in (400, 413, 422):
        return Failure(400, "The model could not accept this conversation. Try a smaller task or /new.", code="invalid_model_request", recoverable=False)
    retry = status in (404, 408, 500, 502, 503, 504) or kind in {"timeout", "provider_overloaded", "provider_unavailable", "server"}
    return Failure(502, "The provider could not complete the response.", code="provider_unavailable", recoverable=retry)


def retry_seconds(value):
    """Accept both forms of Retry-After; never shorten the provider's delay."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value) if re.fullmatch(r"\d+(?:\.\d+)?", value.strip()) else (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        return max(1, min(86400, int(seconds + .999)))
    except (ValueError, TypeError, OverflowError):
        return None


def zero(value):
    if isinstance(value, bool) or value is None:
        return False
    if not isinstance(value, (str, int, float, Decimal)):
        return False
    try:
        return Decimal(str(value)).is_zero()
    except InvalidOperation:
        return False


def free_pricing(pricing):
    return (
        isinstance(pricing, dict)
        and zero(pricing.get("prompt"))
        and zero(pricing.get("completion"))
        and all((isinstance(v, str) or k == "discount") and zero(v) for k, v in pricing.items())
    )


def free_model(model):
    return (
        isinstance(model, dict)
        and bool(MODEL_ID.fullmatch(model.get("id", "")))
        and free_pricing(model.get("pricing"))
        and "tools" in model.get("supported_parameters", [])
        and type(model.get("context_length")) is int and model["context_length"] >= 32768
    )


def healthy(endpoint, model_id):
    uptime = endpoint.get("uptime_last_30m")
    recent = endpoint.get("uptime_last_5m")
    return (
        endpoint.get("model_id") == model_id
        and endpoint.get("status") == 0
        and free_pricing(endpoint.get("pricing"))
        and {"tools", "max_tokens"}.issubset(endpoint.get("supported_parameters", []))
        and isinstance(endpoint.get("tag"), str) and bool(endpoint["tag"])
        and type(endpoint.get("context_length")) is int and endpoint["context_length"] >= 32768
        and isinstance(uptime, (int, float, Decimal)) and 95 <= uptime <= 100
        and (recent is None or (isinstance(recent, (int, float, Decimal)) and 95 <= recent <= 100))
    )


def validate(data):
    if not isinstance(data, dict) or set(data) - {"model", "messages", "stream", "preferred_model", "avoid_models"}:
        raise Failure(400, "Unsupported request field.")
    model = data.get("model", "auto")
    if not isinstance(model, str) or (model != "auto" and not MODEL_ID.fullmatch(model)):
        raise Failure(400, "Choose auto or an explicit :free model.")
    if not isinstance(data.get("stream", False), bool):
        raise Failure(400, "stream must be a boolean.")
    preferred = data.get("preferred_model")
    avoid = data.get("avoid_models", [])
    if preferred is not None and (not isinstance(preferred, str) or not MODEL_ID.fullmatch(preferred)):
        raise Failure(400, "preferred_model must be an explicit :free model.")
    if not isinstance(avoid, list) or len(avoid) > 35 or any(not isinstance(m, str) or not MODEL_ID.fullmatch(m) for m in avoid):
        raise Failure(400, "avoid_models must contain at most 35 explicit :free models.")
    if model != "auto" and (preferred is not None or avoid):
        raise Failure(400, "Routing preferences apply only to auto. A pinned model stays pinned.")
    messages = data.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise Failure(400, "Conversation has too many messages. Use /new for a fresh conversation.")
    pending = set()
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content", "tool_calls", "tool_call_id", "reasoning_details"}:
            raise Failure(400, "Unsupported message field.")
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise Failure(400, "Invalid message role.")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise Failure(400, "Messages must contain text only.")
        if role == "tool":
            if message.get("tool_call_id") not in pending:
                raise Failure(400, "Unmatched tool result.")
            pending.remove(message["tool_call_id"])
        elif pending:
            raise Failure(400, "Missing tool results.")
        calls = message.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 16:
                raise Failure(400, "Invalid tool calls.")
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in pending:
                    raise Failure(400, "Invalid tool call.")
                function = call.get("function", {})
                if function.get("name") != "bash" or not isinstance(function.get("arguments"), str):
                    raise Failure(400, "Only Bash is supported.")
                pending.add(call["id"])
        if "reasoning_details" in message and (role != "assistant" or not isinstance(message["reasoning_details"], list)):
            raise Failure(400, "Invalid reasoning details.")
    if pending:
        raise Failure(400, "Missing tool results.")
    return {"model": model, "messages": messages, "stream": data.get("stream", False),
            "preferred_model": preferred, "avoid_models": avoid}


async def read_json(response, limit=8_000_000):
    raw = bytearray()
    try:
        async for chunk in response.chunks():
            raw.extend(chunk)
            if len(raw) > limit:
                raise Failure(502, "Upstream response is too large.")
    finally:
        try:
            await response.close()
        except Exception:
            pass
    try:
        return json.loads(raw, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise Failure(502, "Invalid upstream response.") from None


def check_message(result, source=None):
    if not isinstance(result, dict):
        raise Failure(502, "Invalid model response.")
    if result.get("usage", {}).get("cost") is not None and not zero(result["usage"]["cost"]):
        raise Failure(502, "Unexpected upstream cost. Stopped; investigate the provider.", code="unexpected_cost", recoverable=False)
    if result.get("error"):
        raise upstream_failure(502, result, source)
    choices = result.get("choices") or []
    if not choices:
        raise Failure(502, "The model returned no response. Try another free model.")
    choice = choices[0]
    if choice.get("error"):
        raise upstream_failure(502, choice, source)
    if choice.get("finish_reason") in ("content_filter", "refusal") or choice.get("message", {}).get("refusal"):
        raise upstream_failure(403, {})
    if choice.get("finish_reason") == "length":
        raise Failure(502, "Model response reached its length limit. Try a smaller task or /new.", code="response_too_long", recoverable=False)
    if choice.get("finish_reason") not in ("stop", "tool_calls"):
        raise Failure(502, "The model did not finish its response.")
    message = choice.get("message", {})
    if message.get("role") != "assistant" or (message.get("content") is not None and not isinstance(message["content"], str)):
        raise Failure(502, "Invalid model response.")
    answer = {"role": "assistant", "content": message.get("content")}
    calls = message.get("tool_calls")
    if calls:
        if not isinstance(calls, list) or len(calls) > 16:
            raise Failure(502, "Invalid model tool calls.")
        ids = set()
        for call in calls:
            function = call.get("function", {})
            if call.get("type") != "function" or function.get("name") != "bash" or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids:
                raise Failure(502, "Invalid tool call. No commands were run.")
            ids.add(call["id"])
            try:
                args = json.loads(function.get("arguments", ""))
            except (ValueError, TypeError):
                raise Failure(502, "Invalid Bash arguments. No commands were run.") from None
            if not isinstance(args, dict) or not isinstance(args.get("command"), str) or not args["command"].strip():
                raise Failure(502, "The model returned an empty Bash command.")
            if "workdir" in args and not isinstance(args["workdir"], str):
                raise Failure(502, "Invalid working directory.")
            if "interactive" in args and not isinstance(args["interactive"], bool):
                raise Failure(502, "Invalid interactive flag.")
            if "timeout_ms" in args and (type(args["timeout_ms"]) is not int or not 1 <= args["timeout_ms"] <= 1800000):
                raise Failure(502, "Invalid command timeout.")
        answer["tool_calls"] = calls
    elif not (message.get("content") or "").strip():
        raise Failure(502, "The model returned an empty response. Try another model.")
    if isinstance(message.get("reasoning_details"), list):
        answer["reasoning_details"] = message["reasoning_details"]
    return answer


class Router:
    def __init__(self, transport, key, *, capacity=None, provider_keys=None, accounts=None, sleep=asyncio.sleep):
        self.transport, self.key = transport, key
        self.capacity = capacity or LocalCapacity()
        self.providers = Providers(transport, provider_keys, accounts)
        self.sleep = sleep
        self.deadline = None
        self.snapshot = {"models": [], "health": {}}
        self.evaluation = False

    def remaining(self):
        remaining = self.deadline - monotonic() if self.deadline is not None else REQUEST_SECONDS
        if remaining <= 0:
            raise Failure(504, "Recovery time limit reached. Try again later.", code="recovery_exhausted", recoverable=False)
        return remaining

    async def metadata(self, path):
        try:
            timeout = min(12, self.remaining())
            async with asyncio.timeout(timeout):
                response = await self.transport.request(ORIGIN + path, headers={"Cache-Control": "no-cache, no-store"}, timeout=timeout)
                if response.status != 200:
                    await response.close()
                    raise Failure(503, "Live free pricing is unavailable. Try again shortly.", code="pricing_unavailable", recoverable=False)
                return await read_json(response)
        except Failure:
            raise
        except Exception:
            raise Failure(503, "Could not verify free pricing. No inference was sent.") from None

    async def catalog(self):
        result = await self.metadata("/models")
        if not isinstance(result.get("data"), list):
            raise Failure(503, "Model catalog is unavailable.")
        models = []
        for raw in filter(free_model, result["data"]):
            model = {**raw, "source": "openrouter", "upstream_id": raw["id"],
                     "max_output": (raw.get("top_provider") or {}).get("max_completion_tokens") or 8192,
                     "revision": raw.get("created"), "parameters": {}}
            model["fingerprint"] = fingerprint(model)
            models.append(model)
        return models

    async def discover(self):
        rows, issue = [], None
        if self.key:
            try:
                rows = await self.catalog()
            except Failure as exc:
                issue = exc
        rows.extend(await self.providers.discover())
        if not rows and issue:
            raise issue
        return rows

    async def candidates(self, data=None):
        self.snapshot = await self.capacity.rankings()
        data = data or {}
        models = await self.discover()
        candidates = rank(models, self.snapshot, data.get("preferred_model"), data.get("avoid_models", []))
        # Client failure hints may outlive a brief server cooldown. They must
        # not hide every qualified route after capacity has returned. Shared
        # reservations and cooldowns still gate every attempt below.
        return candidates or rank(models, self.snapshot, data.get("preferred_model"))

    async def endpoints(self, model):
        result = await self.metadata(f'/models/{model["id"]}/endpoints')
        data = result.get("data", {})
        if data.get("id") != model["id"]:
            return []
        return [e for e in data.get("endpoints", []) if healthy(e, model["id"])]

    async def models(self):
        rows = await self.candidates()
        return {"models": [{k: m[k] for k in ("id", "name", "source", "context_length", "quality", "trials", "reliability")} for m in rows],
                "default": rows[0]["id"] if rows else None,
                "ranking": "Bailout setup-and-repair benchmark plus recent route health",
                "ranking_updated_at": self.snapshot.get("generated_at"), "checked_at": now()}

    def body(self, model, endpoints, messages, stream):
        caps = [e["max_completion_tokens"] for e in endpoints if isinstance(e.get("max_completion_tokens"), int) and e["max_completion_tokens"] > 0]
        # Automatic tool choice: ordinary conversation never needs to execute a command.
        return {"model": model["id"], "messages": messages, "tools": [BASH_TOOL], "stream": stream,
                "max_tokens": min([8192] + caps),
                "provider": {"only": list(dict.fromkeys(e["tag"] for e in endpoints)), "allow_fallbacks": False,
                             "require_parameters": True, "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}}}

    async def prepare(self, data):
        self.deadline = monotonic() + REQUEST_SECONDS
        if not self.key and not self.providers.keys:
            raise Failure(503, "The hosted service is not configured.")
        catalog = await self.candidates(data)
        if data["model"] == "auto":
            candidates = catalog
        else:
            candidates = [m for m in catalog if m["id"] == data["model"]]
        if not candidates:
            raise Failure(503, "No eligible free model is available. Please try again later.", code="no_free_models")
        return candidates

    async def completion_events(self, data, candidates):
        attempts, failed, blocked = 0, [], set()
        cooldown, waited = COOLDOWN_SECONDS, 0
        auto = data["model"] == "auto"
        last = Failure(503, "No free model capacity is available. Try again later.", code="free_capacity_exhausted", recoverable=False)
        shortest_wait = None
        context_skipped = []
        quota_skipped = []
        notices = []
        try:
            for index, candidate in enumerate(candidates):
                source = candidate.get("source", "openrouter")
                if source in blocked:
                    continue
                if attempts >= MAX_ATTEMPTS:
                    break
                for retry in range(1 if self.evaluation else 2):
                    self.remaining()
                    if attempts >= MAX_ATTEMPTS:
                        break
                    # Recheck pricing/eligibility for every inference, including
                    # a retry of the same model. Nothing is sent on unknown prices.
                    try:
                        if source != "openrouter":
                            model = await self.providers.refresh(candidate)
                            live = []
                        else:
                            model = next((m for m in await self.catalog() if m["id"] == candidate["id"]), None)
                            live = await self.endpoints(model) if model else []
                        if model is None or (source == "openrouter" and not live):
                            break
                        if self.evaluation and model["fingerprint"] != candidate["fingerprint"]:
                            raise Failure(503, "Evaluation candidate metadata changed. Rediscover before testing.", code="pricing_unavailable", recoverable=False)
                        qualification = next((r for r in self.snapshot.get("models", []) if r["id"] == model["id"]), {})
                        if not self.evaluation and not qualified(qualification, model):
                            break
                    except Exception as exc:
                        if isinstance(exc, Failure) and exc.code == "capacity_unavailable":
                            raise
                        blocked.add(source)
                        last = exc if isinstance(exc, Failure) else Failure(503, "Provider eligibility could not be verified.", code="pricing_unavailable")
                        last.scope = "provider"
                        break
                    messages = data["messages"]
                    if source != "openrouter" or attempts > 0 or (auto and data.get("preferred_model") not in (None, model["id"])):
                        messages = [{k: v for k, v in m.items() if k != "reasoning_details"} for m in messages]
                    fit = budget(model, live, messages, [BASH_TOOL], output=2048 if source == "groq" else 4096)
                    if fit is None:
                        context_skipped.append(model["id"])
                        if not auto:
                            raise Failure(400, "The selected model has insufficient context room. Use Auto or /new.", code="context_exhausted", recoverable=False)
                        break
                    body = (self.body(model, fit["endpoints"], messages, data["stream"]) if source == "openrouter"
                            else self.providers.body(model, messages, data["stream"], fit["output_tokens"]))
                    if source == "openrouter":
                        body["max_tokens"] = fit["output_tokens"]
                    tokens = quota_tokens(messages, [BASH_TOOL], fit["output_tokens"])
                    permit = await self.capacity.reserve(source, model["id"], tokens, **({"quota": model["quota"]} if model.get("quota") else {}))
                    if not permit["ok"]:
                        if permit.get("code") == "route_context_capacity":
                            # Waiting cannot make a request fit a completely
                            # empty token bucket. Preserve history and use a
                            # larger independent free allowance in Auto.
                            quota_skipped.append(model["id"])
                            if not auto:
                                raise Failure(400, "This conversation exceeds the selected provider's free per-request token allowance. Use Auto for a larger free route or /new.", code="context_exhausted", recoverable=False)
                            break
                        delay = permit.get("retry_after_seconds", 0)
                        if delay > 0:
                            shortest_wait = delay if shortest_wait is None else min(shortest_wait, delay)
                        # Try the independent pool first. Only the last remaining
                        # pool waits, and only a bounded time within this request.
                        alternatives = any(m.get("source", "openrouter") != source and m.get("source", "openrouter") not in blocked for m in candidates[index + 1:])
                        if not self.evaluation and permit.get("scope") != "model" and not alternatives and 0 < delay <= MAX_CAPACITY_WAIT - waited and delay + 2 < self.remaining():
                            yield {"type": "model", "model": model["id"], "retry": True, "notice": f"Free capacity is busy. Retrying in {delay}s…"}
                            await self.sleep(delay + random.uniform(.05, .25))
                            waited += delay
                            continue  # Refresh eligibility and prices after waiting.
                        if not permit["ok"]:
                            if permit.get("scope") == "provider" or (not permit.get("scope") and permit.get("code") in {"provider_capacity", "route_context_capacity"}):
                                blocked.add(source)
                            break
                    attempts += 1
                    response, completed = None, None
                    # Reserve fallback time when there are other qualified routes.
                    # With one route, give it the normal response window instead
                    # of aborting early for a fallback that does not exist.
                    alternatives = any(m.get("source", "openrouter") not in blocked for m in candidates[index + 1:])
                    timeout = min(AUTO_ATTEMPT_SECONDS if auto and alternatives else PINNED_ATTEMPT_SECONDS, self.remaining())
                    selected = {"type": "model", "model": model["id"], "provider": source, "attempt": attempts,
                                "context": {k: v for k, v in fit.items() if k != "endpoints"}}
                    if context_skipped:
                        selected.update(retry=True, reason="context_limit", notice=f"Conversation is near the context limit of {context_skipped[-1]}. Switching to {model['id']} with more room; your conversation is preserved.")
                        notices.append(selected["notice"])
                    elif quota_skipped:
                        selected.update(retry=True, reason="provider_token_limit", notice=f"Conversation exceeds the free token allowance for {quota_skipped[-1]}. Switching to {model['id']}; your conversation is preserved.")
                        notices.append(selected["notice"])
                    elif data.get("preferred_model") and model["id"] != data["preferred_model"]:
                        selected.update(retry=True, reason="route_change", notice=f"Switching to {model['id']} for available free capacity.")
                        notices.append(selected["notice"])
                    started = monotonic()
                    yield selected
                    try:
                        async with asyncio.timeout(timeout):
                            response = await self.transport.request((ORIGIN if source == "openrouter" else ORIGINS[source]) + "/chat/completions", method="POST", timeout=timeout,
                                headers={"Authorization": f"Bearer {self.key if source == 'openrouter' else self.providers.keys[source]}", "Content-Type": "application/json",
                                         "HTTP-Referer": "https://bailout.dev", "X-OpenRouter-Title": "bailout"}, body=json.dumps(body))
                            if response.status != 200:
                                status = response.status
                                if status in (401, 402, 403):
                                    raise upstream_failure(status, {})
                                try:
                                    error = await read_json(response, 64_000)
                                except Failure:
                                    error = {}
                                failure = upstream_failure(status, error, source)
                                if self.evaluation:
                                    failure.diagnostic = {"upstream_status": status}
                                if source in {"groq", "mistral"} and status == 429 and not model.get("quota"):
                                    failure.scope = "provider"  # unsplit account bucket
                                failure.retry_after_seconds = retry_seconds(getattr(response, "retry_after", None)) or failure.retry_after_seconds
                                raise failure
                            if data["stream"]:
                                async for item in self.read_stream(model["id"], response, source):
                                    if item["type"] == "done":
                                        completed = item
                                    else:
                                        yield item
                            else:
                                result = await read_json(response, 2_000_000)
                                message = check_message(result, source)
                                completed = {"type": "done", "model": model["id"], "message": message,
                                             "usage": {"cost": 0 if result.get("usage", {}).get("cost") is not None else None},
                                             "tokens": result.get("usage", {}).get("total_tokens"), "checked_at": now()}
                    except Failure as exc:
                        last = exc
                    except TimeoutError:
                        last = upstream_failure(429, {}) if response is not None and response.status == 429 else Failure(504, "The model did not respond in time.", code="provider_timeout")
                    except Exception as exc:
                        last = upstream_failure(429, {}) if response is not None and response.status == 429 else Failure(502, "The provider connection failed or returned an invalid response.", code="provider_unavailable")
                        if self.evaluation:
                            last.diagnostic = {"exception_type": type(exc).__name__[:64]}
                    finally:
                        if response is not None:
                            try:
                                await response.close()
                            except Exception:
                                pass
                        if completed is None:
                            try:
                                await self.capacity.settle(permit.get("permit"), None)
                            except Failure:
                                pass  # An abandoned lease expires after 120s.
                    if completed is not None:
                        usage = completed.pop("tokens", None)
                        # Settlement failure leaves the larger reservation in place.
                        if not isinstance(usage, int) or isinstance(usage, bool) or not 0 <= usage <= 1000000:
                            usage = None
                        try:
                            await self.capacity.settle(permit.get("permit"), usage)
                        except Failure:
                            pass
                        await self.observe(model["id"], "success", started)
                        completed.update(failed_models=failed, cooldown_seconds=cooldown, provider=source, free_only=True,
                                         notices=notices, context={k: v for k, v in fit.items() if k != "endpoints"})
                        yield completed
                        return
                    if last.code == "context_exceeded" and auto:
                        context_skipped.append(model["id"])
                        break  # Request-specific; never poison global model health.
                    if last.code not in {"upstream_policy", "invalid_model_request", "context_exceeded"}:
                        await self.observe(model["id"], last.code, started)
                    if last.code == "unexpected_cost":
                        await self.capacity.cooldown(source, None, 86400)
                    if not last.recoverable:
                        raise last
                    if self.evaluation and last.retry_after_seconds is None and last.code in {
                        "upstream_rate_limited", "provider_rate_limited", "provider_unavailable", "provider_timeout"
                    }:
                        # The benchmark controller retries this exact model
                        # within its own request/time budget. A headerless
                        # transient refusal must not create a five-minute ban.
                        last.retry_after_seconds = 2
                    # Retry temporary 429s once, with jitter and Retry-After. A
                    # daily/account allowance or auth failure needs another pool.
                    delay = last.retry_after_seconds or 2
                    if not self.evaluation and last.status == 429 and last.code != "upstream_quota" and retry == 0 and attempts < MAX_ATTEMPTS and delay <= MAX_RETRY_WAIT and delay + 2 < self.remaining():
                        await self.capacity.cooldown(source, None if last.scope == "provider" else model["id"], delay)
                        yield {"type": "model", "model": model["id"], "retry": True,
                               "notice": f"Model is busy. Retrying in {delay}s…"}
                        await self.sleep(delay + random.uniform(.05, .5))
                        continue
                    if model["id"] not in failed:
                        failed.append(model["id"])
                    cooldown = max(cooldown, last.retry_after_seconds or 0)
                    provider_wide = last.scope == "provider"
                    await self.capacity.cooldown(source, None if provider_wide else model["id"],
                                                 last.retry_after_seconds or (60 if provider_wide else COOLDOWN_SECONDS))
                    if provider_wide:
                        blocked.add(source)
                    if not auto:
                        last.message += " Your selected model is unchanged. Use Auto for automatic recovery."
                        raise last
                    if provider_wide and not any(m.get("source", "openrouter") not in blocked for m in candidates):
                        raise last
                    yield {"type": "model", "model": model["id"], "retry": True,
                           "notice": "Trying another available free model…", "failed_models": failed.copy(), "cooldown_seconds": cooldown}
                    break
            if context_skipped and (attempts == 0 or last.code == "context_exceeded") and shortest_wait is None:
                raise Failure(400, "No qualified free model currently has enough context room for this conversation. Your history was preserved. Use /new for a fresh task.", code="context_exhausted", recoverable=False)
            if quota_skipped and attempts == 0 and shortest_wait is None:
                raise Failure(400, "This conversation exceeds every available provider's free token allowance. Waiting will not make it fit. Your history was preserved. Use /new for a smaller task.", code="context_exhausted", recoverable=False)
            if shortest_wait is not None:
                last = Failure(429, "Bailout's free model capacity is busy. No paid fallback was used.", code="free_capacity_exhausted", recoverable=False)
                last.retry_after_seconds = shortest_wait
                raise last
            if last.scope == "provider" or not attempts:
                raise last
            exhausted = Failure(503, f"No free route completed after {attempts} attempt(s). {last.message} No paid fallback was used.", code="recovery_exhausted", recoverable=False)
            exhausted.retry_after_seconds = max(60, last.retry_after_seconds or 0)
            raise exhausted
        except Failure as exc:
            exc.failed_models = failed
            raise

    async def observe(self, model, outcome, started):
        try:
            await self.capacity.record(model, outcome, int((monotonic() - started) * 1000))
        except Failure:
            pass  # Losing aggregate health must not discard a completed response.

    async def chat(self, data):
        candidates = await self.prepare(data)
        async with aclosing(self.completion_events(data, candidates)) as events:
            async for item in events:
                if item["type"] == "done":
                    return {k: v for k, v in item.items() if k != "type"}

    async def stream_chat(self, data, candidates):
        try:
            async with aclosing(self.completion_events(data, candidates)) as events:
                async for item in events:
                    yield event(item)
        except Failure as exc:
            yield event({"type": "error", **exc.payload()})

    async def stream(self, model, response):
        """Single-attempt stream adapter, also used by protocol tests."""
        try:
            yield event({"type": "model", "model": model})
            async for item in self.read_stream(model, response):
                yield event(item)
        except Failure as exc:
            yield event({"type": "error", **exc.payload()})
        except Exception:
            yield event({"type": "error", "error": "Invalid model stream. No commands were run."})

    async def read_stream(self, model, response, source=None):
        """Forward real text deltas; execute tools only after a validated final event."""
        message = {"role": "assistant", "content": ""}
        calls, reasoning, usage = {}, {}, {}
        finish, ended, size, buffer = None, False, 0, ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            async for chunk in response.chunks():
                size += len(chunk)
                if size > 2_000_000:
                    raise Failure(502, "Model response is too large. No commands were run.")
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        ended = True
                        continue
                    if not payload:
                        continue
                    frame = json.loads(payload, parse_float=Decimal)
                    if frame.get("error"):
                        failure = upstream_failure(502, frame, source)
                        if self.evaluation:
                            failure.diagnostic = {"stream_error": True}
                        raise failure
                    if frame.get("usage") or frame.get("x_groq", {}).get("usage"):
                        usage = frame.get("usage") or frame["x_groq"]["usage"]
                        if usage.get("cost") is not None and not zero(usage["cost"]):
                            raise Failure(502, "Unexpected upstream cost. Stopped; investigate the provider.", code="unexpected_cost", recoverable=False)
                    for choice in frame.get("choices", [])[:1]:
                        finish = choice.get("finish_reason") or finish
                        if choice.get("error"):
                            raise upstream_failure(502, choice, source)
                        delta = choice.get("delta") or {}
                        if delta.get("refusal"):
                            raise upstream_failure(403, {})
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            message["content"] += text
                            yield {"type": "text", "text": text}
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index")
                            if not isinstance(index, int) or not 0 <= index < 16:
                                raise Failure(502, "Invalid streamed tool call.")
                            target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if call.get("id"):
                                target["id"] = call["id"]
                            if call.get("type"):
                                target["type"] = call["type"]
                            for key in ("name", "arguments"):
                                fragment = call.get("function", {}).get(key)
                                if fragment:
                                    target["function"][key] += fragment
                        for detail in delta.get("reasoning_details") or []:
                            index = detail.get("index", 0)
                            target = reasoning.setdefault(index, {})
                            for key, value in detail.items():
                                if key in ("text", "data", "summary") and isinstance(value, str):
                                    target[key] = target.get(key, "") + value
                                else:
                                    target[key] = value
            if not ended or not finish:
                raise Failure(502, "The model connection closed early. No commands were run.")
            if calls:
                message["tool_calls"] = [calls[i] for i in sorted(calls)]
            if reasoning:
                message["reasoning_details"] = list(reasoning.values())
            result = {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}
            checked = check_message(result, source)
            yield {"type": "done", "model": model, "message": checked, "usage": {"cost": 0 if usage.get("cost") is not None else None}, "checked_at": now(), "tokens": usage.get("total_tokens")}
        finally:
            try:
                await response.close()
            except Exception:
                pass


def event(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def now():
    return datetime.now(timezone.utc).isoformat()
