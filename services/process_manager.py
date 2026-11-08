import json, os, socket, subprocess, time
from pathlib import Path
import psutil
from models import AppConfig, load_settings

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)
CONFIG = Path(os.getenv("GATEWAY_CONFIG", BASE / "apps.yaml"))
STATE = BASE / "state.json"        # pid / port / launch settings of started apps


def _alive(rec: dict) -> bool:
    """Same pid AND same start time (guards against pid reuse), not a zombie."""
    try:
        p = psutil.Process(rec["pid"])
        return abs(p.create_time() - rec["ct"]) < 1 and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


class ProcessManager:
    def __init__(self):
        self._popen: dict[str, subprocess.Popen] = {}
        self.config_error = ""
        self._mtime = 0.0
        try:
            self.state: dict[str, dict] = json.loads(STATE.read_text())
        except Exception:
            self.state = {}
        self.state = {k: r for k, r in self.state.items() if _alive(r)}   # adopt survivors, drop the rest
        self.reload()
        self._save()

    # ---- config -------------------------------------------------------------------------
    def reload(self):
        s = load_settings(CONFIG)                      # raises on bad yaml: old config stays active
        apps = {a.id: a for a in s.apps}
        removed = set()
        for aid, rec in list(self.state.items()):
            if aid in apps:
                continue
            if self._is_alive(aid):                    # removed from yaml but still running: keep it
                apps[aid] = AppConfig(**rec["cfg"])    # visible so it can be stopped, never killed silently
                removed.add(aid)
            else:
                self.state.pop(aid, None)
        self.settings, self.apps, self.removed = s, apps, removed
        self._mtime = CONFIG.stat().st_mtime
        self._save()

    def check_config(self):
        try:
            if CONFIG.stat().st_mtime != self._mtime:
                self.reload()
            self.config_error = ""
        except Exception as e:
            self.config_error = str(e)                 # _mtime unchanged -> retried on next tick

    # ---- helpers ------------------------------------------------------------------------
    def _save(self):
        STATE.write_text(json.dumps(self.state))

    def _is_alive(self, aid: str) -> bool:
        rec = self.state.get(aid)
        if not rec:
            return False
        po = self._popen.get(aid)
        if po is not None and po.poll() is not None:   # also reaps our own dead children
            return False
        return _alive(rec)

    def port(self, aid: str) -> int:
        rec = self.state.get(aid)
        return rec["port"] if rec and self._is_alive(aid) else self.apps[aid].port

    def _env(self, app: AppConfig) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
        env.update(app.env)
        env["PYTHONUNBUFFERED"] = "1"
        env["GATEWAY_URL"] = self.settings.gateway_url
        for a in self.apps.values():   # lets apps discover each other with zero code change
            env[f"APP_{a.id.upper().replace('-', '_')}_URL"] = f"http://127.0.0.1:{a.port}"
        return env

    def _cmd(self, app: AppConfig):
        if app.command:
            return app.command.format(port=app.port, python=app.interpreter())
        cmd = [app.interpreter(), "-m", "uvicorn", app.module,
               "--host", "127.0.0.1", "--port", str(app.port)]
        if app.mode == "path":
            cmd += ["--root-path", f"/apps/{app.id}"]
        return cmd

    # ---- lifecycle ----------------------------------------------------------------------
    def start(self, aid: str):
        if self.status(aid) in ("running", "starting"):
            return
        app = self.apps[aid]
        cmd = self._cmd(app)
        # detached from the gateway's console/session so apps outlive the gateway
        flags = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        with open(LOGS / f"{aid}.log", "ab") as log:
            po = subprocess.Popen(cmd, cwd=app.path, env=self._env(app), stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT,
                                  shell=isinstance(cmd, str), **flags)
        try:
            ct = psutil.Process(po.pid).create_time()
        except psutil.NoSuchProcess:
            ct = time.time()                           # died instantly -> shows as crashed
        self._popen[aid] = po
        self.state[aid] = {"pid": po.pid, "ct": ct, "port": app.port,
                           "sig": app.signature(), "cfg": app.model_dump()}
        self._save()

    def stop(self, aid: str):
        rec = self.state.pop(aid, None)
        self._popen.pop(aid, None)
        if rec and _alive(rec):
            try:
                root = psutil.Process(rec["pid"])
                tree = [*root.children(recursive=True), root]
                for x in tree:
                    x.terminate()
                _, alive = psutil.wait_procs(tree, timeout=5)
                for x in alive:
                    x.kill()
            except psutil.NoSuchProcess:
                pass
        if aid in self.removed:                        # app no longer in yaml: it disappears once stopped
            self.removed.discard(aid)
            self.apps.pop(aid, None)
        self._save()

    def restart(self, aid: str):
        if aid in self.removed:
            raise ValueError("it was removed from apps.yaml; add it back or just stop it")
        self.stop(aid)
        self.start(aid)

    def stop_all(self):
        for aid in list(self.state):
            self.stop(aid)

    # ---- status -------------------------------------------------------------------------
    def status(self, aid: str) -> str:
        rec = self.state.get(aid)
        if rec is None:
            return "stopped"
        if not self._is_alive(aid):
            return "crashed"
        with socket.socket() as s:
            s.settimeout(0.2)
            return "running" if s.connect_ex(("127.0.0.1", rec["port"])) == 0 else "starting"

    def snapshot(self) -> list[dict]:
        rows = []
        for a in list(self.apps.values()):
            st, rec = self.status(a.id), self.state.get(a.id)
            live = st in ("running", "starting")
            rows.append({
                "id": a.id, "name": a.label, "status": st,
                "port": rec["port"] if live else a.port,
                "mode": rec["cfg"]["mode"] if live else a.mode,     # link follows how it was launched
                "restart_needed": live and rec["sig"] != a.signature(),
                "removed": a.id in self.removed})
        return rows

    def tail(self, aid: str, n: int = 150) -> str:
        f = LOGS / f"{aid}.log"
        return "".join(f.read_text(errors="replace").splitlines(True)[-n:]) if f.exists() else ""


pm = ProcessManager()
