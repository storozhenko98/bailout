"""Free-only routing policy. Independent of FastAPI and the Workers transport."""
import asyncio
import codecs
import json
import re
from contextlib import aclosing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic

ORIGIN = "https://openrouter.ai/api/v1"
MODEL_ID = re.compile(r"^[\w.-]+/[\w.-]+:free$")
MAX_BODY = 512_000
REQUEST_SECONDS = 120
AUTO_ATTEMPT_SECONDS = 40
PINNED_ATTEMPT_SECONDS = 90
COOLDOWN_SECONDS = 300
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
    def __init__(self, status, message, *, code="request_failed", recoverable=None):
        self.status, self.message = status, message
        self.code = code
        self.recoverable = status in (502, 504) if recoverable is None else recoverable
        self.failed_models = []
        self.retry_after_seconds = None
        super().__init__(message)

    def payload(self):
        result = {"error": self.message, "code": self.code, "failed_models": self.failed_models,
                  "cooldown_seconds": max(COOLDOWN_SECONDS, self.retry_after_seconds or 0)}
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result


def upstream_failure(status, result):
    """Classify without exposing or storing provider bodies (which can echo prompts)."""
    error = result.get("error", {}) if isinstance(result, dict) else {}
    if not isinstance(error, dict):
        error = {}
    metadata = error.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    kind = metadata.get("error_type")
    code = error.get("code")
    if isinstance(code, int):
        status = code
    if status == 401 or kind == "authentication":
        return Failure(503, "OpenRouter could not authenticate the hosted service. Try again later.", code="upstream_authentication", recoverable=False)
    if status == 402 or kind in {"payment_required", "token_limit_exceeded"}:
        return Failure(503, "OpenRouter's account allowance is exhausted. No paid fallback was used.", code="upstream_quota", recoverable=False)
    if status == 403 or kind in {"permission_denied", "content_policy_violation", "refusal"}:
        return Failure(503, "OpenRouter or the provider blocked this request under its access or content policy. No model switch was attempted.", code="upstream_policy", recoverable=False)
    if status == 429 or kind == "rate_limit_exceeded":
        # Unknown scope fails closed. Only a clearly provider-scoped limit can
        # justify trying another model; account/key/daily limits must not be retried.
        source = str(metadata.get("limit_source", "")).lower()
        scope = str(metadata.get("scope", "")).lower()
        message = str(error.get("message", "")).lower()
        global_limit = (source.startswith("openrouter") or scope in {"account", "key", "global"}
                        or any(s in message for s in ("daily", "per day", "per-day", "account", "api key", "credits")))
        provider_limit = (bool(metadata.get("provider_name")) and not global_limit
                          and source in {"", "provider", "provider_rate_limit"} and scope in {"", "provider", "model"})
        return Failure(429, "The provider is busy." if provider_limit else "OpenRouter's shared rate limit was reached. Wait before retrying.",
                       code="provider_rate_limited" if provider_limit else "upstream_rate_limited", recoverable=provider_limit)
    if kind in {"invalid_request", "invalid_prompt", "context_length_exceeded", "string_too_long", "payload_too_large"} or status in (400, 413, 422):
        return Failure(400, "The model could not accept this conversation. Try a smaller task or /new.", code="invalid_model_request", recoverable=False)
    retry = status in (404, 408, 500, 502, 503, 504) or kind in {"timeout", "provider_overloaded", "provider_unavailable", "server"}
    return Failure(502, "The provider could not complete the response.", code="provider_unavailable", recoverable=retry)


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
        and (model.get("context_length") or 0) >= 32768
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
        and (endpoint.get("context_length") or 0) >= 32768
        and isinstance(uptime, (int, float, Decimal)) and 95 <= uptime <= 100
        and (recent is None or (isinstance(recent, (int, float, Decimal)) and 95 <= recent <= 100))
    )


def ability(model):
    text = (model["id"] + " " + model.get("description", "")).lower()
    score, reasons = 20, ["native tools"]
    if re.search(r"coding agent|agentic coding|coding model", text):
        score += 40
        reasons.append("coding specialist")
    elif re.search(r"coding|programming|code generation", text):
        score += 20
        reasons.append("coding")
    if "reasoning" in text:
        score += 10
        reasons.append("reasoning")
    if re.search(r"terminal-bench|swe-bench", text):
        score += 10
        reasons.append("coding evaluation mentioned")
    if model.get("context_length", 0) >= 128000:
        score += 5
    if re.search(r"mini|small|nano|\bxs\b", model["id"]):
        score -= 5
    if re.search(r"advises against.*coding|not (?:suited|recommended).*coding", text):
        score -= 60
        reasons.append("coding discouraged")
    return score, reasons


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
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise Failure(400, "Expected 1–256 messages. Use /new for a fresh conversation.")
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


