import hashlib, json, sys, zlib
from pathlib import Path
import yaml
from pydantic import BaseModel, field_validator


class AppConfig(BaseModel):
    id: str
    name: str | None = None
    path: str
    venv: str | None = None
    python: str | None = None
    module: str = "main:app"
    command: str | None = None      # custom launcher, supports {port} and {python}
    port: int | None = None
    env: dict[str, str] = {}
    autostart: bool = False
    mode: str = "host"              # "host" -> http://<id>.<base_domain>/  |  "path" -> /apps/<id>/
                                    # "port" -> http://<any gateway address>:<public_port>/   (no DNS needed)
    public_port: int | None = None  # mode "port": LAN-facing port the gateway listens on for this app

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v):
        import re
        if not re.fullmatch(r"[a-z0-9-]+", v):
            raise ValueError(f"app id '{v}' must be lowercase letters, digits or hyphens (hostnames are case-insensitive)")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, v):
        if v not in ("path", "host", "port"):
            raise ValueError("mode must be 'path', 'host' or 'port'")
        return v

    def signature(self) -> str:
        """Fingerprint of the settings that only apply at launch."""
        keys = ("path", "venv", "python", "module", "command", "port", "mode", "env")
        return hashlib.md5(json.dumps({k: getattr(self, k) for k in keys}, sort_keys=True).encode()).hexdigest()

    @property
    def label(self) -> str:
        return self.name or self.id

    def interpreter(self) -> str:
        if self.python:
            return self.python
        if self.venv:
            v = Path(self.venv)
            for p in (v / "Scripts" / "python.exe", v / "bin" / "python"):
                if p.exists():
                    return str(p)
            raise FileNotFoundError(f"No python found in venv: {v}")
        return sys.executable


class Settings(BaseModel):
    gateway_url: str = "http://127.0.0.1:8000"
    base_domain: str = "localhost"
    port_base: int = 9000
    stop_apps_on_exit: bool = False   # False: apps keep running when the gateway stops
    public_port_base: int = 10000     # mode "port": public ports are auto-picked from base .. base+999
    default_mode: str = "host"        # mode for apps that do not set their own
    apps: list[AppConfig] = []

    @field_validator("default_mode")
    @classmethod
    def _default_mode_ok(cls, v):
        if v not in ("path", "host", "port"):
            raise ValueError("default_mode must be 'path', 'host' or 'port'")
        return v


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = Settings(**raw.get("gateway", {}), apps=raw.get("apps", []))
    used = {a.port for a in s.apps if a.port}
    for a in s.apps:                      # stable port per app id
        if not a.port:
            p = s.port_base + zlib.crc32(a.id.encode()) % 1000
            while p in used:
                p += 1
            a.port = p
            used.add(p)
    used |= {a.port for a in s.apps}
    used |= {a.public_port for a in s.apps if a.public_port}
    for a in s.apps:
        if "mode" not in a.model_fields_set:
            a.mode = s.default_mode
        if not a.public_port:                 # stable public port per app id, never equal to any internal port
            p = s.public_port_base + zlib.crc32(("pub:" + a.id).encode()) % 1000
            while p in used:
                p += 1
            a.public_port = p
            used.add(p)
    return s