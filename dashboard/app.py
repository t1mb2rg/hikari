from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
from typing import Sequence

from .probes import DashboardProbeConfig, DashboardProbeService


def create_app(config: DashboardProbeConfig):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            'Dashboard dependencies are missing; install with `pip install -e ".[dashboard]"`'
        ) from exc

    service = DashboardProbeService(config)
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(
        title="Hikari Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/status")
    def api_status():
        return service.snapshot()

    @app.get("/api/events")
    def api_events(limit: int = 60):
        return {"events": service.recent_events(limit=limit)}

    @app.post("/api/napcat/restart")
    def api_restart_napcat():
        try:
            service.restart_napcat()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"NapCat 重启失败：{type(exc).__name__}",
            ) from None
        return {"ok": True, "message": "已请求重启 NapCat"}

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Hikari dashboard")
    parser.add_argument("repository", nargs="?", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--napcat-root", default=r"D:\NapCat-Shell-v4.18.19")
    parser.add_argument("--napcat-task-name", default="Hikari NapCat Shell")
    parser.add_argument("--onebot-port", type=int, default=8081)
    return parser


def _require_loopback(host: str) -> None:
    normalized = host.strip().casefold()
    if normalized == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("Dashboard v0.1 only accepts a loopback --host") from None
    if not address.is_loopback:
        raise ValueError("Dashboard v0.1 only accepts a loopback --host")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _require_loopback(args.host)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    from resident.windows_host import default_state_dir

    config = DashboardProbeConfig(
        repository=Path(args.repository),
        state_dir=Path(args.state_dir) if args.state_dir else default_state_dir(),
        napcat_root=Path(args.napcat_root),
        napcat_task_name=args.napcat_task_name,
        onebot_port=args.onebot_port,
    )
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            'Dashboard dependencies are missing; install with `pip install -e ".[dashboard]"`'
        ) from exc

    uvicorn.run(
        create_app(config),
        host=args.host,
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
