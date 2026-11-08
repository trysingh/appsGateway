import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
import controllers
from services.process_manager import pm
from services.proxy import forward


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


app.include_router(controllers.router)
