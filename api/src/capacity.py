"""Private binding to the same durable ledger used by the public gateway."""
import asyncio
import json


class Capacity:
    def __init__(self, binding):
        self.binding = binding

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
        except Exception:
            raise Failure(503, "Shared provider capacity could not be checked. No further inference was sent.", code="capacity_unavailable", recoverable=False) from None

    async def reserve(self, provider, model, tokens):
        return await self.call("reserve", provider=provider, model=model, tokens=tokens)

    async def settle(self, permit, tokens):
        if permit is not None:
            await self.call("settle", permit=permit, tokens=tokens)

    async def cooldown(self, provider, model, seconds):
        return await self.call("cooldown", provider=provider, model=model, seconds=seconds)


class LocalCapacity:
    """Local development/test transport; production always uses the binding."""
    async def reserve(self, provider, model, tokens):
        return {"ok": True, "permit": None}

    async def settle(self, permit, tokens):
        pass

    async def cooldown(self, provider, model, seconds):
        pass
