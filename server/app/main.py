"""ADIPACK MDM — punto de entrada.  Ejecutar:  uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import logging
import logging.handlers
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import api_agent, avisos_web, control_web, web
from .config import get_settings
from .db import Base, create_schema, get_engine
from .models import Admin, Policy
from .security import hash_password

log = logging.getLogger("adipack.mdm")
logging.getLogger("adipack").addHandler(logging.NullHandler())


def setup_logging() -> None:
    """Registro en data/logs/mdm.log (rota a los 5 MB, guarda 5 archivos)."""
    settings = get_settings()
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("adipack")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(log_dir / "mdm.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
        root.setLevel(logging.INFO)


def bootstrap() -> None:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging()
    settings.apk_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    create_schema(engine)
    with Session(engine) as db:
        if db.scalar(select(Admin).limit(1)) is None:
            db.add(Admin(username=settings.initial_admin_user, password_hash=hash_password(settings.initial_admin_password)))
            log.warning("Administrador inicial '%s' creado. CAMBIA LA CONTRASEÑA en /account.", settings.initial_admin_user)
        if db.scalar(select(Policy).limit(1)) is None:
            db.add(Policy(name="PC Planta (sin kiosco)", platform="windows", description="Inventario y comandos básicos",
                          settings={"kiosk_enabled": False, "kiosk_url": "", "kiosk_browser": "edge", "allow_scripts": False,
                                    "inventory_interval_min": 60, "sync_interval_sec": 60}))
        db.commit()
    if settings.secret_key == "CAMBIAR-ESTA-CLAVE":
        log.warning("MDM_SECRET_KEY usa el valor por defecto. Defínela antes de producción.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    bootstrap()
    from .mqtt_bus import bus

    if get_settings().mqtt_enabled:
        bus.start()  # si el puerto está ocupado, el MDM sigue funcionando y la página Avisos lo explica
    try:
        yield
    finally:
        bus.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="ADIPACK MDM", version="1.4.2", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="adipack_mdm",
                       max_age=8 * 3600, same_site="lax", https_only=settings.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    def wants_html(request: Request) -> bool:
        p = request.url.path
        return not (p.startswith("/api/") or p.startswith("/agent/") or p == "/healthz")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (301, 302, 303, 307) and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if not wants_html(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        titles = {400: "Solicitud no válida", 403: "Acceso denegado", 404: "Página no encontrada"}
        return web.render_error(request, exc.status_code, titles.get(exc.status_code, "Error"), str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        ref = uuid.uuid4().hex[:8].upper()
        log.exception("Error no controlado [%s] en %s %s", ref, request.method, request.url.path)
        if not wants_html(request):
            return JSONResponse({"detail": "Error interno del servidor", "ref": ref}, status_code=500)
        return web.render_error(request, 500, "Ocurrió un error inesperado",
                                f"El problema quedó registrado con la referencia {ref} en server/data/logs/mdm.log. "
                                "Vuelve a intentarlo; si se repite, envía esa referencia y el archivo de registro.")

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Miniatura de la pestaña del navegador (los navegadores la piden siempre en /favicon.ico)."""
        return FileResponse(Path(__file__).parent / "static" / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=86400"})
    app.include_router(api_agent.router)
    app.include_router(web.router)
    app.include_router(control_web.router)
    app.include_router(avisos_web.router)
    return app


app = create_app()
