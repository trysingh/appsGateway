"""Generic launcher accepting the same command line arguments as uvicorn."""

import argparse
import sys
from pathlib import Path
import uvicorn

DEFAULT_APP_PATH = "main:app"
DEFAULT_PORT = 8002

def parse_args() -> argparse.Namespace:
    """Parse uvicorn-compatible command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generic launcher (uvicorn-compatible)"
    )
    parser.add_argument("--app", default=DEFAULT_APP_PATH, help=f"ASGI app import string (default: {DEFAULT_APP_PATH})")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--reload-dir", default=".", help="Directory to watch when --reload is enabled")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker processes")
    parser.add_argument("--log-level", default="info", help="Log level (default: info)")
    return parser.parse_args()


def main() -> None:
    """Run the ASGI app with uvicorn using the parsed arguments."""
    args = parse_args()
    # Ensure the project root is importable when launched via a script path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    uvicorn.run(
        app=args.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=args.reload_dir,
        workers=args.workers,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
