"""Private binding to the same durable ledger used by the public gateway."""
import asyncio
import json


class Capacity:
    def __init__(self, binding, *, diagnostics=False):
        self.binding = binding
        self.diagnostics = diagnostics

    async def call(self, action, **data):
        from router import Failure
        if self.binding is None:
            raise Failure(503, "Shared provider capacity is unavailable. No inference was sent.", code="capacity_unavailable", recoverable=False)
        try:
            async with asyncio.timeout(5):
                stub = self.binding.getByName("global-v1")
                response = await stub.fetch(
                    "https://capacity/capacity/" + action,
                    method="POST", body=json.dumps(data))
                if response.status != 200:
                    raise ValueError("Capacity service unavailable")
                return json.loads(await response.text())
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            failure = Failure(503, "Shared provider capacity could not be checked. No further inference was sent.",
                              code="capacity_busy" if timed_out else "capacity_unavailable", recoverable=False)
            if timed_out:
                # Stop this attempt without inference. A later CLI request can
                # retry admission; any ambiguous reservation remains counted
                # conservatively and its concurrency lease expires normally.
                failure.retry_after_seconds = 5
            if self.diagnostics:
                failure.diagnostic = {"capacity_exception_type": type(exc).__name__[:64]}
            raise failure from None

    async def reserve(self, provider, model, tokens, quota=None):
        return await self.call("reserve", provider=provider, model=model, tokens=tokens, **({"quota": quota} if quota else {}))

    async def settle(self, permit, tokens):
        if permit is not None:
            await self.call("settle", permit=permit, tokens=tokens)

    async def cooldown(self, provider, model, seconds):
        return await self.call("cooldown", provider=provider, model=model, seconds=seconds)

    async def rankings(self):
        return await self.call("rankings")

    async def record(self, model, outcome, latency_ms):
        return await self.call("outcome", model=model, outcome=outcome, latency_ms=latency_ms)


class LocalCapacity:
    """Local development/test transport; production always uses the binding."""
    async def reserve(self, provider, model, tokens, quota=None):
        return {"ok": True, "permit": None}

    async def settle(self, permit, tokens):
        pass

    async def cooldown(self, provider, model, seconds):
        pass

    async def rankings(self):
        return {"models": [], "health": {}}

    async def record(self, model, outcome, latency_ms):
        pass
