"""API que consumen los agentes (Android y PC). Autenticación: Bearer <device_token>."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
import anyio
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db, new_session
from .models import App, Command, Device, EnrollmentToken
from .schemas import CheckinIn, CheckinOut, CommandOut, CommandResultIn, EnrollIn, EnrollOut
from .security import new_token, token_hash
from .services import apk_path, audit, latest_agent_apk, render_policy_for_agent

router = APIRouter()
RESEND_AFTER = timedelta(minutes=5)
MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite devuelve datetimes "naive": los normalizamos a UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def current_device(authorization: str = Header(""), db: Session = Depends(get_db)) -> Device:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Falta token de dispositivo")
    dev = db.scalar(select(Device).where(Device.token_hash == token_hash(authorization[7:].strip())))
    if dev is None or dev.retired:
        raise HTTPException(401, "Dispositivo no reconocido o retirado")
    return dev


@router.post("/api/v1/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn, request: Request, db: Session = Depends(get_db)):
    et = db.scalar(select(EnrollmentToken).where(EnrollmentToken.token == body.enrollment_token))
    who = f"{body.manufacturer} {body.model} (serial {body.serial or '?'}) desde {_ip(request)}"

    def reject(code: int, msg: str):
        # Queda en la Auditoría y en la página del QR: así se ve por qué una tablet no se activó.
        audit(db, "agente", "enroll_rechazado", f"{msg}: {who}")
        db.commit()
        raise HTTPException(code, msg)

    if et is None or et.revoked:
        reject(403, "Código de activación inválido o revocado")
    if et.expires_at and _aware(et.expires_at) < _now():
        reject(403, "Código de activación vencido")
    if et.max_uses and et.uses >= et.max_uses:
        reject(403, "Código de activación agotado (ya se usó todas las veces permitidas)")
    if et.policy.platform != body.platform and not (
        et.policy.platform in {"windows", "linux"} and body.platform in {"windows", "linux"}
    ):
        reject(400, f"Este código es para {et.policy.platform}, no para {body.platform}")

    token = new_token()
    # Re-enrolamiento del mismo hardware: reutilizamos el registro (conserva nombre/ubicación).
    dev = db.scalar(
        select(Device).where(Device.hardware_id == body.hardware_id, Device.platform == body.platform)
    )
    if dev is None:
        count = db.scalar(select(func.count(Device.id)).where(Device.name.like(f"{et.name_prefix}-%"))) or 0
        dev = Device(name=body.name or f"{et.name_prefix}-{count + 1:02d}", platform=body.platform,
                     hardware_id=body.hardware_id, token_hash="")
        db.add(dev)
    dev.token_hash = token_hash(token)
    dev.retired = False
    dev.policy_id = et.policy_id
    dev.manufacturer, dev.model, dev.serial = body.manufacturer, body.model, body.serial
    dev.os_version, dev.agent_version = body.os_version, body.agent_version
    dev.applied_policy_version = 0
    dev.enrolled_at = _now()
    dev.last_seen = _now()
    et.uses += 1
    db.flush()
    audit(db, f"device:{dev.id}", "enroll", f"{dev.name} {body.manufacturer} {body.model} desde {_ip(request)}")
    db.commit()
    return EnrollOut(device_id=dev.id, device_token=token, name=dev.name)


@router.post("/api/v1/checkin", response_model=CheckinOut)
def checkin(body: CheckinIn, request: Request, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    now = _now()
    status = dict(body.status)
    status["remote_ip"] = request.client.host if request.client else ""
    # El inventario pesado (PC) llega cada N minutos: conservamos el último entre check-ins.
    for key in ("inventory", "apps"):
        if key not in status and key in (dev.status or {}):
            status[key] = dev.status[key]
    dev.status = status
    dev.last_seen = now
    dev.applied_policy_version = body.applied_policy_version
    if body.agent_version:
        dev.agent_version = body.agent_version
    if body.os_version:
        dev.os_version = body.os_version

    out_cmds: list[CommandOut] = []
    pending = db.scalars(
        select(Command).where(Command.device_id == dev.id, Command.state.in_(["pending", "sent"])).order_by(Command.id)
    ).all()
    for c in pending:
        if c.state == "sent" and _aware(c.sent_at) and now - _aware(c.sent_at) < RESEND_AFTER:
            continue  # ya entregado, esperando resultado
        if c.attempts >= MAX_ATTEMPTS:
            c.state, c.result, c.finished_at = "failed", "Sin respuesta del dispositivo tras 5 intentos", now
            continue
        c.state, c.sent_at, c.attempts = "sent", now, c.attempts + 1
        out_cmds.append(CommandOut(id=c.id, type=c.type, params=c.params or {}))

    policy = dev.policy
    rendered = None
    if policy is not None and body.applied_policy_version != policy.version:
        rendered = render_policy_for_agent(db, policy, dev)
    db.commit()
    return CheckinOut(
        device_id=dev.id, name=dev.name, policy_version=policy.version if policy else 0,
        policy=rendered, commands=out_cmds, server_time=now.isoformat(),
    )


@router.post("/api/v1/commands/{command_id}/result")
def command_result(command_id: int, body: CommandResultIn, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    cmd = db.get(Command, command_id)
    if cmd is None or cmd.device_id != dev.id:
        raise HTTPException(404, "Comando no encontrado")
    if cmd.state in {"done", "failed", "cancelled"}:
        return {"ok": True, "state": cmd.state}  # idempotente: el agente puede reintentar
    cmd.state = "done" if body.success else "failed"
    cmd.result = body.output
    cmd.finished_at = _now()
    if cmd.type == "wipe" and body.success:
        dev.retired = True
    db.commit()
    return {"ok": True, "state": cmd.state}


@router.get("/api/v1/apps/{app_id}/download")
def download_app(app_id: int, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    app = db.get(App, app_id)
    if app is None or not apk_path(app).exists():
        raise HTTPException(404, "App no encontrada")
    return FileResponse(apk_path(app), media_type="application/vnd.android.package-archive",
                        filename=app.filename, headers={"X-SHA256": app.sha256})


def _log_event(action: str, detail: str) -> None:
    db = new_session()
    try:
        audit(db, "tablet", action, detail)
        db.commit()
    except Exception:  # noqa: BLE001 - el registro nunca debe cortar la descarga
        db.rollback()
    finally:
        db.close()


@router.get("/agent/android.apk")
def agent_apk(request: Request, db: Session = Depends(get_db)):
    """Descarga pública del agente: la usa el asistente de configuración de Android al leer el QR.

    Cada descarga queda registrada (inicio y si terminó completa o se cortó). Es la forma de saber,
    cuando una tablet dice «No se puede configurar», si llegó o no hasta esta PC.
    """
    app = latest_agent_apk(db)
    ip = _ip(request)
    ua = (request.headers.get("user-agent") or "?")[:120]
    if app is None or not apk_path(app).exists():
        _log_event("qr_descarga_error", f"{ip}: el APK del agente no está cargado en el MDM")
        raise HTTPException(404, "APK del agente no cargado")
    path = apk_path(app)
    size = path.stat().st_size
    _log_event("qr_descarga_inicio", f"{ip} pidió el agente v{app.version_name} ({size:,} bytes) · {ua}")

    def done(sent: int, completed: bool) -> None:
        if completed:
            _log_event("qr_descarga_ok", f"{ip} descargó el agente completo ({sent:,} bytes)")
        else:
            _log_event("qr_descarga_cortada", f"{ip}: descarga interrumpida en {sent:,} de {size:,} bytes "
                                              "(señal Wi-Fi débil o la red cortó la conexión)")

    return LoggedFileResponse(path, size, done)


class LoggedFileResponse(Response):
    """Envía un archivo y avisa al terminar si llegó completo o si el cliente cortó la conexión.

    StreamingResponse no sirve para esto: al cortarse la conexión abandona el generador sin cerrarlo
    y nunca nos enteraríamos. Aquí escuchamos «http.disconnect» igual que Starlette, pero el aviso
    final se ejecuta siempre (bloque finally).
    """

    CHUNK = 256 * 1024

    def __init__(self, path, size: int, on_done):
        super().__init__(media_type="application/vnd.android.package-archive", headers={
            "Content-Length": str(size),
            "Content-Disposition": 'attachment; filename="adipack-mdm-agent.apk"',
            "Cache-Control": "no-store",
        })
        self.path, self.size, self.on_done = path, size, on_done

    async def __call__(self, scope, receive, send):
        state = {"sent": 0, "completed": False}

        async def listen_disconnect(tg):
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    tg.cancel_scope.cancel()
                    return

        async def stream(tg):
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            with open(self.path, "rb") as f:
                while chunk := await anyio.to_thread.run_sync(f.read, self.CHUNK):
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    state["sent"] += len(chunk)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            state["completed"] = state["sent"] >= self.size
            tg.cancel_scope.cancel()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(listen_disconnect, tg)
                tg.start_soon(stream, tg)
        finally:
            self.on_done(state["sent"], state["completed"])


@router.get("/prueba-conexion", response_class=HTMLResponse)
def network_test(request: Request):
    """Página pública mínima: se abre desde un celular en el Wi-Fi de planta para probar que llega a esta PC."""
    ip = _ip(request)
    _log_event("prueba_red", f"{ip} abrió la prueba de conexión · {(request.headers.get('user-agent') or '?')[:120]}")
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ADIPACK MDM · Prueba de conexión</title><link rel="icon" href="/static/favicon.ico">
<style>body{{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f4f6f9;color:#1b2b3a}}
.c{{background:#fff;border-radius:16px;padding:32px;max-width:420px;text-align:center;box-shadow:0 4px 24px #0001}}
.ok{{font-size:56px}}h1{{font-size:22px;margin:8px 0}}p{{color:#555;line-height:1.5}}</style></head>
<body><div class="c"><div class="ok">✅</div><h1>Conexión correcta con ADIPACK MDM</h1>
<p>Este equipo (<b>{ip}</b>) llega al servidor. Las tablets conectadas a esta misma red Wi-Fi podrán descargar el agente al escanear el QR.</p></div></body></html>""")


@router.get("/healthz")
def healthz():
    return {"ok": True}
