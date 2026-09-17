"""Free-only routing policy. Independent of FastAPI and the Workers transport."""
import asyncio
import codecs
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ORIGIN = "https://openrouter.ai/api/v1"
MODEL_ID = re.compile(r"^[\w.-]+/[\w.-]+:free$")
MAX_BODY = 512_000
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
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


class Failure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


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
    if not isinstance(data, dict) or set(data) - {"model", "messages", "stream"}:
        raise Failure(400, "Only model, messages, and stream are accepted.")
    model = data.get("model", "auto")
    if not isinstance(model, str) or (model != "auto" and not MODEL_ID.fullmatch(model)):
        raise Failure(400, "Choose auto or an explicit :free model.")
    if not isinstance(data.get("stream", False), bool):
        raise Failure(400, "stream must be a boolean.")
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
    return {"model": model, "messages": messages, "stream": data.get("stream", False)}


async def read_json(response, limit=8_000_000):
    raw = bytearray()
    try:
        async for chunk in response.chunks():
            raw.extend(chunk)
            if len(raw) > limit:
                raise Failure(502, "Upstream response is too large.")
    finally:
        await response.close()
    try:
        return json.loads(raw, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise Failure(502, "Invalid upstream response.") from None


def check_message(result):
    if result.get("usage", {}).get("cost") is not None and not zero(result["usage"]["cost"]):
        raise Failure(502, "Unexpected upstream cost. Stopped; investigate the provider.")
    choices = result.get("choices") or []
    if not choices:
        raise Failure(502, "The model returned no response. Try another free model.")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise Failure(502, "Model response was cut short. Try a smaller task or another model.")
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

    async def metadata(self, path):
        try:
            response = await self.transport.request(ORIGIN + path, headers={"Cache-Control": "no-cache, no-store"}, timeout=12)
            if response.status != 200:
                await response.close()
                raise Failure(503, "Live free pricing is unavailable. Try again shortly.")
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

    async def open_completion(self, data):
        if not self.key:
            raise Failure(503, "The hosted service is not configured.")
        catalog = await self.catalog()
        candidates = catalog[:35] if data["model"] == "auto" else [m for m in catalog if m["id"] == data["model"]]
        if not candidates:
            raise Failure(503, "This model is no longer verified free. Choose another with /model.")
        attempts, last_status = 0, 503
        for model in candidates:
            if attempts >= 3:
                break
            if attempts:
                model = next((m for m in await self.catalog() if m["id"] == model["id"]), None)
                if not model:
                    continue
            try:
                live = await self.endpoints(model)
            except Failure:
                continue
            if not live:
                continue
            attempts += 1
            try:
                response = await self.transport.request(ORIGIN + "/chat/completions", method="POST", timeout=90,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                             "HTTP-Referer": "https://bailout.bailout-router.workers.dev", "X-OpenRouter-Title": "bailout"},
                    body=json.dumps(self.body(model, live, data["messages"], data["stream"])))
            except Exception:
                raise Failure(504, "The model timed out. Try again or choose another with /model.") from None
            if response.status == 200:
                return model["id"], response
            last_status = response.status
            await response.close()
            if last_status in (401, 402, 403):
                raise Failure(503, "OpenRouter rejected the service key or account policy. No paid fallback was used.")
            if last_status not in (404, 408, 429, 500, 502, 503, 504):
                break
        raise Failure(429 if last_status == 429 else 503,
                      "Free capacity is busy. Try again shortly or use /model to choose another. Paid models are never used.")

    async def chat(self, data):
        model, response = await self.open_completion(data)
        result = await read_json(response, 2_000_000)
        if result.get("error"):
            raise Failure(502, "The provider could not finish this request. Try another model.")
        message = check_message(result)
        return {"model": model, "message": message, "usage": {"cost": 0 if result.get("usage", {}).get("cost") is not None else None}, "checked_at": now()}

    async def stream(self, model, response):
        """Forward real text deltas; execute tools only after a validated final event."""
        message = {"role": "assistant", "content": ""}
        calls, reasoning, usage = {}, {}, {}
        finish, ended, size, buffer = None, False, 0, ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        yield event({"type": "model", "model": model})
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
                        raise Failure(502, "The provider interrupted the response. Try again or use /model.")
                    if frame.get("usage"):
                        usage = frame["usage"]
                    for choice in frame.get("choices", [])[:1]:
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            message["content"] += text
                            yield event({"type": "text", "text": text})
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
            yield event({"type": "done", "model": model, "message": checked, "usage": {"cost": 0 if usage.get("cost") is not None else None}, "checked_at": now()})
        except Failure as exc:
            yield event({"type": "error", "error": exc.message})
        except Exception:
            yield event({"type": "error", "error": "The model connection failed. No commands were run. Try again or use /model."})
        finally:
            await response.close()


def event(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def now():
    return datetime.now(timezone.utc).isoformat()
