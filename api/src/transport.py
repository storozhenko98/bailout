import sys


class LocalResponse:
    def __init__(self, response, client):
        self.response, self.client = response, client
        self.status = response.status_code
        self.retry_after = response.headers.get("Retry-After")

    async def chunks(self):
        async for chunk in self.response.aiter_bytes():
            yield chunk

    async def close(self):
        await self.response.aclose()
        await self.client.aclose()


class EdgeResponse:
    def __init__(self, response, controller):
        self.response, self.controller, self.reader = response, controller, None
        self.status = response.status
        self.retry_after = response.headers.get("Retry-After")

    async def chunks(self):
        self.reader = self.response.body.getReader()
        while True:
            chunk = await self.reader.read()
            if chunk.done:
                break
            yield bytes(chunk.value.to_py())

    async def close(self):
        if self.reader:
            await self.reader.cancel()
            self.reader.releaseLock()
        elif self.response.body:
            await self.response.body.cancel()
        self.controller.abort()


class Transport:
    async def request(self, url, *, method="GET", headers=None, body=None, timeout=12):
        if sys.platform == "emscripten":
            from workers import fetch
            from js import AbortController, AbortSignal
            from pyodide.ffi import to_js
            controller = AbortController.new()
            signal = AbortSignal.any(to_js([controller.signal, AbortSignal.timeout(max(1, int(timeout * 1000)))]))
            response = await fetch(url, method=method, headers=headers or {}, body=body, signal=signal, cache="no-store", redirect="manual")
            return EdgeResponse(response, controller)
        import httpx
        client = httpx.AsyncClient(timeout=timeout)
        try:
            request = client.build_request(method, url, headers=headers, content=body)
            response = await client.send(request, stream=True)
            return LocalResponse(response, client)
        except BaseException:
            await client.aclose()
            raise
