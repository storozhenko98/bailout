import json
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from router import Failure, MAX_BODY, Router, validate
from transport import Transport

app = FastAPI(title="bailout API", version="0.5.0", responses={429: {"description": "Fair-use or upstream capacity limit. Honor Retry-After; see https://bailout.dev/docs/#service-limits."}, 503: {"description": "Shared hosting allowance exhausted (code: budget_exhausted), service paused, or service unavailable. JSON error, code, retry_after_seconds, resets_at, docs. Do not retry immediately."}}, description="Free-only inference for bailout, the temporary machine setup and recovery harness.")
app.add_middleware(CORSMiddleware, allow_origins=["https://bailout.dev", "http://localhost:4173"], allow_methods=["GET"], allow_headers=[])


def binding(request, name, default=None):
    env = request.scope.get("env")
    return getattr(env, name, default) if env is not None else os.environ.get(name, default)


def router(request):
    return Router(getattr(app.state, "transport", None) or Transport(), binding(request, "OPENROUTER_API_KEY", ""))


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
    return {"ok": True, "service": "bailout", "version": "0.5.0", "framework": "FastAPI", "free_only": True, "configured": bool(binding(request, "OPENROUTER_API_KEY"))}


@app.get("/", tags=["service"])
async def root():
    return {"name": "bailout API", "docs": "/docs", "site": "https://bailout.dev"}


@app.get("/v1/models", tags=["models"])
async def models(request: Request):
    return await router(request).models()


@app.post("/v1/chat", tags=["chat"], summary="Ask a verified free model",
    description="Auto may try three verified-free models within 120 seconds. Pinned models never switch. Streaming model events with retry=true mark discarded attempts; execute Bash only from the final validated done message. Account, policy, price-verification and hosting limits stop recovery.",
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False, "required": ["messages"],
        "properties": {"model": {"type": "string", "default": "auto", "description": "auto or an explicit vendor/model:free ID"},
                       "stream": {"type": "boolean", "default": False},
                       "preferred_model": {"type": "string", "description": "Auto only: last successful explicit :free model. Still rechecked for free pricing and health."},
                       "avoid_models": {"type": "array", "maxItems": 35, "items": {"type": "string"}, "description": "Auto only: temporarily avoid these failed :free models. No identity or session token required."},
                       "messages": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "object"}}},
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
