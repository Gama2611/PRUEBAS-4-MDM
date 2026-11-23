"""Lógica de negocio: APKs, QR de aprovisionamiento, render de políticas, auditoría."""
import base64
import hashlib
import io
import json
import os
import struct
import uuid
from pathlib import Path

import qrcode
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import App, AuditLog, Policy
from .schemas import POLICY_MODELS
from .security import pin_hash

AGENT_PACKAGE = "com.adipack.mdm.agent"
AGENT_ADMIN_COMPONENT = f"{AGENT_PACKAGE}/{AGENT_PACKAGE}.AdminReceiver"


def audit(db: Session, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail[:4000]))


# ---------------------------------------------------------------- APKs
class ApkError(ValueError):
    pass


def read_apk_info(path: Path) -> dict:
    """Lee paquete, versión y nombre del APK y CIERRA el archivo.

    Importante en Windows: pyaxmlparser deja el ZIP abierto; si no se cierra, Windows impide
    mover el archivo ("está siendo utilizado por otro proceso") y la subida fallaba.
    """
    from pyaxmlparser import APK  # import diferido: es pesado

    apk = None
    try:
        apk = APK(str(path))
        package = apk.package or ""
        version_code = int(apk.version_code or 0)
        version_name = apk.version_name or ""
        try:
            label = apk.application or package
        except Exception:  # noqa: BLE001 - recursos ofuscados: usamos el paquete
            label = package
        return {"package": package, "version_code": version_code, "version_name": str(version_name),
                "label": str(label or package)}
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de lectura = APK inválido
        raise ApkError("El archivo no es un APK válido de Android (¿está dañado o es otro tipo de archivo?).") from exc
    finally:
        zf = getattr(apk, "zip", None)
        if zf is not None:
            try:
                zf.close()
            except Exception:  # noqa: BLE001
                pass
        del apk


def store_apk(db: Session, upload_stream, original_name: str, is_agent: bool = False) -> App:
    """Guarda el APK, calcula SHA-256 y extrae paquete/versión del manifiesto."""
    settings = get_settings()
    settings.apk_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.apk_dir / f".upload-{uuid.uuid4().hex}.apk"
    h = hashlib.sha256()
    size = 0
    limit = settings.max_upload_mb * 1024 * 1024
    try:
        with tmp.open("wb") as out:
            while chunk := upload_stream.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ApkError(f"El archivo supera el máximo de {settings.max_upload_mb} MB.")
                h.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ApkError("El archivo está vacío.")
        info = read_apk_info(tmp)
        package = info["package"]
        if not package:
            raise ApkError("El APK no declara nombre de paquete.")
        # El agente se reconoce solo por su paquete: no depende de marcar la casilla.
        if package == AGENT_PACKAGE:
            is_agent = True
        elif is_agent:
            raise ApkError(f"Marcaste «Es el agente MDM», pero este APK es {package}. "
                           f"El agente es {AGENT_PACKAGE}. Desmarca la casilla para subirlo como aplicación.")

        digest = h.hexdigest()
        existing = db.scalar(select(App).where(App.sha256 == digest))
        if existing:
            raise ApkError(f"Este APK ya está cargado: {existing.label} versión {existing.version_name} "
                           f"(código {existing.version_code}).")
        same_version = db.scalar(select(App).where(App.package_name == package,
                                                   App.version_code == info["version_code"]))
        if same_version:
            raise ApkError(f"Ya existe {package} con el mismo código de versión {info['version_code']} pero con "
                           "contenido distinto. Sube la nueva versión con un versionCode mayor.")
        final_name = f"{package}-{info['version_code']}-{digest[:8]}.apk"
        os.replace(tmp, settings.apk_dir / final_name)
        app = App(
            package_name=package[:255], label=info["label"][:255], version_name=info["version_name"][:64],
            version_code=info["version_code"], filename=final_name, sha256=digest, size=size, is_agent=is_agent,
        )
        db.add(app)
        db.flush()
        return app
    finally:
        tmp.unlink(missing_ok=True)


def apk_path(app: App) -> Path:
    return get_settings().apk_dir / app.filename


def latest_agent_apk(db: Session) -> App | None:
    return db.scalar(select(App).where(App.is_agent.is_(True)).order_by(App.version_code.desc(), App.id.desc()))


# ---------------------------------------------------------------- firma del APK (esquema v2/v3)
_APK_SIG_MAGIC = b"APK Sig Block 42"
_SIG_BLOCK_IDS = (0xF05368C0, 0x7109871A)  # v3, v2 (sin rotación de llave el certificado es el mismo)


