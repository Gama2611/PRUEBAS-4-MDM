"""Consola web de administración (Jinja2, sesión firmada + CSRF)."""
import json
import re
import secrets
import time
import typing
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Admin, App, AuditLog, Command, Device, EnrollmentToken, Policy
from .schemas import COMMANDS, POLICY_MODELS, AndroidPolicy
from .security import hash_password, new_token, verify_password
from .services import (AGENT_PACKAGE, ApkError, apk_path, audit, latest_agent_apk, provisioning_payload, qr_png,
                       setup_checks, store_apk, validate_policy_settings)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ============================================================ textos de los campos de política
FIELD_LABELS = {
    "kiosk_enabled": "Modo kiosco activo", "kiosk_package": "Aplicación del kiosco",
    "kiosk_extra_packages": "Aplicaciones adicionales permitidas",
    "kiosk_exit_pin": "PIN de salida del kiosco",
    "kiosk_show_home_button": "Mostrar botón Inicio", "kiosk_show_notifications": "Permitir notificaciones",
    "kiosk_show_system_info": "Mostrar hora y batería", "kiosk_allow_power_menu": "Permitir menú de apagado",
    "disable_camera": "Deshabilitar cámara", "disable_screenshots": "Bloquear capturas de pantalla",
    "disable_status_bar": "Bloquear barra de estado", "disable_keyguard": "Sin pantalla de bloqueo",
    "disable_factory_reset": "Bloquear restablecimiento de fábrica", "disable_safe_boot": "Bloquear modo seguro",
    "disable_usb_file_transfer": "Bloquear transferencia de archivos por USB",
    "disable_debugging": "Bloquear depuración USB (ADB)",
    "disable_install_unknown_sources": "Bloquear apps de orígenes desconocidos", "disable_add_user": "Bloquear nuevos usuarios",
    "disable_config_wifi": "Impedir que cambien el Wi-Fi", "disable_bluetooth": "Deshabilitar Bluetooth",
    "stay_on_while_plugged": "Pantalla siempre encendida mientras carga",
    "auto_grant_permissions": "Conceder permisos a las apps automáticamente",
    "system_updates": "Actualizaciones del sistema", "update_window_start_min": "Ventana de actualización: desde",
    "update_window_end_min": "Ventana de actualización: hasta",
    "wifi_ssid": "Nombre de la red (SSID)", "wifi_password": "Clave del Wi-Fi", "wifi_security": "Tipo de seguridad",
    "required_app_ids": "Aplicaciones obligatorias",
    "app_config": "Configuración gestionada (JSON)",
    "sync_interval_sec": "Intervalo de sincronización (segundos)",
    "kiosk_url": "Dirección web del kiosco", "kiosk_browser": "Navegador",
    "allow_scripts": "Permitir el comando «Ejecutar script»", "inventory_interval_min": "Inventario de software cada (minutos)",
}
FIELD_HELP = {
    "kiosk_enabled": "La tablet sólo mostrará la aplicación elegida; no hay escritorio ni barra de notificaciones.",
    "kiosk_package": "Elige una de las apps subidas o escribe su paquete (por ejemplo com.adipack.cmmsconnect).",
    "kiosk_extra_packages": "Paquetes que también pueden abrirse, separados por coma. Ej.: com.google.android.documentsui, com.android.camera",
    "kiosk_exit_pin": "De 4 a 12 dígitos. Tocando 7 veces el logo ADIPACK en la tablet se pide este PIN. Vacío = conservar el actual.",
    "kiosk_show_home_button": "Normalmente desactivado en planta.",
    "kiosk_allow_power_menu": "Permite apagar o reiniciar con el botón de encendido.",
    "disable_camera": "Déjalo desactivado si la app usa la cámara (Adipack CMMS la usa).",
    "disable_debugging": "Actívalo en producción. Mientras estés probando, déjalo desactivado (el plan B por USB lo necesita).",
    "disable_keyguard": "Sin PIN ni patrón al encender la pantalla: el operario entra directo a la app.",
    "system_updates": "Recomendado: sólo en la ventana horaria, para no interrumpir la producción.",
    "wifi_ssid": "Va dentro del código QR: la tablet se conecta sola al activarla.",
    "wifi_password": "Vacío = conservar la clave actual.",
    "required_app_ids": "Se instalan y actualizan solas en todas las tablets con esta política.",
    "app_config": "Sólo si tu app lee configuración gestionada (RestrictionsManager). Adipack CMMS 1.0 no la usa: deja {}.",
    "sync_interval_sec": "Cada cuánto la tablet consulta al servidor (15 a 3600). 60 es un buen valor.",
    "kiosk_url": "Ej.: http://192.168.1.50:8501 (Streamlit del CMMS o del MES).",
    "allow_scripts": "Permite ejecutar scripts PowerShell/Bash desde la consola. Úsalo con cuidado.",
}
FIELD_HELP_PC = {
    "kiosk_enabled": "La PC abre el navegador a pantalla completa con la dirección indicada y lo reabre si lo cierran.",
    "sync_interval_sec": "Cada cuánto la PC consulta al servidor (15 a 3600). 60 es un buen valor.",
}
SECTIONS = {
    "android": [
        ("kiosk", "Modo kiosco", "Qué aplicación se muestra y cómo se sale del kiosco.",
         ["kiosk_enabled", "kiosk_package", "kiosk_exit_pin", "kiosk_extra_packages", "kiosk_show_system_info",
          "kiosk_allow_power_menu", "kiosk_show_home_button", "kiosk_show_notifications"]),
        ("apps", "Aplicaciones", "APKs que se instalan y actualizan solos.", ["required_app_ids", "app_config"]),
        ("wifi", "Red Wi-Fi de planta", "Se configura en la tablet y viaja dentro del código QR.",
         ["wifi_ssid", "wifi_password", "wifi_security"]),
        ("security", "Seguridad y restricciones", "Lo que el operario NO podrá hacer en la tablet.",
         ["disable_factory_reset", "disable_safe_boot", "disable_status_bar", "disable_screenshots",
          "disable_usb_file_transfer", "disable_install_unknown_sources", "disable_add_user", "disable_debugging",
          "disable_config_wifi", "disable_bluetooth", "disable_camera", "disable_keyguard"]),
        ("system", "Pantalla y sistema", "Comportamiento general de la tablet.",
         ["stay_on_while_plugged", "auto_grant_permissions", "system_updates", "update_window_start_min",
          "update_window_end_min"]),
        ("advanced", "Avanzado", "Normalmente no hace falta cambiarlo.", ["sync_interval_sec"]),
    ],
    "pc": [
        ("kiosk", "Modo kiosco", "Navegador a pantalla completa con el CMMS o el MES.",
         ["kiosk_enabled", "kiosk_url", "kiosk_browser"]),
        ("inventory", "Inventario y scripts", "", ["inventory_interval_min", "allow_scripts"]),
        ("advanced", "Avanzado", "Normalmente no hace falta cambiarlo.", ["sync_interval_sec"]),
    ],
}
OPTION_LABELS = {
    "windowed": "Sólo en la ventana horaria indicada (recomendado)", "postpone": "Posponer 30 días",
    "automatic": "Instalar en cuanto estén disponibles",
    "WPA": "WPA / WPA2 (con clave)", "WEP": "WEP (antigua)", "NONE": "Abierta (sin clave)",
    "edge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "Chromium", "firefox": "Mozilla Firefox",
}
TIME_FIELDS = {"update_window_start_min", "update_window_end_min"}
PLATFORM_NAMES = {"android": "Tablet Android", "windows": "PC Windows", "linux": "PC Linux"}

