from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from services.process_manager import pm
from services.proxy import forward, forward_ws

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _get(aid: str):
    if aid not in pm.apps:
        raise HTTPException(404, f"Unknown app '{aid}'")
    return pm.apps[aid]


@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"domain": pm.settings.base_domain})


@router.get("/admin/apps")                       # also works as a service registry for apps
def list_apps(response: Response):
    if pm.config_error:   # config is live-reloaded; surface a bad edit instead of silently ignoring it
        response.headers["X-Config-Error"] = pm.config_error.replace("\n", " ")[:300].encode("latin-1", "replace").decode("latin-1")
    return pm.snapshot()


@router.post("/admin/reload")
def reload_config():
    try:
        pm.reload()
        pm.config_error = ""
    except Exception as e:
        raise HTTPException(400, f"apps.yaml: {e}")
    return pm.snapshot()


@router.post("/admin/apps/{aid}/{action}")
def control(aid: str, action: str):
    _get(aid)
    fn = {"start": pm.start, "stop": pm.stop, "restart": pm.restart}.get(action)
    if not fn:
        raise HTTPException(400, "Action must be start, stop or restart")
    try:
        fn(aid)
    except Exception as e:
        raise HTTPException(400, f"Could not {action} '{aid}': {e}")
    return {"status": pm.status(aid)}


@router.get("/admin/apps/{aid}/logs", response_class=PlainTextResponse)
def logs(aid: str):
    _get(aid)
    return pm.tail(aid)


@router.api_route("/apps/{aid}", methods=METHODS, include_in_schema=False)
@router.api_route("/apps/{aid}/{path:path}", methods=METHODS, include_in_schema=False)
async def proxy(request: Request, aid: str, path: str = ""):
    _get(aid)
    return await forward(request, pm.port(aid), path, prefix=f"/apps/{aid}")


@router.websocket("/apps/{aid}")
@router.websocket("/apps/{aid}/{path:path}")
async def proxy_ws(ws: WebSocket, aid: str, path: str = ""):
    if aid not in pm.apps:
        await ws.close(code=1008)
        return
    await forward_ws(ws, pm.port(aid), path)