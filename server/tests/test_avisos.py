"""Avisos por MQTT de punta a punta: consola → base de datos → broker integrado → tablet (paho) → confirmación.

Las «tablets» son clientes paho-mqtt que se comportan como la app ADIPACK Avisos: usan la configuración
gestionada que entrega la política, publican su estado y confirman «recibido» y «leído».
"""
import json
import queue
import socket
import time

import paho.mqtt.client as mqtt
import pytest
from sqlalchemy import select

from test_control import db, flash_of, mk_asset
from test_mdm import enroll, setup_android


@pytest.fixture()
def mclient(monkeypatch, request):
    monkeypatch.setenv("MDM_TEST_MQTT", "true")
    return request.getfixturevalue("client")


def wait(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def add_avisos_app(pid):
    """Sube (en la base) la app ADIPACK Avisos y la agrega a la política como obligatoria."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import App, Policy
    with db() as s:
        app = App(package_name="com.adipack.avisos", label="ADIPACK Avisos", filename="avisos.apk", sha256="0" * 64,
                  version_name="1.0.7", version_code=7)
        s.add(app)
        s.flush()
        pol = s.get(Policy, pid)
        pol.settings = {**pol.settings, "required_app_ids": [*pol.settings.get("required_app_ids", []), app.id]}
        flag_modified(pol, "settings")
        pol.version += 1
        s.commit()


def enroll_and_policy(client, token, hw):
    r = enroll(client, token, hw)
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['device_token']}"}
    pol = client.post("/api/v1/checkin", json={"applied_policy_version": 0}, headers=h).json()["policy"]
    return r.json()["device_id"], pol


class Tablet:
    """Simula la app ADIPACK Avisos con la configuración gestionada que le da el MDM."""

    def __init__(self, cfg, port, connect=True):
        self.cfg, self.port = cfg, port
        self.id = cfg["dispositivo_id"]
        self.inbox = queue.Queue()
        self.subs = queue.Queue()
        self.connected = queue.Queue()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["usuario"], clean_session=False)
        c.username_pw_set(cfg["usuario"], cfg["clave"])
        c.will_set(f"adipack/estado/{self.id}", json.dumps({"estado": "desconectado"}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = lambda cl, u, m: self.inbox.put(json.loads(m.payload))
        c.on_subscribe = lambda cl, u, mid, rcs, p: self.subs.put([r.value for r in rcs])
        self.c = c
        if connect:
            self.start()

    def _on_connect(self, cl, u, flags, rc, p):
        self.connected.put(rc.value)
        if rc.value == 0:
            cl.subscribe(f"adipack/avisos/{self.id}", 1)
            cl.publish(f"adipack/estado/{self.id}", json.dumps({"estado": "en_linea", "app": "1.0.7"}), qos=1, retain=True)

    def start(self):
        self.c.connect("127.0.0.1", self.port, 30)
        self.c.loop_start()
        assert self.connected.get(timeout=5) == 0
        assert self.subs.get(timeout=5) == [1]

    def ack(self, notice_id, estado):
        self.c.publish(f"adipack/ack/{self.id}", json.dumps({"id": notice_id, "estado": estado, "ms": 0}), qos=1)

    def get(self, timeout=5):
        return self.inbox.get(timeout=timeout)

    def stop(self):
        self.c.disconnect()
        self.c.loop_stop()


def deliveries(notice_id):
    from app.models import NoticeDelivery
    with db() as s:
        return {d.device_id: (d.received_at is not None, d.read_at is not None)
                for d in s.scalars(select(NoticeDelivery).where(NoticeDelivery.notice_id == notice_id))}


def send(client, csrf, **form):
    data = {"csrf": csrf, "destino": "todas", "prioridad": "normal", "titulo": "Prueba", "mensaje": "Hola", "vigencia": "8"}
    data.update(form)
    r = client.post("/avisos/enviar", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    return (int(loc.rsplit("/", 1)[1]) if loc.startswith("/avisos/") and loc != "/avisos" else None), flash_of(client.get(loc))


def test_policy_gives_avisos_config_only_when_app_in_policy(mclient):
    csrf, pid, token = setup_android(mclient)
    _, pol = enroll_and_policy(mclient, token, "hw-a")
    assert "app_configs" not in pol and "keep_alive_services" not in pol
    add_avisos_app(pid)
    dev_id, pol = enroll_and_policy(mclient, token, "hw-b")
    cfg = pol["app_configs"]["com.adipack.avisos"]
    from app.mqtt_bus import bus, mqtt_password
    assert cfg == {"broker_host": "mdm.test", "broker_port": bus.port, "usuario": f"tablet-{dev_id}",
                   "clave": mqtt_password(f"tablet-{dev_id}"), "dispositivo_id": str(dev_id), "dispositivo_nombre": "TAB-02"}
    assert "com.adipack.avisos" in pol["kiosk_extra_packages"] and "com.adipack.mes" == pol["kiosk_package"]
    assert pol["grant_permissions"] == [{"package": "com.adipack.avisos", "permissions": ["android.permission.POST_NOTIFICATIONS"]}]
    assert pol["keep_alive_services"] == [{"package": "com.adipack.avisos", "service": "com.adipack.avisos.AvisosService"}]
    assert pol["alert_activities"] == ["com.adipack.avisos/com.adipack.avisos.AlertActivity"]
    # La política guardada no cambia (la config se arma por tablet al entregarla)
    from app.models import Policy
    with db() as s:
        assert "app_configs" not in s.get(Policy, pid).settings


def test_end_to_end_send_receive_read_and_resend_on_reconnect(mclient):
    csrf, pid, token = setup_android(mclient)
    add_avisos_app(pid)
    from app.mqtt_bus import bus
    assert bus.running and bus.wait_ready()
    id1, p1 = enroll_and_policy(mclient, token, "hw-1")
    id2, p2 = enroll_and_policy(mclient, token, "hw-2")
    t1 = Tablet(p1["app_configs"]["com.adipack.avisos"], bus.port)
    t2 = Tablet(p2["app_configs"]["com.adipack.avisos"], bus.port, connect=False)  # apagada
    assert wait(lambda: bus.is_online(id1)) and not bus.is_online(id2)

    nid, msg = send(mclient, csrf, prioridad="urgente", titulo="Parada línea 3", mensaje="A las 14:00 ñ €")
    assert "1 no están conectadas" in msg
    m = t1.get()
    assert (m["tipo"], m["id"], m["titulo"], m["mensaje"], m["prioridad"]) == ("aviso", nid, "Parada línea 3", "A las 14:00 ñ €", "urgente")
    assert m["vence_ms"] > m["enviado_ms"] > 0 and m["de"] == "admin"
    t1.ack(nid, "recibido")
    assert wait(lambda: deliveries(nid)[id1] == (True, False))
    t1.ack(nid, "leido")
    assert wait(lambda: deliveries(nid)[id1] == (True, True))
    assert deliveries(nid)[id2] == (False, False)

    # La tablet 2 se enciende: recibe lo pendiente sin que nadie haga nada
    t2.start()
    m2 = t2.get()
    assert m2["id"] == nid
    t2.ack(nid, "recibido")
    assert wait(lambda: deliveries(nid)[id2] == (True, False))
    page = mclient.get(f"/avisos/{nid}").text
    assert "Leído" in page and "Recibido, sin leer" in page
    lst = mclient.get("/avisos").text
    assert "Parada línea 3" in lst and "2/2" in lst and "1/2" in lst

    # Retirar: las tablets reciben la orden de quitarlo de la pantalla
    mclient.post(f"/avisos/{nid}/retirar", data={"csrf": csrf})
    assert t1.get() == {"tipo": "retirar", "id": nid} and t2.get() == {"tipo": "retirar", "id": nid}
    t1.stop(); t2.stop()
    assert wait(lambda: not bus.is_online(id1) and not bus.is_online(id2))


def test_tablet_cannot_read_others_or_fake_acks(mclient):
    csrf, pid, token = setup_android(mclient)
    add_avisos_app(pid)
    from app.mqtt_bus import bus
    bus.wait_ready()
    id1, p1 = enroll_and_policy(mclient, token, "hw-1")
    id2, p2 = enroll_and_policy(mclient, token, "hw-2")
    t1 = Tablet(p1["app_configs"]["com.adipack.avisos"], bus.port)
    t1.c.subscribe(f"adipack/avisos/{id2}", 1)
    assert t1.subs.get(timeout=5) == [128]
    t2 = Tablet(p2["app_configs"]["com.adipack.avisos"], bus.port)
    nid, _ = send(mclient, csrf, destino="tablet", tablet=str(id2))
    assert t2.get()["id"] == nid
    t1.c.publish(f"adipack/ack/{id2}", json.dumps({"id": nid, "estado": "leido"}), qos=1)  # falsificación
    time.sleep(0.6)
    assert t1.inbox.empty() and deliveries(nid) == {id2: (False, False)}
    # Contraseña equivocada o tablet retirada: no entra
    bad = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x")
    rcq = queue.Queue()
    bad.on_connect = lambda c, u, f, rc, p: rcq.put(rc.value)
    bad.username_pw_set(f"tablet-{id1}", "clave-mala")
    bad.connect("127.0.0.1", bus.port)
    bad.loop_start()
    assert rcq.get(timeout=5) == 134
    bad.loop_stop()
    from app.models import Device
    with db() as s:
        s.get(Device, id1).retired = True
        s.commit()
    t1.stop()
    again = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="y")
    again.on_connect = lambda c, u, f, rc, p: rcq.put(rc.value)
    cfg = p1["app_configs"]["com.adipack.avisos"]
    again.username_pw_set(cfg["usuario"], cfg["clave"])
    again.connect("127.0.0.1", bus.port)
    again.loop_start()
    assert rcq.get(timeout=5) == 134
    again.loop_stop(); t2.stop()


def test_area_target_and_validation(mclient):
    csrf, pid, token = setup_android(mclient)
    add_avisos_app(pid)
    from app.mqtt_bus import bus
    bus.wait_ready()
    id1, p1 = enroll_and_policy(mclient, token, "hw-1")
    id2, p2 = enroll_and_policy(mclient, token, "hw-2")
    mk_asset(mclient, csrf, code="VP-TAB-001", area="Impresión", device_id=str(id1))
    mk_asset(mclient, csrf, code="VP-TAB-002", serial="SN-2", area="S/A", device_id=str(id2))
    nid, _ = send(mclient, csrf, destino="area", area="Impresión")
    assert set(deliveries(nid)) == {id1}
    _, msg = send(mclient, csrf, destino="area", area="Sellado")
    assert "Ninguna tablet recibe ese aviso" in msg
    _, msg = send(mclient, csrf, titulo="")
    assert "Escribe el título" in msg
    _, msg = send(mclient, csrf, prioridad="extrema")
    assert "Prioridad inválida" in msg


def test_integration_user_creates_notices_over_mqtt(mclient):
    csrf, pid, token = setup_android(mclient)
    add_avisos_app(pid)
    from app.mqtt_bus import INTEGRATION_USER, bus, mqtt_password
    bus.wait_ready()
    id1, p1 = enroll_and_policy(mclient, token, "hw-1")
    t1 = Tablet(p1["app_configs"]["com.adipack.avisos"], bus.port)
    i = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mes")
    i.username_pw_set(INTEGRATION_USER, mqtt_password(INTEGRATION_USER))
    i.connect("127.0.0.1", bus.port)
    i.loop_start()
    i.publish("adipack/enviar", json.dumps({"titulo": "Falla extrusora 3", "mensaje": "Esperar supervisor",
                                            "prioridad": "alta", "destino": "tablet:TAB-01", "vigencia_horas": 2}), qos=1)
    m = t1.get()
    assert (m["titulo"], m["prioridad"], m["de"]) == ("Falla extrusora 3", "alta", "integracion")
    i.publish("adipack/enviar", b"esto no es json", qos=1)
    i.publish(f"adipack/avisos/{id1}", json.dumps({"tipo": "aviso", "id": 999, "titulo": "falso"}), qos=1)  # sin permiso
    time.sleep(0.8)
    assert t1.inbox.empty()
    from app.models import AuditLog
    with db() as s:
        acts = [a.action for a in s.scalars(select(AuditLog))]
    assert "aviso_mqtt" in acts and "aviso_mqtt_rechazado" in acts
    i.loop_stop(); t1.stop()


def test_pages_and_missing_app_hints(mclient):
    csrf, pid, token = setup_android(mclient)
    html = mclient.get("/avisos").text
    assert "Paso 1:" in html and "Activo" in html
    add_avisos_app(pid)
    html = mclient.get("/avisos").text
    assert "ninguna política" not in html and "adipack/enviar" in html
    _, msg = send(mclient, csrf)
    assert "Ninguna tablet recibe ese aviso" in msg  # aún no hay tablets enroladas


def test_port_in_use_keeps_mdm_running(monkeypatch, request):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    monkeypatch.setenv("MDM_TEST_MQTT", "true")
    monkeypatch.setenv("MDM_MQTT_PORT_OVERRIDE", str(s.getsockname()[1]))
    c = request.getfixturevalue("client")
    from app.mqtt_bus import bus
    assert not bus.running and "No se pudo abrir el puerto" in bus.broker_error
    from test_mdm import login
    login(c)
    assert "El servicio MQTT no pudo arrancar" in c.get("/avisos").text
    assert c.get("/").status_code == 200
    s.close()
