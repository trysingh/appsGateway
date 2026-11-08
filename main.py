import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket
import controllers
from services.process_manager import pm
from services.proxy import forward, forward_ws


async def watch_config():
    """Pick up apps.yaml edits within ~2s, no reload click or restart needed."""
    while True:
        await asyncio.sleep(2)
        pm.check_config()


@asynccontextmanager
async def lifespan(_):
    for a in list(pm.apps.values()):
        if a.autostart:
            try:
                pm.start(a.id)          # no-op if the app is already running
            except Exception as e:
                print(f"autostart failed for {a.id}: {e}")
    task = asyncio.create_task(watch_config())
    yield
    task.cancel()
    if pm.settings.stop_apps_on_exit:
        pm.stop_all()


app = FastAPI(title="App Gateway", lifespan=lifespan)


@app.middleware("http")
async def subdomain_router(request: Request, call_next):
    """http://<app_id>.<base_domain>:<port>/... -> app served at its own root (no path prefix)."""
    host = request.headers.get("host", "").split(":")[0]
    suffix = "." + pm.settings.base_domain
    if host.endswith(suffix) and host[: -len(suffix)] in pm.apps:
        aid = host[: -len(suffix)]
        return await forward(request, pm.port(aid), request.url.path.lstrip("/"))
    return await call_next(request)


class WebSocketSubdomainRouter:
    """Pure ASGI: @app.middleware("http") never sees websocket scopes, so
    ws://<app_id>.<base_domain>:<port>/... would fall through to the gateway and 403."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").split(":")[0]
            suffix = "." + pm.settings.base_domain
            if host.endswith(suffix) and host[: -len(suffix)] in pm.apps:
                aid = host[: -len(suffix)]
                await forward_ws(WebSocket(scope, receive, send), pm.port(aid), scope["path"].lstrip("/"))
                return
        await self.app(scope, receive, send)


app.add_middleware(WebSocketSubdomainRouter)
app.include_router(controllers.router)