def _lp(buf: bytes, pos: int) -> tuple[bytes, int]:
    """Lee un bloque con prefijo de longitud uint32 (little-endian)."""
    if pos + 4 > len(buf):
        raise ValueError("bloque truncado")
    (n,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    if pos + n > len(buf):
        raise ValueError("bloque truncado")
    return buf[pos:pos + n], pos + n


def apk_signing_cert_der(path: Path) -> bytes | None:
    """Certificado (DER) con el que está firmado el APK, leído del 'APK Signing Block' v2/v3.

    Devuelve None si el APK no tiene firma v2/v3 (sólo v1 o sin firmar).
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        tail_len = min(size, 65535 + 22)
        f.seek(size - tail_len)
        tail = f.read(tail_len)
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0 or eocd + 22 > len(tail):
            return None
        (cd_offset,) = struct.unpack_from("<I", tail, eocd + 16)
        if cd_offset < 32:
            return None
        f.seek(cd_offset - 24)
        footer = f.read(24)
        if footer[8:] != _APK_SIG_MAGIC:
            return None
        (block_size,) = struct.unpack_from("<Q", footer, 0)
        start = cd_offset - (block_size + 8)
        if start < 0 or block_size > 64 * 1024 * 1024:
            return None
        f.seek(start)
        block = f.read(block_size + 8)
    pairs = block[8:-24]
    found: dict[int, bytes] = {}
    pos = 0
    while pos + 12 <= len(pairs):
        (length,) = struct.unpack_from("<Q", pairs, pos)
        (pid,) = struct.unpack_from("<I", pairs, pos + 8)
        if length < 4 or pos + 8 + length > len(pairs):
            break
        found[pid] = pairs[pos + 12:pos + 8 + length]
        pos += 8 + length
    for pid in _SIG_BLOCK_IDS:
        value = found.get(pid)
        if not value:
            continue
        try:
            signers, _ = _lp(value, 0)
            signer, _ = _lp(signers, 0)
            signed_data, _ = _lp(signer, 0)
            _digests, p2 = _lp(signed_data, 0)
            certs, _ = _lp(signed_data, p2)
            cert, _ = _lp(certs, 0)
            if cert:
                return cert
        except ValueError:
            continue
    return None


def is_debug_signed(path: Path) -> bool:
    """True si el APK está firmado con la llave de pruebas de Android («CN=Android Debug»).

    Esa llave cambia en cada compilación de GitHub Actions: el APK no se puede actualizar después.
    """
    try:
        cert = apk_signing_cert_der(path)
    except OSError:
        return False
    return bool(cert) and b"Android Debug" in cert


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_agent_cache: dict[tuple, dict] = {}


def agent_apk_check(app: App) -> dict:
    """Verifica el APK del agente en disco y calcula los checksums del QR (con caché por archivo)."""
    path = apk_path(app)
    if not path.exists():
        raise ApkError("El archivo del APK del agente no está en la carpeta data/apks. Vuelve a subirlo en Apps.")
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns, app.sha256)
    if key not in _agent_cache:
        real = file_sha256(path)
        if real != app.sha256:
            raise ApkError("El APK del agente en disco no coincide con el que se subió (¿se reemplazó a mano?). "
                           "Vuelve a subirlo en Apps.")
        cert = apk_signing_cert_der(path)
        _agent_cache.clear()
        _agent_cache[key] = {
            "debug": bool(cert) and b"Android Debug" in cert,
            "package_checksum": b64url(bytes.fromhex(real)),
            "signature_checksum": b64url(hashlib.sha256(cert).digest()) if cert else "",
            "cert_sha256": hashlib.sha256(cert).hexdigest().upper() if cert else "",
        }
    return _agent_cache[key]


# ---------------------------------------------------------------- QR Android Enterprise
def provisioning_payload(db: Session, enrollment_token: str, policy: Policy, include_wifi: bool = True) -> dict:
    """QR estándar de Android (7+): restablecer de fábrica, tocar 6 veces la pantalla de bienvenida."""
    settings = get_settings()
    agent = latest_agent_apk(db)
    if agent is None:
        raise ApkError("Primero sube el APK del agente (pestaña Apps > 'Es el agente MDM')")
    chk = agent_apk_check(agent)
    base = settings.public_url.rstrip("/")
    payload = {
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME": AGENT_ADMIN_COMPONENT,
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION": f"{base}/agent/android.apk",
        "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED": True,
        "android.app.extra.PROVISIONING_SKIP_EDUCATION_SCREENS": True,
        "android.app.extra.PROVISIONING_TIME_ZONE": settings.time_zone,
        "android.app.extra.PROVISIONING_LOCALE": "es_VE",
        "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE": {
            "server_url": base,
            "enrollment_token": enrollment_token,
        },
    }
    # Android verifica el agente descargado con UNO de estos dos valores. Preferimos el de la FIRMA:
    # es el que recomienda Google, lo aceptan bien Android 14/15 (HyperOS) y no cambia al recompilar
    # el agente con la misma llave. El del archivo queda sólo para APKs sin firma v2/v3.
    if chk["signature_checksum"]:
        payload["android.app.extra.PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM"] = chk["signature_checksum"]
    else:
        payload["android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM"] = chk["package_checksum"]
    s = policy.settings or {}
    if include_wifi and s.get("wifi_ssid"):
        payload["android.app.extra.PROVISIONING_WIFI_SSID"] = s["wifi_ssid"]
        payload["android.app.extra.PROVISIONING_WIFI_SECURITY_TYPE"] = s.get("wifi_security", "WPA")
        if s.get("wifi_password"):
            payload["android.app.extra.PROVISIONING_WIFI_PASSWORD"] = s["wifi_password"]
    return payload


def qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- políticas
def validate_policy_settings(platform: str, data: dict) -> dict:
    model = POLICY_MODELS[platform]
    return model.model_validate(data).model_dump()


def render_policy_for_agent(db: Session, policy: Policy, device=None) -> dict:
    """Lo que recibe el agente. Nunca enviamos el PIN en claro."""
    s = dict(policy.settings or {})
    s["version"] = policy.version
    base = get_settings().public_url.rstrip("/")
    if policy.platform == "android":
        pin = s.pop("kiosk_exit_pin", "")
        salt = f"adipack-{policy.id}-{policy.version}"
        s["kiosk_exit_pin_salt"] = salt
        s["kiosk_exit_pin_hash"] = pin_hash(pin, salt) if pin else ""
        apps = []
        ids = s.pop("required_app_ids", [])
        if ids:
            for app in db.scalars(select(App).where(App.id.in_(ids))):
                apps.append({
                    "id": app.id, "package": app.package_name, "version_code": app.version_code,
                    "sha256": app.sha256, "url": f"{base}/api/v1/apps/{app.id}/download",
                })
        s["required_apps"] = apps
        # App ADIPACK Avisos (MQTT): configuración, permisos, mantenerla viva y permitirla en el kiosco
        from .mqtt_bus import AVISOS_ALERT, AVISOS_PACKAGE, AVISOS_SERVICE, app_config_for

        if device is not None and any(a["package"] == AVISOS_PACKAGE for a in apps):
            s["app_configs"] = {AVISOS_PACKAGE: app_config_for(db, device)}
            s["grant_permissions"] = [{"package": AVISOS_PACKAGE,
                                       "permissions": ["android.permission.POST_NOTIFICATIONS"]}]
            s["keep_alive_services"] = [{"package": AVISOS_PACKAGE, "service": AVISOS_SERVICE}]
            s["alert_activities"] = [AVISOS_ALERT]
            extra = list(s.get("kiosk_extra_packages") or [])
            if AVISOS_PACKAGE not in extra:
                s["kiosk_extra_packages"] = extra + [AVISOS_PACKAGE]
    return s


def policy_settings_json(policy: Policy) -> str:
    return json.dumps(policy.settings or {}, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- diagnóstico "¿estoy listo para el QR?"
def setup_checks(db: Session, policy: Policy | None = None) -> list[dict]:
    """Lista de verificación en lenguaje simple. ok=True verde, False rojo, None aviso (no bloquea)."""
    from urllib.parse import urlparse

    settings = get_settings()
    host = urlparse(settings.public_url).hostname or ""
    checks = []
    local = host in ("127.0.0.1", "localhost", "0.0.0.0", "")
    checks.append({
        "ok": not local,
        "title": f"Dirección del servidor: {settings.public_url}",
        "help": "Es una dirección local: las tablets no podrán llegar. Conecta esta PC a la red de planta "
                "y reinicia el MDM, o define MDM_PUBLIC_URL en .env." if local else
                "Las tablets deben estar en la misma red Wi-Fi/LAN que esta PC. Recomendado: IP fija para esta PC.",
    })
    from .netdiag import firewall_rule_ok, local_ipv4s

    ips = local_ipv4s()
    real = [i for i in ips if not i["virtual"]]
    if not local and host not in {i["ip"] for i in ips} and host.replace(".", "").isdigit():
        checks.append({
            "ok": False,
            "title": f"La IP del QR ({host}) ya no es de esta PC",
            "help": "La PC cambió de IP. Elige la correcta en «Dirección que va dentro del QR» (más abajo).",
        })
    elif len(real) > 1:
        checks.append({
            "ok": None,
            "title": f"Esta PC tiene {len(real)} redes: " + ", ".join(
                f"{i['ip']}{' (' + i['name'] + ')' if i['name'] else ''}" for i in real),
            "help": f"El QR usa {host}. Debe ser la IP de la red a la que se conectan las tablets; "
                    "si no lo es, cámbiala en «Dirección que va dentro del QR».",
        })
    fw = firewall_rule_ok(settings.port)
    if fw is not None:
        checks.append({
            "ok": fw,
            "title": f"Firewall de Windows: puerto {settings.port} abierto" if fw else
                     f"Firewall de Windows: el puerto {settings.port} NO está abierto",
            "help": "Las tablets pueden entrar a esta PC." if fw else
                    "Es la causa más común de «No se puede configurar el dispositivo»: la tablet no logra descargar "
                    "el agente. Cierra el MDM y ejecuta ABRIR_FIREWALL.bat (acepta el permiso de administrador).",
        })
    if settings.mqtt_enabled:
        fwm = firewall_rule_ok(settings.mqtt_port)
        if fwm is False:
            checks.append({
                "ok": None,
                "title": f"Firewall de Windows: el puerto de avisos MQTT ({settings.mqtt_port}) NO está abierto",
                "help": "No impide activar tablets, pero los avisos no llegarán. Ejecuta ABRIR_FIREWALL.bat.",
            })
    agent = latest_agent_apk(db)
    checks.append({
        "ok": agent is not None,
        "title": "APK del agente MDM cargado" if agent else "Falta el APK del agente MDM",
        "help": f"Versión {agent.version_name} ({agent.version_code})." if agent else
                "Ve a Apps, sube el APK compilado del agente y marca 'Es el agente MDM'. Sin esto el QR no funciona.",
    })
    if agent is not None:
        try:
            chk = agent_apk_check(agent)
            checks.append({
                "ok": True if chk["signature_checksum"] else None,
                "title": "Firma del agente verificada" if chk["signature_checksum"] else
                         "El agente no tiene firma v2/v3",
                "help": f"El QR valida el agente por su certificado (SHA-256 {chk['cert_sha256'][:23]}…)."
                        if chk["signature_checksum"] else
                        "El QR lo valida por el archivo completo. Usa el APK firmado que genera GitHub Actions.",
            })
            if chk.get("debug"):
                checks.append({
                    "ok": False,
                    "title": "El agente cargado es un APK de PRUEBAS (debug)",
                    "help": "Su firma cambia en cada compilación: las tablets no podrán actualizarlo. Ejecuta "
                            "CREAR_LLAVE_FIRMA.bat, crea los Secrets en GitHub y sube el APK del artefacto «APK-FIRMADO».",
                })
        except ApkError as exc:
            checks.append({"ok": False, "title": "El archivo del agente tiene un problema", "help": str(exc)})
    if policy is not None and policy.platform == "android":
        st = policy.settings or {}
        pkg = st.get("kiosk_package", "")
        ids = st.get("required_app_ids", [])
        apps = list(db.scalars(select(App).where(App.id.in_(ids)))) if ids else []
        has_kiosk_app = any(a.package_name == pkg for a in apps)
        if st.get("kiosk_enabled"):
            checks.append({
                "ok": has_kiosk_app,
                "title": f"App del kiosco ({pkg}) incluida en 'Apps obligatorias'" if has_kiosk_app
                         else f"La app del kiosco ({pkg}) no está en 'Apps obligatorias'",
                "help": "Se instalará sola al activar la tablet." if has_kiosk_app else
                        "Sube el APK del MES/CMMS en Apps y márcalo en la política; si no, la tablet quedará esperando la app.",
            })
        checks.append({
            "ok": True if st.get("wifi_ssid") else None,
            "title": f"Wi-Fi en el QR: {st['wifi_ssid']}" if st.get("wifi_ssid") else "La política no tiene Wi-Fi",
            "help": "La tablet se conecta sola al escanear." if st.get("wifi_ssid") else
                    "Recomendado: pon SSID y clave en la política. Si no, tendrás que elegir la red a mano en la tablet.",
        })
        checks.append({
            "ok": True if st.get("kiosk_exit_pin") else None,
            "title": "PIN de salida del kiosco configurado" if st.get("kiosk_exit_pin") else "Sin PIN de salida",
            "help": "Tocando 7 veces el logo ADIPACK se pide el PIN." if st.get("kiosk_exit_pin") else
                    "Sin PIN sólo podrás salir del kiosco desde la consola (comando 'Salir del kiosco').",
        })
    return checks
