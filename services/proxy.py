import asyncio
from urllib.parse import quote
import httpx
from fastapi import Request, WebSocket
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketState

try:                                                    # websockets >= 14
    from websockets.asyncio.client import connect as ws_connect
    _WS_HDR = "additional_headers"
except ImportError:                                     # websockets < 14
    from websockets import connect as ws_connect
    _WS_HDR = "extra_headers"

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade"}
client = httpx.AsyncClient(timeout=None, follow_redirects=False)


async def forward(request: Request, port: int, path: str, prefix: str = ""):
    url = f"http://127.0.0.1:{port}/{quote(path)}"
    if request.url.query:
        url += f"?{request.url.query}"
    # keep the original Host so url_for()/request.base_url build public URLs, not 127.0.0.1:<port>
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
    headers += [("x-forwarded-host", request.headers.get("host", "")),
                ("x-forwarded-proto", request.url.scheme)]
    has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
    try:
        r = await client.send(
            client.build_request(request.method, url, headers=headers,
                                 content=request.stream() if has_body else None),
            stream=True)
    except httpx.ConnectError:
        return PlainTextResponse("This app is not running. Start it from the gateway dashboard.",
                                 status_code=503)
    out = []
    for k, v in r.headers.multi_items():
        if k.lower() in HOP or k.lower() == "host":
            continue
        if k.lower() == "location" and prefix and v.startswith("/"):
            v = prefix + v                      # keep redirects inside the app's prefix
        out.append((k.encode(), v.encode()))
    resp = StreamingResponse(r.aiter_raw(), status_code=r.status_code,
                             background=BackgroundTask(r.aclose))
    resp.raw_headers = out                      # keeps duplicate headers (Set-Cookie)
    return resp


WS_FORWARD = {"cookie", "authorization", "user-agent", "accept-language"}


async def forward_ws(ws: WebSocket, port: int, path: str):
    """Bridge a browser WebSocket to ws://127.0.0.1:<port>/<path> (text, binary, subprotocols, close codes)."""
    url = f"ws://127.0.0.1:{port}/{quote(path)}"
    if ws.url.query:
        url += f"?{ws.url.query}"
    headers = [(k, v) for k, v in ws.headers.items() if k.lower() in WS_FORWARD]
    headers += [("x-forwarded-host", ws.headers.get("host", "")),
                ("x-forwarded-proto", "https" if ws.url.scheme == "wss" else "http")]
    kw = {_WS_HDR: headers, "subprotocols": ws.scope.get("subprotocols") or None,
          "open_timeout": 10, "max_size": None, "ping_interval": None}
    if ws.headers.get("origin"):
        kw["origin"] = ws.headers["origin"]
    try:
        up = await ws_connect(url, **kw)
    except Exception as e:                              # app stopped / refused the handshake
        print(f"ws upstream {url} failed: {e!r}")
        await ws.close(code=1011)                       # pre-accept close -> HTTP 403 to the browser
        return
    await ws.accept(subprotocol=up.subprotocol)

    async def client_to_app():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("text") is not None:
                await up.send(m["text"])
            elif m.get("bytes") is not None:
                await up.send(m["bytes"])

    async def app_to_client():
        async for m in up:
            if isinstance(m, str):
                await ws.send_text(m)
            else:
                await ws.send_bytes(m)

    tasks = [asyncio.create_task(client_to_app()), asyncio.create_task(app_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        code = getattr(up, "close_code", None)
        await up.close()
        if ws.application_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close(code=code if code and code not in (1005, 1006, 1015) else 1000)
            except Exception:
                pass


class PortForwarder:
    """One LAN-facing listener per 'port'-mode app: raw TCP <-> 127.0.0.1:<app port>.
    Needs no DNS (open it by whatever address reached the dashboard), keeps the app at its own root,
    and being byte-level it carries HTTP, WebSocket and SSE unchanged."""

    def __init__(self, resolve):
        self.resolve = resolve                          # app id -> current internal port
        self.servers: dict[int, tuple[str, asyncio.AbstractServer]] = {}
        self.failed: set[int] = set()

    async def sync(self, wanted: dict[int, str]):
        for pp in [p for p, (aid, _) in self.servers.items() if wanted.get(p) != aid]:
            self.servers.pop(pp)[1].close()             # open connections keep running
        for pp, aid in wanted.items():
            if pp in self.servers:
                continue
            try:
                srv = await asyncio.start_server(lambda r, w, aid=aid: self._handle(aid, r, w), port=pp)
                self.servers[pp] = (aid, srv)
                self.failed.discard(pp)
            except OSError as e:
                if pp not in self.failed:               # report once, retry silently on later syncs
                    print(f"port-mode listener for '{aid}' on :{pp} failed: {e}")
                    self.failed.add(pp)

    async def close(self):
        for _, srv in self.servers.values():
            srv.close()
        self.servers.clear()

    async def _handle(self, aid, reader, writer):
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.resolve(aid)), 5)
        except Exception:
            body = b"This app is not running. Start it from the gateway dashboard."
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n"
                         b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await self._close(writer)
            return
        await asyncio.gather(self._pipe(reader, uw), self._pipe(ur, writer))
        await asyncio.gather(self._close(uw), self._close(writer))

    @staticmethod
    async def _pipe(r, w):
        try:
            while chunk := await r.read(65536):
                w.write(chunk)
                await w.drain()
            if w.can_write_eof():
                w.write_eof()                           # half-close: let the other direction finish
        except (OSError, asyncio.CancelledError):
            w.close()

    @staticmethod
    async def _close(w):
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass