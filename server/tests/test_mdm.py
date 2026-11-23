import base64
import hashlib
import json
import re

from sqlalchemy import select


def csrf_of(client, url="/login"):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client):
    tok = csrf_of(client)
    r = client.post("/login", data={"csrf": tok, "username": "admin", "password": "CambiarYa!2026"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf_of(client, "/")


def android_policy_form(csrf, **over):
    data = {"csrf": csrf, "platform": "android", "id": "", "name": "Tablets MES", "description": "",
            "kiosk_enabled": "on", "kiosk_package": "com.adipack.mes", "kiosk_extra_packages": "",
            "kiosk_exit_pin": "2468", "system_updates": "windowed", "update_window_start_min": "02:00",
            "update_window_end_min": "05:00", "wifi_ssid": "", "wifi_password": "", "wifi_security": "WPA",
            "app_config": '{"server_url": "http://mes.local"}', "sync_interval_sec": "60",
            "disable_screenshots": "on", "disable_status_bar": "on"}
    data.update(over)
    return data


def add_agent_apk(client, content=b"FAKE-AGENT-APK"):
    """Inserta un APK de agente sin parsear (los tests no tienen un APK real)."""
    from app import db as dbm
    from app.config import get_settings
    from app.models import App
    s = get_settings()
    s.apk_dir.mkdir(parents=True, exist_ok=True)
    (s.apk_dir / "agent.apk").write_bytes(content)
    with dbm._SessionLocal() as db:
        db.add(App(package_name="com.adipack.mdm.agent", filename="agent.apk", sha256=hashlib.sha256(content).hexdigest(),
                   size=len(content), is_agent=True, version_code=1))
        db.commit()


def setup_android(client):
    csrf = login(client)
    r = client.post("/policies/save", data=android_policy_form(csrf), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/policies/")
    from app import db as dbm
    from app.models import Policy
    with dbm._SessionLocal() as db:
        pid = db.scalar(select(Policy.id).where(Policy.name == "Tablets MES"))
    r = client.post("/enroll/new", data={"csrf": csrf, "policy_id": pid, "name_prefix": "TAB", "max_uses": 7, "days": 7},
                    follow_redirects=False)
    from app.models import EnrollmentToken
    with dbm._SessionLocal() as db:
        token = db.scalar(select(EnrollmentToken.token).order_by(EnrollmentToken.id.desc()))
    return csrf, pid, token


def enroll(client, token, hw="hw-1", platform="android"):
    r = client.post("/api/v1/enroll", json={"enrollment_token": token, "platform": platform, "hardware_id": hw,
                                            "manufacturer": "Xiaomi", "model": "Redmi Pad 2", "agent_version": "1.0"})
    return r


# ---------------------------------------------------------------- tests
def test_requires_login_and_csrf(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/login", data={"csrf": "malo", "username": "admin", "password": "x"})
    assert r.status_code == 400


def test_bad_password(client):
    tok = csrf_of(client)
    r = client.post("/login", data={"csrf": tok, "username": "admin", "password": "nope"})
    assert "incorrectos" in r.text


def test_policy_validation_kiosk_requires_package(client):
    csrf = login(client)
    r = client.post("/policies/save", data=android_policy_form(csrf, kiosk_package=""))
    assert "No se guardó" in r.text
    r = client.post("/policies/save", data=android_policy_form(csrf, kiosk_package="no valido"))
    assert "inválido" in r.text
    r = client.post("/policies/save", data=android_policy_form(csrf, app_config="{malo"))
    assert "JSON inválido" in r.text
    r = client.post("/policies/save", data=android_policy_form(csrf, update_window_start_min="25:99"))
    assert "hora inválida" in r.text


def test_full_android_flow(client):
    csrf, pid, token = setup_android(client)
    r = enroll(client, token)
    assert r.status_code == 200, r.text
    dev_token = r.json()["device_token"]
    assert r.json()["name"] == "TAB-01"
    h = {"Authorization": f"Bearer {dev_token}"}

    r = client.post("/api/v1/checkin", json={"applied_policy_version": 0, "status": {"battery": 80, "kiosk_active": True}}, headers=h)
    body = r.json()
    pol = body["policy"]
    assert pol["kiosk_package"] == "com.adipack.mes"
    assert "kiosk_exit_pin" not in pol and len(pol["kiosk_exit_pin_hash"]) == 64
    expected = hashlib.sha256((pol["kiosk_exit_pin_salt"] + ":2468").encode()).hexdigest()
    assert pol["kiosk_exit_pin_hash"] == expected
    assert pol["app_config"] == {"server_url": "http://mes.local"}

    # Política ya aplicada -> no se reenvía
    r = client.post("/api/v1/checkin", json={"applied_policy_version": body["policy_version"]}, headers=h)
    assert r.json()["policy"] is None

    # Comando
    dev_id = body["device_id"]
    client.post(f"/devices/{dev_id}/command", data={"csrf": csrf, "type": "lock"})
    r = client.post("/api/v1/checkin", json={"applied_policy_version": 1}, headers=h)
    cmds = r.json()["commands"]
    assert [c["type"] for c in cmds] == ["lock"]
    # No se reenvía inmediatamente
    assert client.post("/api/v1/checkin", json={"applied_policy_version": 1}, headers=h).json()["commands"] == []
    r = client.post(f"/api/v1/commands/{cmds[0]['id']}/result", json={"success": True, "output": "ok"}, headers=h)
    assert r.json()["state"] == "done"
    # idempotente
    assert client.post(f"/api/v1/commands/{cmds[0]['id']}/result", json={"success": False}, headers=h).json()["state"] == "done"


def test_dangerous_command_needs_confirmation_and_wipe_retires(client):
    csrf, pid, token = setup_android(client)
    r = enroll(client, token)
    h = {"Authorization": f"Bearer {r.json()['device_token']}"}
    dev_id = r.json()["device_id"]
    r = client.post(f"/devices/{dev_id}/command", data={"csrf": csrf, "type": "wipe"})
    assert "Acción peligrosa" in r.text
    client.post(f"/devices/{dev_id}/command", data={"csrf": csrf, "type": "wipe", "confirm": "TAB-01"})
    cmds = client.post("/api/v1/checkin", json={}, headers=h).json()["commands"]
    assert cmds[0]["type"] == "wipe"
    client.post(f"/api/v1/commands/{cmds[0]['id']}/result", json={"success": True}, headers=h)
    assert client.post("/api/v1/checkin", json={}, headers=h).status_code == 401


def test_cannot_uninstall_agent(client):
    csrf, pid, token = setup_android(client)
    dev_id = enroll(client, token).json()["device_id"]
    r = client.post(f"/devices/{dev_id}/command", data={"csrf": csrf, "type": "uninstall_app", "package": "com.adipack.mdm.agent"})
    assert "No se puede" in r.text


def test_token_platform_and_limits(client):
    csrf, pid, token = setup_android(client)
    assert enroll(client, token, platform="windows").status_code == 400
    assert client.post("/api/v1/enroll", json={"enrollment_token": "x" * 20, "platform": "android", "hardware_id": "h"}).status_code == 403
    # re-enrolar el mismo hardware no duplica
    a = enroll(client, token, hw="same").json()
    b = enroll(client, token, hw="same").json()
    assert a["device_id"] == b["device_id"]
    # el token viejo deja de servir
    assert client.post("/api/v1/checkin", json={}, headers={"Authorization": f"Bearer {a['device_token']}"}).status_code == 401


def test_qr_payload_checksum(client):
    csrf, pid, token = setup_android(client)
    add_agent_apk(client, b"AGENT-BYTES")
    from app import db as dbm
    from app.models import EnrollmentToken
    from app.services import provisioning_payload
    with dbm._SessionLocal() as db:
        et = db.scalar(select(EnrollmentToken))
        p = provisioning_payload(db, et.token, et.policy)
    want = base64.urlsafe_b64encode(hashlib.sha256(b"AGENT-BYTES").digest()).decode().rstrip("=")
    assert p["android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM"] == want
    assert p["android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE"]["enrollment_token"] == token
    assert p["android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME"] == "com.adipack.mdm.agent/com.adipack.mdm.agent.AdminReceiver"
    r = client.get(f"/enroll/{et.id}/qr.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/agent/android.apk").content == b"AGENT-BYTES"


def test_upload_rejects_non_apk(client):
    csrf = login(client)
    r = client.post("/apps/upload", data={"csrf": csrf}, files={"file": ("x.apk", b"not a zip", "application/octet-stream")})
    assert "no es un APK válido" in r.text


def test_pc_flow(client):
    csrf = login(client)
    from app import db as dbm
    from app.models import EnrollmentToken, Policy
    with dbm._SessionLocal() as db:
        pid = db.scalar(select(Policy.id).where(Policy.platform == "windows"))
    client.post("/enroll/new", data={"csrf": csrf, "policy_id": pid, "name_prefix": "PC", "max_uses": 0, "days": 0})
    with dbm._SessionLocal() as db:
        token = db.scalar(select(EnrollmentToken.token).order_by(EnrollmentToken.id.desc()))
    r = enroll(client, token, hw="pc1", platform="linux")  # windows/linux comparten política PC
    assert r.status_code == 200 and r.json()["name"] == "PC-01"
    h = {"Authorization": f"Bearer {r.json()['device_token']}"}
    assert client.post("/api/v1/checkin", json={}, headers=h).json()["policy"]["kiosk_enabled"] is False
    # run_script bloqueado por política
    r2 = client.post(f"/devices/{r.json()['device_id']}/command", data={"csrf": csrf, "type": "run_script", "script": "echo hi", "confirm": "PC-01"})
    assert "no permite scripts" in r2.text


def test_pages_render(client):
    csrf, pid, token = setup_android(client)
    dev_id = enroll(client, token).json()["device_id"]
    for url in ["/", "/policies", f"/policies/{pid}", "/policies/new?platform=windows", "/apps", "/enroll", "/enroll/1", "/audit", "/account", f"/devices/{dev_id}"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])


def test_auto_public_url(monkeypatch):
    monkeypatch.setenv("MDM_PUBLIC_URL", "auto")
    monkeypatch.setenv("MDM_PORT", "8123")
    from app.config import Settings
    s = Settings()
    assert s.public_url.startswith("http://") and s.public_url.endswith(":8123")


def test_setup_checks_and_qr_extras(client):
    csrf, pid, token = setup_android(client)
    r = client.get("/enroll/1")
    assert "Falta el APK del agente" in r.text and "no está en" in r.text and "Todavía no se puede generar" in r.text
    add_agent_apk(client, b"X")
    r = client.get("/enroll/1")
    assert "APK del agente MDM cargado" in r.text and "Imprimir QR" in r.text
    assert "America/Caracas" in r.text
    assert "Configuración inicial" in client.get("/").text


REAL_APK = "/mnt/user-data/uploads/5a3ce6d7-CMMS_ADIPACK_APP.apk"


def test_policy_stores_update_window_in_minutes(client):
    csrf, pid, token = setup_android(client)
    from app import db as dbm
    from app.models import Policy
    with dbm._SessionLocal() as db:
        st = db.get(Policy, pid).settings
    assert st["update_window_start_min"] == 120 and st["update_window_end_min"] == 300
    assert 'value="02:00"' in client.get(f"/policies/{pid}").text


def test_android_wizard_creates_policy_and_qr(client):
    import os
    import pytest
    if not os.path.exists(REAL_APK):
        pytest.skip("APK real no disponible")
    csrf = login(client)
    r = client.get("/devices/add/android")
    assert "Sube el APK del agente" in r.text
    with open(REAL_APK, "rb") as f:  # subir la app real del CMMS
        r = client.post("/apps/upload", data={"csrf": csrf, "next": "/devices/add/android"},
                        files={"file": ("CMMS_ADIPACK_APP.apk", f, "application/vnd.android.package-archive")})
    assert "Aplicación cargada: Adipack CMMS" in r.text or "Adipack CMMS" in r.text
    assert r.url.path == "/devices/add/android"
    from app import db as dbm
    from app.models import App, Policy
    with dbm._SessionLocal() as db:
        app_id = db.scalar(select(App.id).where(App.package_name == "com.adipack.cmmsconnect"))
    # PIN inválido
    r = client.post("/devices/add/android/policy", data={"csrf": csrf, "name": "Tablets CMMS planta", "app_id": app_id, "pin": "12"})
    assert "entre 4 y 12 dígitos" in r.text
    # Wi-Fi con clave corta
    r = client.post("/devices/add/android/policy", data={"csrf": csrf, "name": "Tablets CMMS planta", "app_id": app_id,
                                                          "pin": "246810", "wifi_ssid": "PLANTA", "wifi_password": "123"})
    assert "8 caracteres" in r.text
    r = client.post("/devices/add/android/policy", data={"csrf": csrf, "name": "Tablets CMMS planta", "app_id": app_id,
                                                          "pin": "246810", "wifi_ssid": "PLANTA", "wifi_password": "ClaveWifi2026"})
    assert "creada" in r.text
    with dbm._SessionLocal() as db:
        p = db.scalar(select(Policy).where(Policy.name == "Tablets CMMS planta"))
        st = p.settings
    assert st["kiosk_package"] == "com.adipack.cmmsconnect" and st["required_app_ids"] == [app_id]
    assert st["kiosk_enabled"] and st["disable_factory_reset"] and not st["disable_camera"]
    add_agent_apk(client, b"AGENTE")
    r = client.post("/enroll/new", data={"csrf": csrf, "policy_id": p.id, "name_prefix": "TAB", "max_uses": "7", "days": "7"})
    assert r.status_code == 200 and "Cómo activar cada tablet" in r.text and "qr.png" in r.text
    assert "PLANTA" in r.text  # Wi-Fi dentro del QR


def test_upload_agent_autodetect_and_mismatch(client):
    import os
    import pytest
    if not os.path.exists(REAL_APK):
        pytest.skip("APK real no disponible")
    csrf = login(client)
    with open(REAL_APK, "rb") as f:  # marcar como agente un APK que no lo es
        r = client.post("/apps/upload", data={"csrf": csrf, "is_agent": "on"}, files={"file": ("x.apk", f, "application/octet-stream")})
    assert "Desmarca la casilla" in r.text
    with open(REAL_APK, "rb") as f:
        client.post("/apps/upload", data={"csrf": csrf}, files={"file": ("x.apk", f, "application/octet-stream")})
    with open(REAL_APK, "rb") as f:
        r = client.post("/apps/upload", data={"csrf": csrf}, files={"file": ("x.apk", f, "application/octet-stream")})
    assert "ya está cargado" in r.text


def test_apk_file_closed_before_move(client):
    """Regresión del error de Windows: el APK debe quedar cerrado antes de moverlo."""
    import os
    import pytest
    psutil = pytest.importorskip("psutil")
    if not os.path.exists(REAL_APK):
        pytest.skip("APK real no disponible")
    import app.services as S
    real = S.os.replace

    def windows_like(src, dst):
        if any(os.path.basename(f.path) == os.path.basename(str(src)) for f in psutil.Process().open_files()):
            raise PermissionError(32, "archivo en uso", str(src))
        return real(src, dst)
    S.os.replace = windows_like
    try:
        csrf = login(client)
        with open(REAL_APK, "rb") as f:
            r = client.post("/apps/upload", data={"csrf": csrf}, files={"file": ("x.apk", f, "application/octet-stream")})
        assert "Adipack CMMS" in r.text and "Ocurrió un error" not in r.text
    finally:
        S.os.replace = real


def test_friendly_error_pages(client):
    login(client)
    r = client.get("/devices/9999")
    assert r.status_code == 404 and "Ese equipo no existe" in r.text and "<!doctype html>" in r.text
    r = client.post("/policies/save", data={"csrf": "malo"})
    assert r.status_code == 400 and "caducó" in r.text
    assert client.post("/api/v1/checkin", json={}).headers["content-type"].startswith("application/json")


def test_add_pages_and_bulk(client):
    csrf, pid, token = setup_android(client)
    for url in ["/devices/add", "/devices/add/android", "/devices/add/pc", "/enroll"]:
        assert client.get(url).status_code == 200, url
    r = client.post("/devices/bulk", data={"csrf": csrf, "type": "sync"})
    assert "Marca al menos un equipo" in r.text


def test_favicon_logo(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon" and r.content[:4] == b"\x00\x00\x01\x00"
    assert client.get("/static/favicon.png").status_code == 200
    assert client.get("/static/apple-touch-icon.png").status_code == 200
    assert 'href="/static/favicon.ico"' in client.get("/login").text
