from __future__ import annotations

import argparse
from io import BytesIO
import ipaddress
from pathlib import Path
from typing import Sequence

from starlette.requests import Request

from .napcat_control import NapCatDashboardControl
from .probes import DashboardProbeConfig, DashboardProbeService
from .operations import DashboardOperations
from .settings import DashboardSettings, SettingsConflict
from .github import DashboardGitHub


DEFAULT_DASHBOARD_PORT = 8787


def create_app(config: DashboardProbeConfig):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, Response
        from fastapi.staticfiles import StaticFiles
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ImportError as exc:
        raise RuntimeError(
            'Dashboard dependencies are missing; install with `pip install -e ".[dashboard]"`'
        ) from exc

    service = DashboardProbeService(config)
    settings = DashboardSettings(config.env_file or config.repository / ".env")
    operations = DashboardOperations(config.state_dir, settings)
    github = DashboardGitHub(config.repository, settings, config.state_dir)
    napcat_control = NapCatDashboardControl(config.napcat_root)
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(
        title="Hikari Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_operator_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (request.headers.get("x-hikari-action") != "dashboard"
                    or request.headers.get("sec-fetch-site") == "cross-site"
                    or (origin is not None and origin.rstrip("/") != str(request.base_url).rstrip("/"))):
                return Response("操作需要从本机 Hikari 面板发起", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.get("/api/status")
    def api_status():
        return service.snapshot()

    @app.get("/api/events")
    def api_events(limit: int = 60):
        return {"events": service.recent_events(limit=limit)}

    @app.get("/api/operations")
    def api_operations():
        return operations.snapshot()

    @app.get("/api/github")
    def api_github():
        return github.snapshot()

    @app.get("/api/github/prs/{number}/gates")
    def api_github_gates(number: int):
        try:
            return github.assessment(number)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.get("/api/settings")
    def api_settings():
        return settings.snapshot()

    @app.put("/api/settings")
    async def api_save_settings(request: Request):
        if len(await request.body()) > 65536:
            raise HTTPException(status_code=413, detail="配置请求过大")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("配置请求必须为对象")
            return settings.save(payload.get("changes"), payload.get("revision"))
        except SettingsConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/api/napcat/qrcode")
    def api_napcat_qrcode():
        snapshot = service.probe_napcat()
        details = snapshot.details or {}
        if details.get("qq_logged_in") is True:
            raise HTTPException(status_code=409, detail="QQ 已登录")
        qrcode_url = details.get("qrcode_url")
        if not isinstance(qrcode_url, str) or not qrcode_url.strip():
            raise HTTPException(status_code=404, detail="NapCat 暂未生成登录二维码")
        try:
            import qrcode
        except ImportError as exc:
            raise RuntimeError(
                'Dashboard QR dependency is missing; install with `pip install -e ".[dashboard]"`'
            ) from exc

        image = qrcode.make(qrcode_url)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            content=buffer.getvalue(),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.post("/api/napcat/qrcode/refresh")
    def api_refresh_napcat_qrcode():
        try:
            napcat_control.refresh_qrcode()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"二维码刷新失败：{type(exc).__name__}",
            ) from None
        return {"ok": True, "message": "已请求 NapCat 刷新二维码"}

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
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--env-file", default=None)
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
        env_file=Path(args.env_file) if args.env_file else None,
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