def check_message(result):
    if not isinstance(result, dict):
        raise Failure(502, "Invalid model response.")
    if result.get("usage", {}).get("cost") is not None and not zero(result["usage"]["cost"]):
        raise Failure(502, "Unexpected upstream cost. Stopped; investigate the provider.", code="unexpected_cost", recoverable=False)
    if result.get("error"):
        raise upstream_failure(502, result)
    choices = result.get("choices") or []
    if not choices:
        raise Failure(502, "The model returned no response. Try another free model.")
    choice = choices[0]
    if choice.get("error"):
        raise upstream_failure(502, choice)
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
    def __init__(self, transport, key):
        self.transport, self.key = transport, key
        self.deadline = None

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
        return sorted(filter(free_model, result["data"]), key=lambda m: (-ability(m)[0], m["id"]))

    async def endpoints(self, model):
        result = await self.metadata(f'/models/{model["id"]}/endpoints')
        data = result.get("data", {})
        if data.get("id") != model["id"]:
            return []
        return [e for e in data.get("endpoints", []) if healthy(e, model["id"])]

    async def models(self):
        semaphore = asyncio.Semaphore(5)
        async def inspect(model):
            async with semaphore:
                try:
                    live = await self.endpoints(model)
                except Failure:
                    live = []
                score, reasons = ability(model)
                return {"id": model["id"], "name": model.get("name", model["id"]), "context_length": model["context_length"],
                        "score": score, "reasons": reasons, "available": bool(live), "providers": len(live),
                        "uptime": float(max(e["uptime_last_30m"] for e in live)) if live else None}
        rows = await asyncio.gather(*(inspect(m) for m in (await self.catalog())[:35]))
        rows.sort(key=lambda m: (not m["available"], -m["score"], m["id"]))
        return {"models": rows, "default": next((m["id"] for m in rows if m["available"]), None),
                "ranking": "coding metadata heuristic, not benchmark scores", "checked_at": now()}

    def body(self, model, endpoints, messages, stream):
        caps = [e["max_completion_tokens"] for e in endpoints if isinstance(e.get("max_completion_tokens"), int) and e["max_completion_tokens"] > 0]
        # Automatic tool choice: ordinary conversation never needs to execute a command.
        return {"model": model["id"], "messages": messages, "tools": [BASH_TOOL], "stream": stream,
                "max_tokens": min([8192] + caps),
                "provider": {"only": list(dict.fromkeys(e["tag"] for e in endpoints)), "allow_fallbacks": False,
                             "require_parameters": True, "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}}}

    async def prepare(self, data):
        self.deadline = monotonic() + REQUEST_SECONDS
        if not self.key:
            raise Failure(503, "The hosted service is not configured.")
        catalog = await self.catalog()
        if data["model"] == "auto":
            candidates = [m for m in catalog if m["id"] not in data.get("avoid_models", [])]
            preferred = data.get("preferred_model")
            candidates.sort(key=lambda m: m["id"] != preferred)  # stable ability order
            candidates = candidates[:35]
        else:
            candidates = [m for m in catalog if m["id"] == data["model"]]
        if not candidates:
            raise Failure(503, "No eligible free model is available. Wait for recovery or choose another with /model.", code="no_free_models")
        return candidates

    async def completion_events(self, data, candidates):
        attempts, failed = 0, []
        cooldown = COOLDOWN_SECONDS
        auto = data["model"] == "auto"
        last = Failure(503, "No healthy verified-free provider is available. Try again later.", code="no_free_models")
        try:
            for candidate in candidates:
                self.remaining()
                if attempts >= 3:
                    break
                # The initial catalog is fresh for attempt one. Every fallback
                # gets a new catalog AND endpoint check, including session hints.
                model = candidate if attempts == 0 else next((m for m in await self.catalog() if m["id"] == candidate["id"]), None)
                if model is None:
                    continue
                live = await self.endpoints(model)
                if not live:
                    continue
                attempts += 1
                response = None
                completed = None
                timeout = min(AUTO_ATTEMPT_SECONDS if auto else PINNED_ATTEMPT_SECONDS, self.remaining())
                messages = data["messages"]
                if attempts > 1 or (auto and data.get("preferred_model") not in (None, model["id"])):
                    # Provider-specific reasoning signatures cannot be transferred
                    # to another model. Keep every user message and completed tool result.
                    messages = [{k: v for k, v in m.items() if k != "reasoning_details"} for m in messages]
                yield {"type": "model", "model": model["id"], "attempt": attempts}
                try:
                    async with asyncio.timeout(timeout):
                        response = await self.transport.request(ORIGIN + "/chat/completions", method="POST", timeout=timeout,
                            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                                     "HTTP-Referer": "https://bailout.dev", "X-OpenRouter-Title": "bailout"},
                            body=json.dumps(self.body(model, live, messages, data["stream"])))
                        if response.status != 200:
                            status = response.status
                            if status in (400, 401, 402, 403, 413, 422):
                                raise upstream_failure(status, {})
                            try:
                                error = await read_json(response, 64_000)
                            except Failure:
                                error = {}
                            failure = upstream_failure(status, error)
                            retry_after = getattr(response, "retry_after", None)
                            if isinstance(retry_after, str) and retry_after.isdigit():
                                failure.retry_after_seconds = min(int(retry_after), 86400)
                            raise failure
                        if data["stream"]:
                            async for item in self.read_stream(model["id"], response):
                                if item["type"] == "done":
                                    completed = item
                                else:
                                    yield item
                        else:
                            result = await read_json(response, 2_000_000)
                            message = check_message(result)
                            completed = {"type": "done", "model": model["id"], "message": message,
                                   "usage": {"cost": 0 if result.get("usage", {}).get("cost") is not None else None},
                                   "checked_at": now()}
                except Failure as exc:
                    last = exc
                except TimeoutError:
                    last = upstream_failure(429, {}) if response is not None and response.status == 429 else Failure(504, "The model did not respond in time.", code="provider_timeout")
                except Exception:
                    last = upstream_failure(429, {}) if response is not None and response.status == 429 else Failure(502, "The provider connection failed or returned an invalid response.", code="provider_unavailable")
                finally:
                    if response is not None:
                        try:
                            await response.close()
                        except Exception:
                            pass
                if completed is not None:
                    completed.update(failed_models=failed, cooldown_seconds=cooldown)
                    yield completed
                    return
                if not last.recoverable:
                    raise last
                failed.append(model["id"])
                cooldown = max(cooldown, last.retry_after_seconds or 0)
                if not auto:
                    raise Failure(last.status, last.message + " Your selected model is unchanged. Use /model auto for automatic recovery.", code=last.code, recoverable=False)
                if attempts < 3:
                    # A model event with additive fields remains readable by v0.4.
                    # Clients must discard partial text/tool calls from this attempt.
                    yield {"type": "model", "model": model["id"], "retry": True,
                           "notice": f"{model['id']} failed. Trying another free model…",
                           "failed_models": failed.copy(), "cooldown_seconds": cooldown}
            raise Failure(503, f"Automatic recovery stopped after {attempts} model attempt(s). No working free model was found. Try again later.",
                          code="recovery_exhausted", recoverable=False) if attempts else last
        except Failure as exc:
            exc.failed_models = failed
            if failed:
                exc.retry_after_seconds = max(exc.retry_after_seconds or 0, cooldown)
            raise

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

    async def read_stream(self, model, response):
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
                        raise upstream_failure(502, frame)
                    if frame.get("usage"):
                        usage = frame["usage"]
                        if usage.get("cost") is not None and not zero(usage["cost"]):
                            raise Failure(502, "Unexpected upstream cost. Stopped; investigate the provider.", code="unexpected_cost", recoverable=False)
                    for choice in frame.get("choices", [])[:1]:
                        finish = choice.get("finish_reason") or finish
                        if choice.get("error"):
                            raise upstream_failure(502, choice)
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
            checked = check_message(result)
            yield {"type": "done", "model": model, "message": checked, "usage": {"cost": 0 if usage.get("cost") is not None else None}, "checked_at": now()}
        finally:
            try:
                await response.close()
            except Exception:
                pass


def event(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def now():
    return datetime.now(timezone.utc).isoformat()
