from urllib.parse import quote
import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

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
