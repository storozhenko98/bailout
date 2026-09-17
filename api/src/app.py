import json
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from router import Failure, MAX_BODY, Router, validate
from transport import Transport

app = FastAPI(title="bailout API", version="0.3.0", description="Free-only inference for bailout, the temporary machine setup and recovery harness.")
app.add_middleware(CORSMiddleware, allow_origins=["https://bailout.bailout-router.workers.dev", "http://localhost:4173"], allow_methods=["GET"], allow_headers=[])


def binding(request, name, default=None):
    env = request.scope.get("env")
    return getattr(env, name, default) if env is not None else os.environ.get(name, default)


def router(request):
    return Router(getattr(app.state, "transport", None) or Transport(), binding(request, "OPENROUTER_API_KEY", ""))


async def limited(request, name, key):
    limiter = binding(request, name)
    if limiter:
        from pyodide.ffi import to_js
        from js import Object
        result = await limiter.limit(to_js({"key": key}, dict_converter=Object.fromEntries))
        if not result.success:
            raise Failure(429, "Free capacity is busy. Try again in a minute.")


@app.exception_handler(Failure)
async def failure_handler(request, exc):
    return JSONResponse({"error": exc.message}, status_code=exc.status, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def limits(request, call_next):
    if request.url.path.startswith("/v1/"):
        try:
            await limited(request, "IP_LIMIT", request.headers.get("CF-Connecting-IP", "local"))
            if request.url.path == "/v1/chat":
                await limited(request, "SHARED_LIMIT", "inference")
        except Failure as exc:
            return await failure_handler(request, exc)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health", tags=["service"])
async def health(request: Request):
    return {"ok": True, "service": "bailout", "version": "0.3.0", "framework": "FastAPI", "free_only": True, "configured": bool(binding(request, "OPENROUTER_API_KEY"))}


@app.get("/", tags=["service"])
async def root():
    return {"name": "bailout API", "docs": "/docs", "site": "https://bailout.bailout-router.workers.dev"}


@app.get("/v1/models", tags=["models"])
async def models(request: Request):
    return await router(request).models()


@app.post("/v1/chat", tags=["chat"], summary="Ask a verified free model",
    description="Automatic tool choice: the model may answer directly or request Bash. With stream=true, returns NDJSON model/text/done/error events. Execute tools only from a validated done message. Every request rechecks free model prices and provider health.",
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False, "required": ["messages"],
        "properties": {"model": {"type": "string", "default": "auto", "description": "auto or an explicit vendor/model:free ID"},
                       "stream": {"type": "boolean", "default": False},
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
        model, response = await route.open_completion(data)
        return StreamingResponse(route.stream(model, response), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})
    return await route.chat(data)
