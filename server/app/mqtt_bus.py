"""Avisos a las tablets por MQTT: broker integrado + cliente del servidor + reglas de entrega.

Temas (todo en JSON UTF-8):
  adipack/avisos/<id>   servidor → tablet   {"tipo":"aviso"|"retirar", "id", "titulo", "mensaje", "prioridad", ...}
  adipack/ack/<id>      tablet → servidor   {"id": 12, "estado": "recibido"|"leido", "ms": ...}
  adipack/estado/<id>   tablet → todos      {"estado": "en_linea"|"desconectado", "app": "1.0.5"} (retenido, Last Will)
  adipack/enviar        otros sistemas → servidor (MES, CMMS, Node-RED…) para crear avisos sin la consola

<id> es el número del equipo en el MDM (no cambia aunque se renombre la tablet).
La base de datos es la fuente de verdad: un aviso queda «pendiente» en cada tablet hasta que ésta
confirma que lo recibió; si la tablet estaba apagada, se le reenvía al volver a conectarse.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import get_settings
from .mqtt_broker import Broker, BrokerThread, Identity

log = logging.getLogger("adipack.mqtt")

AVISOS_PACKAGE = "com.adipack.avisos"
AVISOS_SERVICE = "com.adipack.avisos.AvisosService"
AVISOS_ALERT = "com.adipack.avisos/com.adipack.avisos.AlertActivity"
SERVER_USER = "mdm-servidor"
INTEGRATION_USER = "integracion"
T_AVISO, T_ACK, T_ESTADO, T_ENVIAR = "adipack/avisos/", "adipack/ack/", "adipack/estado/", "adipack/enviar"
PRIORIDADES = {"normal": "Normal", "alta": "Alta", "urgente": "Urgente"}
VIGENCIAS = [(1, "1 hora"), (4, "4 horas"), (8, "8 horas (un turno)"), (24, "24 horas"), (72, "3 días")]
RESEND_AFTER = 45          # s sin confirmación antes de reintentar a una tablet en línea
MAX_ATTEMPTS = 50
_TABLET_RE = re.compile(r"^tablet-(\d{1,9})$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


# ---------------------------------------------------------------- credenciales (derivadas, no se guardan)
def mqtt_password(user: str) -> str:
    key = get_settings().secret_key.encode()
    raw = hmac.new(key, f"adipack-mqtt:{user}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")[:24]


def tablet_user(device_id: int) -> str:
    return f"tablet-{device_id}"


def _device_ok(device_id: int) -> bool:
    from .db import new_session
    from .models import Device

    with new_session() as db:
        dev = db.get(Device, device_id)
        return dev is not None and not dev.retired and dev.platform == "android"


def authenticate(username: str, password: bytes, client_id: str) -> Identity | None:
    """Usuarios: mdm-servidor (todo), integracion (crear avisos) y tablet-<id> (sólo lo suyo)."""
    try:
        pw = password.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not username or not hmac.compare_digest(pw, mqtt_password(username)):
        return None
    if username == SERVER_USER:
        return Identity(username)
    if username == INTEGRATION_USER:
        return Identity(username, can_publish=lambda t: t == T_ENVIAR,
                        can_subscribe=lambda f: f.startswith(T_ESTADO))
    m = _TABLET_RE.match(username)
    if m and _device_ok(int(m.group(1))):
        n = m.group(1)
        return Identity(username,
                        can_publish=lambda t: t in (T_ACK + n, T_ESTADO + n),
                        can_subscribe=lambda f: f == T_AVISO + n)
    return None


# ---------------------------------------------------------------- bus
class AvisosBus:
    def __init__(self):
        self.broker: Broker | None = None
        self.broker_thread: BrokerThread | None = None
        self.broker_error = ""
        self.client = None
        self.client_connected = False
        self.presence: dict[int, dict] = {}   # device_id -> {"online", "since", "app"}
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None
        self._lock = threading.Lock()
        self.port = 0

    # -- ciclo de vida
    def start(self) -> None:
        settings = get_settings()
        self.broker_error = ""
        self._stop.clear()
        try:
            self.broker = Broker(settings.mqtt_bind, settings.mqtt_port, authenticate, self._on_broker_event)
            self.broker_thread = BrokerThread(self.broker)
            self.broker_thread.start()
            self.port = self.broker.port
        except OSError as exc:
            self.broker, self.broker_thread = None, None
            self.broker_error = (f"No se pudo abrir el puerto {settings.mqtt_port}: {exc}. ¿Hay otro programa "
                                 "(por ejemplo Mosquitto) usando ese puerto? Cámbialo con MDM_MQTT_PORT en .env.")
            log.error(self.broker_error)
            return
        self._start_client()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="avisos-reenvio", daemon=True)
        self._sweeper.start()

    def stop(self) -> None:
        self._stop.set()
        if self.client is not None:
            try:
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        self.client_connected = False
        if self.broker_thread is not None:
            self.broker_thread.stop()
        self.broker, self.broker_thread = None, None
        if self._sweeper is not None:
            self._sweeper.join(3)
            self._sweeper = None
        self.presence.clear()

    @property
    def running(self) -> bool:
        return self.broker is not None

    def status(self) -> dict:
        return {
            "activo": self.running, "error": self.broker_error, "puerto": self.port or get_settings().mqtt_port,
            "servidor_conectado": self.client_connected,
            "en_linea": sorted(d for d, p in self.presence.items() if p.get("online")),
            "clientes": self.broker.online_clients() if self.broker else [],
            "contadores": dict(self.broker.counters) if self.broker else {},
        }

    def is_online(self, device_id: int) -> bool:
        return bool(self.presence.get(device_id, {}).get("online"))

    # -- cliente del servidor (paho)
    def _start_client(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mdm-servidor", clean_session=True)
        c.username_pw_set(SERVER_USER, mqtt_password(SERVER_USER))
        c.reconnect_delay_set(1, 10)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async("127.0.0.1", self.port, keepalive=30)
        c.loop_start()
        self.client = c

    def wait_ready(self, timeout: float = 5) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.client_connected:
                return True
            time.sleep(0.05)
        return False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("El servidor no pudo conectarse a su broker MQTT: %s", reason_code)
            return
        self.client_connected = True
        client.subscribe([(T_ACK + "+", 1), (T_ESTADO + "+", 1), (T_ENVIAR, 1)])
        log.info("Servidor conectado al broker MQTT (puerto %s)", self.port)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.client_connected = False

    def _on_broker_event(self, kind: str, data: dict) -> None:
        """Eventos del broker (en su hilo). La desconexión cuenta siempre, aunque la tablet no avise."""
        if kind == "rechazado":
            log.warning("MQTT: conexión rechazada usuario=%r desde %s (código %s)", data["user"], data["peer"], data["code"])
        elif kind == "desconectado":
            m = _TABLET_RE.match(data.get("user") or "")
            if m:
                dev = int(m.group(1))
                prev = self.presence.get(dev, {})
                self.presence[dev] = {"online": False, "since": utcnow(), "app": prev.get("app", "")}

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic.startswith(T_ACK):
                self._handle_ack(int(msg.topic[len(T_ACK):]), msg.payload)
            elif msg.topic.startswith(T_ESTADO):
                self._handle_presence(int(msg.topic[len(T_ESTADO):]), msg.payload)
            elif msg.topic == T_ENVIAR:
                self._handle_integration(msg.payload)
        except Exception:  # noqa: BLE001 - un mensaje malo nunca debe tumbar el bus
            log.exception("Error procesando %s", msg.topic)

    # -- entrada
    def _handle_ack(self, device_id: int, payload: bytes) -> None:
        from .db import new_session
        from .models import NoticeDelivery

        data = json.loads(payload.decode("utf-8"))
        notice_id, estado = int(data["id"]), str(data.get("estado", ""))
        with new_session() as db:
            d = db.scalar(select(NoticeDelivery).where(NoticeDelivery.notice_id == notice_id,
                                                       NoticeDelivery.device_id == device_id))
            if d is None:
                return
            now = utcnow()
            if d.received_at is None:
                d.received_at = now
            if estado == "leido" and d.read_at is None:
                d.read_at = now
            db.commit()

    def _handle_presence(self, device_id: int, payload: bytes) -> None:
        try:
            data = json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError:
            data = {}
        online = data.get("estado") == "en_linea"
        was = self.is_online(device_id)
        self.presence[device_id] = {"online": online, "since": utcnow(), "app": str(data.get("app", ""))[:20]}
        if online and not was:
            self.resend_pending(device_id, force=True)
            self._resend_retires(device_id)

    def _resend_retires(self, device_id: int) -> None:
        """Si se retiró un aviso mientras la tablet estaba apagada, quitarlo de su pantalla al volver."""
        from .db import new_session
        from .models import Notice, NoticeDelivery

        with new_session() as db:
            since = utcnow() - timedelta(days=3)
            for d in db.scalars(select(NoticeDelivery).join(Notice).where(
                    NoticeDelivery.device_id == device_id, Notice.retired.is_(True), Notice.retired_at > since,
                    NoticeDelivery.received_at.is_not(None), NoticeDelivery.read_at.is_(None))):
                self._publish(device_id, {"tipo": "retirar", "id": d.notice_id})

    def _handle_integration(self, payload: bytes) -> None:
        from .db import new_session
        from .services import audit

        try:
            data = json.loads(payload.decode("utf-8"))
            destino = str(data.get("destino", "todas"))
            kind, _, value = destino.partition(":")
            with new_session() as db:
                n = create_notice(db, "integracion", str(data.get("titulo", "")), str(data.get("mensaje", "")),
                                  str(data.get("prioridad", "normal")), kind.strip() or "todas", value.strip(),
                                  int(data.get("vigencia_horas", 8)))
                audit(db, "integracion", "aviso_mqtt", f"Aviso #{n.id} «{n.title}» → {n.target_label}")
                db.commit()
                self.send_notice(db, n)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            log.warning("Aviso de integración rechazado: %s", exc)
            with new_session() as db:
                audit(db, "integracion", "aviso_mqtt_rechazado", str(exc)[:500])
                db.commit()

    # -- salida
    def _publish(self, device_id: int, data: dict) -> bool:
        if self.client is None or not self.client_connected:
            return False
        info = self.client.publish(T_AVISO + str(device_id), json.dumps(data, ensure_ascii=False).encode("utf-8"), qos=1)
        return info.rc == 0

    def send_notice(self, db, notice) -> int:
        """Publica un aviso a todas sus tablets. Devuelve a cuántas se envió (las demás quedan pendientes)."""
        sent = 0
        payload = notice_payload(notice)
        for d in notice.deliveries:
            if d.received_at is None and self._publish(d.device_id, payload):
                d.sent_at, d.attempts = utcnow(), d.attempts + 1
                sent += 1
        db.commit()
        return sent

    def retire_notice(self, db, notice) -> None:
        for d in notice.deliveries:
            self._publish(d.device_id, {"tipo": "retirar", "id": notice.id})

    def resend_pending(self, device_id: int | None = None, force: bool = False) -> int:
        """Reenvía lo no confirmado (a una tablet o a todas las que estén en línea)."""
        from .db import new_session
        from .models import Notice, NoticeDelivery

        if self.client is None or not self.client_connected:
            return 0
        n = 0
        with self._lock, new_session() as db:
            now = utcnow()
            q = (select(NoticeDelivery).join(Notice)
                 .where(NoticeDelivery.received_at.is_(None), Notice.retired.is_(False), Notice.expires_at > now,
                        NoticeDelivery.attempts < MAX_ATTEMPTS))
            if device_id is not None:
                q = q.where(NoticeDelivery.device_id == device_id)
            for d in db.scalars(q.order_by(NoticeDelivery.id)):
                if not self.is_online(d.device_id):
                    continue
                if not force and d.sent_at and (now - _aware(d.sent_at)).total_seconds() < RESEND_AFTER:
                    continue
                if self._publish(d.device_id, notice_payload(d.notice)):
                    d.sent_at, d.attempts = now, d.attempts + 1
                    n += 1
            db.commit()
        return n

    def _sweep_loop(self) -> None:
        while not self._stop.wait(15):
            try:
                self.resend_pending()
            except Exception:  # noqa: BLE001
                log.exception("Error reenviando avisos pendientes")


bus = AvisosBus()


# ---------------------------------------------------------------- reglas de negocio
def notice_payload(n) -> dict:
    return {
        "tipo": "aviso", "id": n.id, "titulo": n.title, "mensaje": n.body, "prioridad": n.priority,
        "de": n.created_by, "enviado_ms": int(_aware(n.created_at).timestamp() * 1000),
        "vence_ms": int(_aware(n.expires_at).timestamp() * 1000),
    }


def avisos_app(db):
    from .models import App

    return db.scalar(select(App).where(App.package_name == AVISOS_PACKAGE).order_by(App.version_code.desc()))


def devices_with_avisos(db) -> list:
    """Tablets activas cuya política instala la app ADIPACK Avisos."""
    from .models import App, Device, Policy

    app_ids = set(db.scalars(select(App.id).where(App.package_name == AVISOS_PACKAGE)))
    if not app_ids:
        return []
    out = []
    for dev in db.scalars(select(Device).where(Device.retired.is_(False), Device.platform == "android")
                          .order_by(Device.name)):
        pol: Policy | None = dev.policy
        if pol is not None and app_ids & set((pol.settings or {}).get("required_app_ids", [])):
            out.append(dev)
    return out


def resolve_targets(db, kind: str, value: str) -> tuple[list, str]:
    """(tablets, descripción) para «todas», «area:<Área>» o «tablet:<id o nombre>»."""
    from .control import AREA_SA, norm_area
    from .models import Asset

    devices = devices_with_avisos(db)
    if kind == "todas":
        return devices, "Todas las tablets"
    if kind == "area":
        area = norm_area(value)
        if area is None or area == AREA_SA:
            raise ValueError(f"Área inválida: {value!r}")
        ids = set(db.scalars(select(Asset.device_id).where(Asset.area == area, Asset.device_id.is_not(None))))
        return [d for d in devices if d.id in ids], f"Área {area}"
    if kind == "tablet":
        v = value.strip()
        match = [d for d in devices if str(d.id) == v or d.name.lower() == v.lower()]
        if not match:
            raise ValueError(f"La tablet {value!r} no existe o no tiene la app ADIPACK Avisos en su política")
        return match, f"Tablet {match[0].name}"
    raise ValueError(f"Destino inválido: {kind!r} (usa todas, area:<Área> o tablet:<nombre>)")


def create_notice(db, user: str, title: str, body: str, priority: str, kind: str, value: str, hours: int):
    from .models import Notice, NoticeDelivery

    title, body = title.strip(), body.strip()
    if not title:
        raise ValueError("Escribe el título del aviso")
    if len(title) > 80 or len(body) > 500:
        raise ValueError("Título máximo 80 caracteres y mensaje máximo 500")
    if priority not in PRIORIDADES:
        raise ValueError("Prioridad inválida (normal, alta o urgente)")
    if not 1 <= hours <= 168:
        raise ValueError("La vigencia debe estar entre 1 y 168 horas")
    devices, label = resolve_targets(db, kind, value)
    if not devices:
        raise ValueError("Ninguna tablet recibe ese aviso: revisa que tengan la app ADIPACK Avisos en su política "
                         "y, si es por área, que estén enlazadas a un equipo del inventario con esa área")
    n = Notice(created_by=user, title=title, body=body, priority=priority, target_kind=kind, target_value=value,
               target_label=label, expires_at=utcnow() + timedelta(hours=hours))
    n.deliveries = [NoticeDelivery(device_id=d.id, device_name=d.name) for d in devices]
    db.add(n)
    db.flush()
    return n


def app_config_for(db, device) -> dict:
    """Configuración gestionada que el agente MDM entrega a la app ADIPACK Avisos de esa tablet."""
    from urllib.parse import urlparse

    s = get_settings()
    user = tablet_user(device.id)
    return {
        "broker_host": urlparse(s.public_url).hostname or "",
        "broker_port": bus.port or s.mqtt_port,
        "usuario": user,
        "clave": mqtt_password(user),
        "dispositivo_id": str(device.id),
        "dispositivo_nombre": device.name,
    }
