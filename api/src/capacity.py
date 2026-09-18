"""Private binding to the same durable ledger used by the public gateway."""
import asyncio
import json
import uuid


class Capacity:
    fair_queue = True

    def __init__(self, binding, *, diagnostics=False):
        self.binding = binding
        self.diagnostics = diagnostics
        self.ticket = None
        self.routes = None

    async def start(self, routes):
        self.routes = routes
        if routes:
            await self._join()

    async def _join(self):
        from router import Failure
        self.ticket = self.ticket or str(uuid.uuid4())
        result = await self.call("join", ticket=self.ticket, routes=self.routes)
        if not result.get("ok"):
            failure = Failure(429, "Bailout's waiting queue is full. Your conversation is preserved; try again shortly.",
                              code="free_capacity_exhausted", recoverable=False)
            failure.retry_after_seconds = result.get("retry_after_seconds", 15)
            raise failure

    async def waiting(self):
        if not self.routes:
            return {"ok": False, "code": "queue_empty", "retry_after_seconds": 0}
        if not self.ticket:
            await self._join()
        result = await self.call("peek", ticket=self.ticket)
        if result.get("code") == "queue_expired":
            await self._join()
            result = await self.call("peek", ticket=self.ticket)
        return result

    async def discard(self, *, provider=None, model=None):
        if self.routes is None:
            return
        self.routes = [route for route in self.routes if not
                       ((provider is not None and route["provider"] == provider) or
                        (model is not None and route["model"] == model))]
        if not self.routes:
            await self.close()
        elif self.ticket:
            await self._join()

    async def close(self):
        if self.ticket:
            ticket, self.ticket = self.ticket, None
            await self.call("leave", ticket=ticket)

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
        if self.routes is not None:
            # Endpoint output caps and stripped provider reasoning can change
            # the estimate since discovery. Waiting priority must use the same
            # requirements as the actual, freshly checked reservation.
            current = {"provider": provider, "model": model, "tokens": tokens, **({"quota": quota} if quota else {})}
            updated = [current if route["model"] == model else route for route in self.routes]
            if not any(route["model"] == model for route in updated):
                updated.append(current)
            changed = updated != self.routes
            self.routes = updated
            if changed or not self.ticket:
                await self._join()
        result = await self.call("reserve", provider=provider, model=model, tokens=tokens,
                                 **({"quota": quota} if quota else {}), **({"ticket": self.ticket} if self.ticket else {}))
        if result.get("ok"):
            self.ticket = None  # The ledger consumed this turn atomically.
        elif result.get("code") == "queue_expired" and self.routes:
            await self._join()
        return result

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
    fair_queue = False

    async def start(self, routes):
        pass

    async def discard(self, *, provider=None, model=None):
        pass

    async def close(self):
        pass

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
