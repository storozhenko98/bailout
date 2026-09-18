import json
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from router import Failure, MAX_BODY, Router, validate, REQUEST_SECONDS
from time import monotonic
from transport import Transport
from capacity import Capacity, LocalCapacity

app = FastAPI(title="bailout API", version="0.7.0", responses={429: {"description": "Fair-use or upstream capacity limit. Honor Retry-After; see https://bailout.dev/docs/#service-limits."}, 503: {"description": "Shared hosting allowance exhausted (code: budget_exhausted), service paused, or service unavailable. JSON error, code, retry_after_seconds, resets_at, docs. Do not retry immediately."}}, description="Free-only inference for bailout, the temporary machine setup and recovery harness.")
app.add_middleware(CORSMiddleware, allow_origins=["https://bailout.dev", "http://localhost:4173"], allow_methods=["GET"], allow_headers=[])


def binding(request, name, default=None):
    env = request.scope.get("env")
    return getattr(env, name, default) if env is not None else os.environ.get(name, default)


def router(request):
    capacity = Capacity(binding(request, "CAPACITY"), diagnostics=request.url.path.startswith("/internal/bench/")) if request.scope.get("env") is not None else LocalCapacity()
    try:
        accounts = json.loads(binding(request, "FREE_ACCOUNTS", "{}"))
        if not isinstance(accounts, dict):
            accounts = {}
    except (ValueError, TypeError):
        accounts = {}
    return Router(getattr(app.state, "transport", None) or Transport(), binding(request, "OPENROUTER_API_KEY", ""),
                  capacity=capacity, accounts=accounts,
                  provider_keys={p: binding(request, name, "") for p, name in {
                      "groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY", "zai": "ZAI_API_KEY",
                      "vercel": "VERCEL_AI_GATEWAY_API_KEY"}.items() if binding(request, name, "")})


@app.exception_handler(Failure)
async def failure_handler(request, exc):
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(exc.payload(), status_code=exc.status, headers=headers)


@app.middleware("http")
async def limits(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health", tags=["service"])
async def health(request: Request):
    return {"ok": True, "service": "bailout", "version": "0.7.0", "framework": "FastAPI", "free_only": True, "configured": bool(binding(request, "OPENROUTER_API_KEY"))}


@app.get("/", tags=["service"])
async def root():
    return {"name": "bailout API", "docs": "/docs", "site": "https://bailout.dev"}


@app.get("/v1/models", tags=["models"])
async def models(request: Request):
    return await router(request).models()


@app.post("/v1/chat", tags=["chat"], summary="Ask a verified free model",
    description="Auto reserves shared capacity for every attempt, retries temporary 429s once with backoff, and can try four attempts within 120 seconds. A bounded fair queue gives older compatible requests priority, with up to 90 seconds of waiting inside that deadline. Retries and follow-up model requests join the back. Legacy pinned requests never switch. Streaming model events with retry=true announce recovery or waiting; execute Bash only from the final validated done message. Provider/account exhaustion can use a separately enabled free provider. Policy and hosting limits stop recovery. Unknown prices disable that route.",
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False, "required": ["messages"],
        "properties": {"model": {"type": "string", "default": "auto", "description": "auto or an explicit vendor/model:free ID"},
                       "stream": {"type": "boolean", "default": False},
                       "preferred_model": {"type": "string", "description": "Auto only: last successful explicit :free model. Still rechecked for free pricing and health."},
                       "avoid_models": {"type": "array", "maxItems": 35, "items": {"type": "string"}, "description": "Auto only: temporarily avoid these failed :free models. No identity or session token required."},
                       "messages": {"type": "array", "minItems": 1, "maxItems": 2048, "items": {"type": "object"}}},
        "example": {"model": "auto", "messages": [{"role": "user", "content": "Hello!"}], "stream": False}
    }}}}})
async def chat(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise Failure(413, "This conversation is too large. Use /new to start fresh.")
    try:
        data = validate(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        raise Failure(400, "Invalid JSON body.") from None
    route = router(request)
    if data["stream"]:
        candidates = await route.prepare(data)
        return StreamingResponse(route.stream_chat(data, candidates), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})
    return await route.chat(data)


def require_evaluation(request):
    # The Python Worker has no public route. The gateway authenticates the
    # operator token and strips this header from all public requests.
    if request.scope.get("env") is None or request.headers.get("X-Bailout-Evaluation") != "authorized":
        raise Failure(404, "Not found.", recoverable=False)


@app.get("/internal/bench/catalog", include_in_schema=False)
async def evaluation_catalog(request: Request):
    require_evaluation(request)
    route = router(request)
    return {"candidates": await route.discover(), "snapshot": await route.capacity.rankings()}


@app.post("/internal/bench/chat", include_in_schema=False)
async def evaluation_chat(request: Request):
    require_evaluation(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise Failure(413, "Evaluation conversation is too large.")
    try:
        body = json.loads(raw)
        selected = body.pop("candidate")
        fingerprint = body.pop("fingerprint")
        body["model"] = selected
        body.pop("preferred_model", None)
        body.pop("avoid_models", None)
        data = validate(body)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Failure(400, "Invalid evaluation request.") from None
    route = router(request)
    route.deadline = monotonic() + REQUEST_SECONDS
    route.evaluation = True
    candidates = [m for m in await route.discover() if m["id"] == selected and m["fingerprint"] == fingerprint]
    if not candidates:
        raise Failure(503, "Evaluation candidate is not currently eligible for free inference. Rediscover before testing.", code="pricing_unavailable")
    # Only the quality gate is bypassed. Pricing, context, quotas, and protocol
    # validation are exactly the production path. No cross-model fallback.
    if data["stream"]:
        return StreamingResponse(route.stream_chat(data, candidates), media_type="application/x-ndjson")
    async for item in route.completion_events(data, candidates):
        if item["type"] == "done":
            return item