_login_failures: dict[str, list[float]] = defaultdict(list)


# ============================================================ utilidades
def _csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.update(request=request, user=request.session.get("user"), csrf=_csrf(request),
               flash=request.session.pop("flash", None), now=datetime.now(timezone.utc),
               public_url=get_settings().public_url, path=request.url.path)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def render_error(request: Request, code: int, title: str, detail: str) -> HTMLResponse:
    return render(request, "error.html", status_code=code, code=code, title=title, detail=detail)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str, default: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else default


def require_admin(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def check_csrf(request: Request) -> None:
    if request.method == "POST":
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
            raise HTTPException(400, "La página caducó por seguridad. Vuelve atrás, recarga la página e inténtalo de nuevo.")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def online(dev: Device) -> bool:
    ls = _aware(dev.last_seen)
    return bool(ls) and datetime.now(timezone.utc) - ls < timedelta(seconds=get_settings().offline_after_seconds)


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(get_settings().time_zone)
    except Exception:  # noqa: BLE001 - sin base de zonas horarias: UTC
        return timezone.utc


def fecha(dt: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Fecha en hora local de la planta (MDM_TIME_ZONE)."""
    if not dt:
        return "—"
    return _aware(dt).astimezone(_tz()).strftime(fmt)


def hace(dt: datetime | None) -> str:
    if not dt:
        return "nunca"
    s = int((datetime.now(timezone.utc) - _aware(dt)).total_seconds())
    if s < 60:
        return "hace segundos"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return f"hace {s // 86400} d"


def mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


templates.env.globals.update(online=online, COMMANDS=COMMANDS, json=json, PLATFORM_NAMES=PLATFORM_NAMES,
                             FIELD_LABELS=FIELD_LABELS, OPTION_LABELS=OPTION_LABELS)
templates.env.filters.update(fecha=fecha, hace=hace, mb=mb)


def setup_progress(db: Session) -> list[dict]:
    """Pasos de la configuración inicial, en lenguaje simple."""
    agent = latest_agent_apk(db) is not None
    has_app = db.scalar(select(func.count(App.id)).where(App.is_agent.is_(False))) > 0
    has_pol = db.scalar(select(func.count(Policy.id)).where(Policy.platform == "android")) > 0
    has_tab = db.scalar(select(func.count(Device.id)).where(Device.platform == "android", Device.retired.is_(False))) > 0
    return [
        {"done": agent, "title": "Subir el agente MDM", "text": "El APK del agente (com.adipack.mdm.agent) compilado.",
         "url": "/apps", "btn": "Ir a Aplicaciones"},
        {"done": has_app, "title": "Subir la app del kiosco", "text": "Tu APK Adipack CMMS o del MES.",
         "url": "/apps", "btn": "Ir a Aplicaciones"},
        {"done": has_pol, "title": "Crear la configuración de las tablets", "text": "Kiosco, PIN de salida y Wi-Fi.",
         "url": "/devices/add/android", "btn": "Configurar"},
        {"done": has_tab, "title": "Activar la primera tablet", "text": "Escanea el código QR con la tablet restablecida.",
         "url": "/devices/add/android", "btn": "Agregar tablet"},
    ]


# ============================================================ sesión
@router.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return back("/")
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    window = [t for t in _login_failures[ip] if time.time() - t < 900]
    _login_failures[ip] = window
    if len(window) >= 8:
        flash(request, "Demasiados intentos fallidos. Espera 15 minutos.", "err")
        return back("/login")
    admin = db.scalar(select(Admin).where(Admin.username == username.strip(), Admin.is_active.is_(True)))
    if admin is None or not verify_password(password, admin.password_hash):
        _login_failures[ip].append(time.time())
        flash(request, "Usuario o contraseña incorrectos.", "err")
        return back("/login")
    request.session.clear()
    request.session["user"] = admin.username
    audit(db, admin.username, "login", ip)
    db.commit()
    return back("/")


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request):
    request.session.clear()
    return back("/login")


@router.get("/account")
def account_page(request: Request, user: str = Depends(require_admin)):
    return render(request, "account.html")


@router.post("/account", dependencies=[Depends(check_csrf)])
def account(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...),
            user: str = Depends(require_admin), db: Session = Depends(get_db)):
    admin = db.scalar(select(Admin).where(Admin.username == user))
    if not admin or not verify_password(current, admin.password_hash):
        flash(request, "La contraseña actual no es correcta.", "err")
    elif new != confirm:
        flash(request, "La nueva contraseña y su confirmación no coinciden.", "err")
    elif len(new) < 10:
        flash(request, "La nueva contraseña debe tener al menos 10 caracteres.", "err")
    else:
        admin.password_hash = hash_password(new)
        audit(db, user, "change_password")
        db.commit()
        flash(request, "Contraseña actualizada.")
    return back("/account")


# ============================================================ inicio / equipos
@router.get("/")
def dashboard(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db),
              q: str = "", platform: str = "", estado: str = ""):
    all_devices = list(db.scalars(select(Device).where(Device.retired.is_(False)).order_by(Device.name)))
    devices = all_devices
    if platform == "android":
        devices = [d for d in devices if d.platform == "android"]
    elif platform == "pc":
        devices = [d for d in devices if d.platform in ("windows", "linux")]
    if estado == "online":
        devices = [d for d in devices if online(d)]
    elif estado == "offline":
        devices = [d for d in devices if not online(d)]
    if q:
        ql = q.lower()
        devices = [d for d in devices if ql in f"{d.name} {d.location} {d.model} {d.serial}".lower()]
    stats = {
        "total": len(all_devices),
        "online": sum(online(d) for d in all_devices),
        "kiosk": sum(bool((d.status or {}).get("kiosk_active")) for d in all_devices),
        "outdated": sum(bool(d.policy and d.applied_policy_version != d.policy.version) for d in all_devices),
    }
    steps = setup_progress(db)
    return render(request, "dashboard.html", devices=devices, stats=stats, q=q, platform=platform, estado=estado,
                  steps=steps, setup_done=all(s["done"] for s in steps))


# ------------------------------------------------------------ asistente "Agregar dispositivo"
@router.get("/devices/add")
def add_device(request: Request, user: str = Depends(require_admin)):
    return render(request, "add_device.html")


@router.get("/devices/add/android")
def add_android(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    policies = db.scalars(select(Policy).where(Policy.platform == "android").order_by(Policy.name)).all()
    apps = db.scalars(select(App).where(App.is_agent.is_(False)).order_by(App.label, App.version_code.desc())).all()
    agent = latest_agent_apk(db)
    return render(request, "add_android.html", policies=policies, apps=apps, agent=agent,
                  checks=setup_checks(db, policies[0] if policies else None))


@router.post("/devices/add/android/policy", dependencies=[Depends(check_csrf)])
def add_android_policy(request: Request, name: str = Form("Tablets CMMS planta"), app_id: int = Form(0),
                       pin: str = Form(""), wifi_ssid: str = Form(""), wifi_password: str = Form(""),
                       user: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Crea la política de tablets con valores recomendados a partir de 4 datos."""
    app = db.get(App, app_id) if app_id else None
    if app is None or app.is_agent:
        flash(request, "Elige la aplicación que se mostrará en el kiosco.", "err")
        return back("/devices/add/android")
    if not re.fullmatch(r"\d{4,12}", pin.strip()):
        flash(request, "El PIN de salida debe tener entre 4 y 12 dígitos (sólo números).", "err")
        return back("/devices/add/android")
    name = name.strip()[:100] or "Tablets CMMS planta"
    if db.scalar(select(Policy).where(Policy.name == name)):
        flash(request, f"Ya existe una política llamada «{name}». Usa otro nombre.", "err")
        return back("/devices/add/android")
    data = AndroidPolicy(kiosk_enabled=True, kiosk_package=app.package_name, required_app_ids=[app.id],
                         kiosk_exit_pin="0000").model_dump()  # valores recomendados por defecto
    data.update(kiosk_exit_pin=pin.strip(), wifi_ssid=wifi_ssid.strip(), wifi_password=wifi_password)
    try:
        settings_ = validate_policy_settings("android", data)
    except ValidationError as exc:
        flash(request, "No se creó: " + _errors_text(exc), "err")
        return back("/devices/add/android")
    p = Policy(name=name, platform="android", settings=settings_, version=1,
               description=f"Kiosco con {app.label}")
    db.add(p)
    db.flush()
    audit(db, user, "policy_save", f"{p.name} v1 (asistente)")
    db.commit()
    flash(request, f"Configuración «{p.name}» creada. Ahora genera el código QR.")
    return back("/devices/add/android")


@router.get("/devices/add/pc")
def add_pc(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    policies = db.scalars(select(Policy).where(Policy.platform.in_(["windows", "linux"])).order_by(Policy.name)).all()
    return render(request, "add_pc.html", policies=policies)


# ------------------------------------------------------------ ficha de equipo
@router.get("/devices/{device_id}")
def device_page(device_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "Ese equipo no existe.")
    pc = dev.platform in ("windows", "linux")
    policies = db.scalars(select(Policy).where(
        Policy.platform.in_(["windows", "linux"]) if pc else Policy.platform == "android").order_by(Policy.name)).all()
    apps = db.scalars(select(App).where(App.is_agent.is_(False)).order_by(App.package_name)).all()
    return render(request, "device.html", d=dev, policies=policies, apps=apps, commands=list(dev.commands[:50]))


@router.post("/devices/{device_id}/update", dependencies=[Depends(check_csrf)])
def device_update(device_id: int, request: Request, name: str = Form(...), location: str = Form(""),
                  policy_id: str = Form(""), user: str = Depends(require_admin), db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "Ese equipo no existe.")
    pol = db.get(Policy, int(policy_id)) if policy_id else None
    if pol and not _platform_ok(pol.platform, dev.platform):
        flash(request, "Esa política es de otra plataforma.", "err")
        return back(f"/devices/{device_id}")
    dev.name, dev.location = name.strip()[:100] or dev.name, location.strip()[:100]
    new_pid = pol.id if pol else None
    if new_pid != dev.policy_id:
        dev.policy_id = new_pid
        dev.applied_policy_version = 0  # cambió la política: se reenvía completa
    audit(db, user, "device_update", f"{dev.name} política={pol.name if pol else '-'}")
    db.commit()
    flash(request, "Cambios guardados.")
    return back(f"/devices/{device_id}")


def _platform_ok(policy_platform: str, device_platform: str) -> bool:
    return policy_platform == device_platform or {policy_platform, device_platform} <= {"windows", "linux"}


def _build_command(dev: Device, ctype: str, form: dict, db: Session) -> tuple[dict | None, str]:
    spec = COMMANDS.get(ctype)
    if not spec or dev.platform not in spec["platforms"]:
        return None, f"La acción «{ctype}» no está disponible para {PLATFORM_NAMES.get(dev.platform, dev.platform)}."
    params: dict = {}
    if ctype == "message":
        text = str(form.get("message", "")).strip()
        if not text:
            return None, "Escribe el mensaje que verá el operario."
        params["text"] = text[:500]
    elif ctype == "exit_kiosk":
        try:
            minutes = int(form.get("minutes") or 10)
        except ValueError:
            minutes = 10
        params["minutes"] = max(1, min(minutes, 240))
    elif ctype == "install_app":
        try:
            app = db.get(App, int(form.get("app_id") or 0))
        except ValueError:
            app = None
        if not app:
            return None, "Elige la aplicación a instalar."
        base = get_settings().public_url.rstrip("/")
        params = {"app_id": app.id, "package": app.package_name, "version_code": app.version_code,
                  "sha256": app.sha256, "url": f"{base}/api/v1/apps/{app.id}/download"}
    elif ctype in {"uninstall_app", "clear_app_data"}:
        pkg = str(form.get("package", "")).strip()
        if not pkg:
            return None, "Indica el paquete de la aplicación."
        if pkg == AGENT_PACKAGE:
            return None, "No se puede desinstalar ni borrar los datos del agente MDM."
        params["package"] = pkg
    elif ctype == "run_script":
        script = str(form.get("script", "")).strip()
        if not script:
            return None, "Escribe el script."
        if not (dev.policy and (dev.policy.settings or {}).get("allow_scripts")):
            return None, "La política del equipo no permite scripts (actívalo en la política)."
        params["script"] = script[:20000]
    if spec["danger"] and str(form.get("confirm", "")).strip() != dev.name:
        return None, f"Acción peligrosa: para confirmar escribe exactamente el nombre del equipo ({dev.name})."
    return params, ""


@router.post("/devices/{device_id}/command", dependencies=[Depends(check_csrf)])
async def device_command(device_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = dict(await request.form())
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "Ese equipo no existe.")
    ctype = str(form.get("type", ""))
    params, err = _build_command(dev, ctype, form, db)
    if err:
        flash(request, err, "err")
    else:
        db.add(Command(device_id=dev.id, type=ctype, params=params, created_by=user))
        audit(db, user, f"cmd:{ctype}", f"{dev.name} {json.dumps(params, ensure_ascii=False)[:300]}")
        db.commit()
        flash(request, f"«{COMMANDS[ctype]['label']}» enviado a {dev.name}. Se ejecutará en su próxima "
                       f"sincronización (normalmente menos de 1 minuto).")
    return back(f"/devices/{device_id}")


@router.post("/devices/bulk", dependencies=[Depends(check_csrf)])
async def bulk_command(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("device_ids") if str(x).isdigit()]
    ctype = str(form.get("type", ""))
    if not ids:
        flash(request, "Marca al menos un equipo en la lista.", "err")
        return back("/")
    if ctype not in {"sync", "lock", "reboot", "message"}:
        flash(request, "Acción masiva no permitida.", "err")
        return back("/")
    ok, errors = 0, []
    for dev in db.scalars(select(Device).where(Device.id.in_(ids))):
        params, err = _build_command(dev, ctype, dict(form), db)
        if err:
            errors.append(f"{dev.name}: {err}")
            continue
        db.add(Command(device_id=dev.id, type=ctype, params=params, created_by=user))
        ok += 1
    audit(db, user, f"bulk:{ctype}", f"{ok} equipos")
    db.commit()
    flash(request, f"«{COMMANDS[ctype]['label']}» enviado a {ok} equipo(s)." + (f" Errores: {'; '.join(errors)}" if errors else ""),
          "err" if errors and not ok else "ok")
    return back("/")


@router.post("/commands/{command_id}/cancel", dependencies=[Depends(check_csrf)])
def cancel_command(command_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    cmd = db.get(Command, command_id)
    if cmd and cmd.state == "pending":
        cmd.state = "cancelled"
        audit(db, user, "cmd_cancel", str(cmd.id))
        db.commit()
        flash(request, "Comando cancelado.")
    return back(f"/devices/{cmd.device_id}" if cmd else "/")


@router.post("/devices/{device_id}/retire", dependencies=[Depends(check_csrf)])
def retire_device(device_id: int, request: Request, confirm: str = Form(""), user: str = Depends(require_admin),
                  db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "Ese equipo no existe.")
    if confirm.strip() != dev.name:
        flash(request, f"Para retirar el equipo escribe exactamente su nombre: {dev.name}", "err")
        return back(f"/devices/{device_id}")
    dev.retired = True
    dev.token_hash = new_token()  # invalida el token anterior
    audit(db, user, "device_retire", dev.name)
    db.commit()
    flash(request, f"{dev.name} fue retirado del MDM. Su acceso quedó anulado.")
    return back("/")


# ============================================================ políticas
def _form_fields(platform: str, values: dict, db: Session) -> list[dict]:
    """Secciones del formulario con sus campos, generadas desde el modelo validado."""
    model = POLICY_MODELS[platform]
    apps = db.scalars(select(App).where(App.is_agent.is_(False)).order_by(App.label, App.version_code.desc())).all()
    packages = sorted({a.package_name for a in apps})
    out = []
    for key, title, desc, names in SECTIONS["android" if platform == "android" else "pc"]:
        fields = []
        for name in names:
            f = model.model_fields[name]
            ann = f.annotation
            origin = typing.get_origin(ann)
            val = values.get(name, f.get_default(call_default_factory=True))
            kind, options = "text", None
            if ann is bool:
                kind = "bool"
            elif name in TIME_FIELDS:
                kind, val = "time", f"{int(val) // 60:02d}:{int(val) % 60:02d}"
            elif ann is int:
                kind = "int"
            elif origin is typing.Literal:
                kind, options = "select", list(typing.get_args(ann))
            elif name == "required_app_ids":
                kind, options = "apps", apps
            elif name == "kiosk_package":
                kind, options = "package", packages
            elif origin is list:
                kind, val = "csv", ", ".join(val or [])
            elif origin is dict:
                kind, val = "json", json.dumps(val or {}, indent=2, ensure_ascii=False)
            elif "password" in name or name.endswith("_pin"):
                kind = "secret"
            help_ = FIELD_HELP_PC.get(name) if platform != "android" and name in FIELD_HELP_PC else FIELD_HELP.get(name, "")
            fields.append({"name": name, "label": FIELD_LABELS.get(name, name), "help": help_,
                           "kind": kind, "value": val, "options": options})
        out.append({"key": key, "title": title, "desc": desc, "fields": fields})
    return out


def _parse_policy_form(platform: str, form) -> dict:
    model = POLICY_MODELS[platform]
    data: dict = {}
    for name, f in model.model_fields.items():
        ann = f.annotation
        origin = typing.get_origin(ann)
        if ann is bool:
            data[name] = form.get(name) == "on"
        elif name in TIME_FIELDS:
            raw = str(form.get(name, "")).strip()
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError(f"{FIELD_LABELS[name]}: hora inválida ({raw or 'vacía'}). Usa HH:MM.")
            data[name] = int(m.group(1)) * 60 + int(m.group(2))
        elif name == "required_app_ids":
            data[name] = [int(x) for x in form.getlist(name) if str(x).isdigit()]
        elif origin is list:
            data[name] = [x.strip() for x in str(form.get(name, "")).split(",") if x.strip()]
        elif origin is dict:
            raw = str(form.get(name, "")).strip() or "{}"
            try:
                data[name] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{FIELD_LABELS.get(name, name)}: JSON inválido ({exc.msg}, línea {exc.lineno}).") from exc
            if not isinstance(data[name], dict):
                raise ValueError(f"{FIELD_LABELS.get(name, name)}: debe ser un objeto JSON entre llaves {{ }}.")
        else:
            data[name] = str(form.get(name, "")).strip()
    return data


def _errors_text(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors():
        label = FIELD_LABELS.get(str(e["loc"][0]), "") if e.get("loc") else ""
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        msg = {"Input should be a valid integer, unable to parse string as an integer": "debe ser un número entero"}.get(msg, msg)
        parts.append(f"{label}: {msg}" if label else msg)
    return "; ".join(parts)


@router.get("/policies")
def policies_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    pols = db.scalars(select(Policy).order_by(Policy.platform, Policy.name)).all()
    return render(request, "policies.html", policies=pols)


@router.get("/policies/new")
def policy_new(request: Request, platform: str = "android", user: str = Depends(require_admin), db: Session = Depends(get_db)):
    if platform not in POLICY_MODELS:
        raise HTTPException(400, "Plataforma desconocida.")
    return render(request, "policy_form.html", p=None, platform=platform, sections=_form_fields(platform, {}, db))


@router.get("/policies/{policy_id}")
def policy_edit(policy_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.get(Policy, policy_id)
    if not p:
        raise HTTPException(404, "Esa política no existe.")
    return render(request, "policy_form.html", p=p, platform=p.platform, sections=_form_fields(p.platform, p.settings or {}, db))


@router.post("/policies/save", dependencies=[Depends(check_csrf)])
async def policy_save(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    platform = str(form.get("platform"))
    pid = str(form.get("id", ""))
    name = str(form.get("name", "")).strip()
    retry = f"/policies/{pid}" if pid else f"/policies/new?platform={platform}"
    if platform not in POLICY_MODELS or not name:
        flash(request, "El nombre de la política es obligatorio.", "err")
        return back(retry if platform in POLICY_MODELS else "/policies")
    p = db.get(Policy, int(pid)) if pid.isdigit() else None
    try:
        raw = _parse_policy_form(platform, form)
        old = (p.settings or {}) if p else {}
        if p and platform == "android" and not raw.get("kiosk_exit_pin"):
            raw["kiosk_exit_pin"] = old.get("kiosk_exit_pin", "")  # vacío = conservar
        if p and "wifi_password" in raw and not raw.get("wifi_password"):
            raw["wifi_password"] = old.get("wifi_password", "")
        settings_ = validate_policy_settings(platform, raw)
    except ValidationError as exc:
        flash(request, "No se guardó. " + _errors_text(exc), "err")
        return back(retry)
    except ValueError as exc:
        flash(request, f"No se guardó. {exc}", "err")
        return back(retry)
    dup = db.scalar(select(Policy).where(Policy.name == name, Policy.id != (p.id if p else 0)))
    if dup:
        flash(request, "Ya existe otra política con ese nombre.", "err")
        return back(retry)
    if p is None:
        p = Policy(name=name, platform=platform, settings=settings_, version=1)
        db.add(p)
    else:
        p.name, p.settings, p.version = name, settings_, p.version + 1
    p.description = str(form.get("description", ""))[:255]
    db.flush()
    audit(db, user, "policy_save", f"{p.name} v{p.version}")
    db.commit()
    flash(request, f"Política «{p.name}» guardada (versión {p.version}). Los equipos la aplican en su próxima sincronización.")
    return back(f"/policies/{p.id}")


@router.post("/policies/{policy_id}/delete", dependencies=[Depends(check_csrf)])
def policy_delete(policy_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.get(Policy, policy_id)
    if p:
        if p.devices:
            flash(request, f"No se puede eliminar: {len(p.devices)} equipo(s) la usan. Cámbiales la política primero.", "err")
            return back(f"/policies/{policy_id}")
        audit(db, user, "policy_delete", p.name)
        db.delete(p)
        db.commit()
        flash(request, "Política eliminada.")
    return back("/policies")


# ============================================================ aplicaciones
@router.get("/apps")
def apps_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    apps = db.scalars(select(App).order_by(App.is_agent.desc(), App.package_name, App.version_code.desc())).all()
    used = {}
    for p in db.scalars(select(Policy)):
        for i in (p.settings or {}).get("required_app_ids", []):
            used.setdefault(i, []).append(p.name)
    return render(request, "apps.html", apps=apps, used=used, max_mb=get_settings().max_upload_mb)


@router.post("/apps/upload", dependencies=[Depends(check_csrf)])
async def apps_upload(request: Request, file: UploadFile, is_agent: str = Form(""), next: str = Form(""),
                      user: str = Depends(require_admin), db: Session = Depends(get_db)):
    dest = safe_next(next, "/apps")
    if not file.filename:
        flash(request, "Elige un archivo .apk.", "err")
        return back(dest)
    if not file.filename.lower().endswith(".apk"):
        flash(request, f"«{file.filename}» no es un archivo .apk.", "err")
        return back(dest)
    try:
        app = store_apk(db, file.file, file.filename, is_agent=is_agent == "on")
        audit(db, user, "app_upload", f"{app.package_name} v{app.version_name} ({app.version_code})")
        db.commit()
        from .services import apk_path, is_debug_signed

        kind = "Agente MDM cargado" if app.is_agent else "Aplicación cargada"
        msg = (f"{kind}: {app.label} — {app.package_name}, versión {app.version_name} "
               f"(código {app.version_code}).")
        if is_debug_signed(apk_path(app)):
            flash(request, msg + " ATENCIÓN: es un APK de PRUEBAS (debug). Su firma cambia en cada compilación, así "
                                 "que las tablets no podrán actualizarlo después. Para producción usa el APK del "
                                 "artefacto «APK-FIRMADO» de GitHub Actions (ver CREAR_LLAVE_FIRMA.bat).", "err")
        else:
            flash(request, msg)
    except ApkError as exc:
        db.rollback()
        flash(request, str(exc), "err")
    finally:
        await file.close()
    return back(dest)


@router.post("/apps/{app_id}/delete", dependencies=[Depends(check_csrf)])
def apps_delete(app_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    app = db.get(App, app_id)
    if app:
        in_use = [p.name for p in db.scalars(select(Policy)) if app_id in (p.settings or {}).get("required_app_ids", [])]
        if in_use:
            flash(request, f"No se puede eliminar: la usa la política {', '.join(in_use)}.", "err")
            return back("/apps")
        try:
            apk_path(app).unlink(missing_ok=True)
        except OSError:
            pass  # archivo en uso: se borra el registro igualmente
        audit(db, user, "app_delete", f"{app.package_name} {app.version_code}")
        db.delete(app)
        db.commit()
        flash(request, "Aplicación eliminada.")
    return back("/apps")


# ============================================================ códigos de activación
@router.get("/enroll")
def enroll_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    tokens = db.scalars(select(EnrollmentToken).order_by(EnrollmentToken.id.desc())).all()
    return render(request, "enroll.html", tokens=tokens)


@router.post("/enroll/new", dependencies=[Depends(check_csrf)])
def enroll_new(request: Request, policy_id: str = Form(""), name_prefix: str = Form("TAB"),
               max_uses: str = Form("0"), days: str = Form("7"), user: str = Depends(require_admin),
               db: Session = Depends(get_db)):
    pol = db.get(Policy, int(policy_id)) if policy_id.isdigit() else None
    if not pol:
        flash(request, "Elige la política.", "err")
        return back("/devices/add")
    try:
        max_uses_i, days_i = max(0, int(max_uses or 0)), max(0, int(days or 0))
    except ValueError:
        flash(request, "Usos y días deben ser números.", "err")
        return back("/devices/add")
    prefix = "".join(ch for ch in name_prefix.upper() if ch.isalnum() or ch in "-_")[:20] or "EQ"
    et = EnrollmentToken(token=new_token(18), policy_id=pol.id, name_prefix=prefix, max_uses=max_uses_i,
                         expires_at=datetime.now(timezone.utc) + timedelta(days=days_i) if days_i > 0 else None)
    db.add(et)
    db.flush()
    audit(db, user, "enroll_token_new", f"{pol.name} prefijo={prefix}")
    db.commit()
    return back(f"/enroll/{et.id}")


ACTIVATION_EVENTS = ("qr_descarga_inicio", "qr_descarga_ok", "qr_descarga_cortada", "qr_descarga_error",
                     "prueba_red", "enroll", "enroll_rechazado")
EVENT_INFO = {
    "qr_descarga_inicio": ("info", "Una tablet empezó a descargar el agente"),
    "qr_descarga_ok": ("ok", "Agente descargado completo: la red y el Firewall están bien"),
    "qr_descarga_cortada": ("err", "La descarga del agente se cortó"),
    "qr_descarga_error": ("err", "La tablet pidió el agente pero no está cargado"),
    "prueba_red": ("ok", "Prueba de conexión desde un celular: OK"),
    "enroll": ("ok", "Tablet activada y registrada en el MDM"),
    "enroll_rechazado": ("err", "El MDM rechazó la activación"),
}


def _activation_events(db: Session, et: EnrollmentToken) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=12)
    rows = db.scalars(select(AuditLog).where(AuditLog.action.in_(ACTIVATION_EVENTS), AuditLog.at >= since)
                      .order_by(AuditLog.id.desc()).limit(25)).all()
    out = []
    for r in rows:
        kind, title = EVENT_INFO.get(r.action, ("info", r.action))
        out.append({"at": r.at, "kind": kind, "title": title, "detail": r.detail})
    return out


def _wifi_on(request: Request) -> bool:
    return request.query_params.get("wifi", "1") != "0"


@router.get("/enroll/{token_id}")
def enroll_detail(token_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    et = db.get(EnrollmentToken, token_id)
    if not et:
        raise HTTPException(404, "Ese código de activación no existe.")
    payload, err = None, ""
    wifi = _wifi_on(request)
    if et.policy.platform == "android":
        try:
            payload = json.dumps(provisioning_payload(db, et.token, et.policy, include_wifi=wifi),
                                 indent=2, ensure_ascii=False)
        except ApkError as exc:
            err = str(exc)
    expired = bool(et.expires_at and _aware(et.expires_at) < datetime.now(timezone.utc))
    exhausted = bool(et.max_uses and et.uses >= et.max_uses)
    enrolled = db.scalars(select(Device).where(Device.policy_id == et.policy_id, Device.retired.is_(False),
                                               Device.name.like(f"{et.name_prefix}-%"))
                          .order_by(Device.enrolled_at.desc()).limit(20)).all()
    from .netdiag import local_ipv4s

    return render(request, "enroll_detail.html", et=et, payload=payload, err=err, expired=expired,
                  exhausted=exhausted, checks=setup_checks(db, et.policy), enrolled=enrolled,
                  wifi=wifi, has_wifi=bool((et.policy.settings or {}).get("wifi_ssid")),
                  ips=local_ipv4s(), events=_activation_events(db, et),
                  test_url=f"{get_settings().public_url}/prueba-conexion")


@router.get("/enroll/{token_id}/actividad")
def enroll_activity(token_id: int, request: Request, user: str = Depends(require_admin),
                    db: Session = Depends(get_db)):
    et = db.get(EnrollmentToken, token_id)
    if not et:
        raise HTTPException(404, "Ese código de activación no existe.")
    # Sin render(): no debe consumir los mensajes «flash» de la sesión en cada consulta automática.
    return templates.TemplateResponse(request, "_activation_events.html", {"events": _activation_events(db, et)},
                                      headers={"Cache-Control": "no-store"})


@router.get("/enroll/{token_id}/qr.png")
def enroll_qr(token_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    et = db.get(EnrollmentToken, token_id)
    if not et or et.policy.platform != "android":
        raise HTTPException(404, "Código no encontrado.")
    try:
        payload = provisioning_payload(db, et.token, et.policy, include_wifi=_wifi_on(request))
    except ApkError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(qr_png(json.dumps(payload, separators=(",", ":"))), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/diagnostico/prueba.png")
def network_test_qr(request: Request, user: str = Depends(require_admin)):
    return Response(qr_png(f"{get_settings().public_url}/prueba-conexion"), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/config/public-url", dependencies=[Depends(check_csrf)])
def config_public_url(request: Request, url: str = Form(""), next_url: str = Form("/devices/add/android"),
                      user: str = Depends(require_admin), db: Session = Depends(get_db)):
    from .netdiag import set_public_url

    next_url = safe_next(next_url, "/devices/add/android")
    try:
        new = set_public_url(url)
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(next_url)
    audit(db, user, "config_public_url", new)
    db.commit()
    flash(request, f"Listo: el QR ahora apunta a {new}. Vuelve a escanear el código nuevo (el impreso antes ya no sirve).")
    return back(next_url)


@router.post("/enroll/{token_id}/revoke", dependencies=[Depends(check_csrf)])
def enroll_revoke(token_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    et = db.get(EnrollmentToken, token_id)
    if et:
        et.revoked = True
        audit(db, user, "enroll_token_revoke", str(et.id))
        db.commit()
        flash(request, "Código revocado: ya no sirve para activar equipos.")
    return back("/enroll")


# ============================================================ auditoría
@router.get("/audit")
def audit_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)).all()
    return render(request, "audit.html", rows=rows)
