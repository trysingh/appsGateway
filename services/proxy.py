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