"""Pruebas de la activación por QR v1.3: checksum por firma, registro de descargas y diagnóstico de red."""
import base64
import hashlib
import io
import struct
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from test_mdm import add_agent_apk, enroll, setup_android

FAKE_CERT = b"0\x82\x01\x0a-certificado-DER-de-prueba-ADIPACK"


def _lp(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def make_signed_apk(cert: bytes = FAKE_CERT, block_id: int = 0x7109871A) -> bytes:
    """ZIP mínimo con un 'APK Signing Block' v2/v3 real en estructura (como lo genera apksigner)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("classes.dex", b"dex" * 100)
    data = buf.getvalue()
    eocd = data.rfind(b"PK\x05\x06")
    (cd_off,) = struct.unpack_from("<I", data, eocd + 16)
    signed_data = _lp(b"") + _lp(_lp(cert)) + _lp(b"")  # digests, certificados, atributos
    signer = _lp(signed_data) + _lp(b"firmas") + _lp(b"llave-publica")
    value = _lp(_lp(signer))
    pair = struct.pack("<Q", len(value) + 4) + struct.pack("<I", block_id) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    head, cd, tail = data[:cd_off], data[cd_off:eocd], bytearray(data[eocd:])
    struct.pack_into("<I", tail, 16, cd_off + len(block))
    return head + block + cd + bytes(tail)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def payload_for(client, **kw):
    from app import db as dbm
    from app.models import EnrollmentToken
    from app.services import provisioning_payload
    with dbm._SessionLocal() as db:
        et = db.scalar(select(EnrollmentToken))
        return et.id, provisioning_payload(db, et.token, et.policy, **kw)


def events(action=None):
    from app import db as dbm
    from app.models import AuditLog
    with dbm._SessionLocal() as db:
        q = select(AuditLog).order_by(AuditLog.id)
        if action:
            q = q.where(AuditLog.action == action)
        return [(r.action, r.detail) for r in db.scalars(q)]


def test_signing_cert_parser_v2_v3_and_unsigned(tmp_path):
    from app.services import apk_signing_cert_der
    for bid in (0x7109871A, 0xF05368C0):
        f = tmp_path / f"s{bid}.apk"
        f.write_bytes(make_signed_apk(block_id=bid))
        assert apk_signing_cert_der(f) == FAKE_CERT
    f = tmp_path / "unsigned.apk"
    f.write_bytes(b"no es un zip")
    assert apk_signing_cert_der(f) is None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a", b"b")
    f.write_bytes(buf.getvalue())
    assert apk_signing_cert_der(f) is None


def test_signing_cert_matches_real_apk():
    """Con el APK real del CMMS: el certificado coincide con el que reporta androguard / apksigner."""
    real = Path("/mnt/user-data/uploads/5a3ce6d7-CMMS_ADIPACK_APP.apk")
    if not real.exists():
        pytest.skip("APK real no disponible en este equipo")
    from app.services import apk_signing_cert_der
    cert = apk_signing_cert_der(real)
    assert hashlib.sha256(cert).hexdigest() == "6c62b7a5d34acb449017a481495c44edb36f366e782794b78c35aab8f39f3c1b"


def test_qr_uses_signature_checksum_when_signed(client):
    setup_android(client)
    apk = make_signed_apk()
    add_agent_apk(client, apk)
    _, p = payload_for(client)
    assert p["android.app.extra.PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM"] == b64(hashlib.sha256(FAKE_CERT).digest())
    assert "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM" not in p
    html = client.get("/devices/add/android").text + client.get("/enroll/1").text
    assert "Firma del agente verificada" in html


def test_qr_detects_tampered_agent_file(client):
    setup_android(client)
    add_agent_apk(client, b"ORIGINAL")
    from app.config import get_settings
    (get_settings().apk_dir / "agent.apk").write_bytes(b"OTRO-ARCHIVO")
    from app.services import ApkError
    with pytest.raises(ApkError, match="no coincide"):
        payload_for(client)
    assert "no coincide" in client.get("/enroll/1").text


def test_qr_without_wifi(client):
    csrf, pid, token = setup_android(client)
    from app import db as dbm
    from app.models import Policy
    with dbm._SessionLocal() as db:
        pol = db.get(Policy, pid)
        pol.settings = {**pol.settings, "wifi_ssid": "PLANTA", "wifi_password": "clave-segura", "wifi_security": "WPA"}
        db.commit()
    add_agent_apk(client)
    _, with_wifi = payload_for(client)
    _, without = payload_for(client, include_wifi=False)
    assert with_wifi["android.app.extra.PROVISIONING_WIFI_SSID"] == "PLANTA"
    assert not any("WIFI" in k for k in without)
    assert client.get("/enroll/1/qr.png?wifi=0").content[:4] == b"\x89PNG"
    page = client.get("/enroll/1?wifi=0").text
    assert "QR <b>sin Wi-Fi</b>" in page and "/enroll/1/qr.png?wifi=0" in page


def test_agent_download_is_logged_complete(client):
    setup_android(client)
    add_agent_apk(client, b"A" * 600_000)  # varios bloques
    r = client.get("/agent/android.apk")
    assert r.status_code == 200 and r.content == b"A" * 600_000
    assert r.headers["content-length"] == "600000"
    acts = [a for a, _ in events()]
    assert "qr_descarga_inicio" in acts and "qr_descarga_ok" in acts and "qr_descarga_cortada" not in acts
    act = client.get("/enroll/1/actividad")
    assert act.status_code == 200 and "Agente descargado completo" in act.text


def test_agent_missing_is_logged(client):
    setup_android(client)
    assert client.get("/agent/android.apk").status_code == 404
    assert events("qr_descarga_error")


def test_enroll_rejections_are_logged(client):
    csrf, pid, token = setup_android(client)
    assert enroll(client, "token-que-no-existe").status_code == 403
    rej = events("enroll_rechazado")
    assert rej and "inválido" in rej[0][1] and "Redmi Pad 2" in rej[0][1]
    assert enroll(client, token).status_code == 200
    assert events("enroll")
    html = client.get("/enroll/1/actividad").text
    assert "El MDM rechazó la activación" in html and "Tablet activada" in html


def test_activity_fragment_requires_login(client):
    setup_android(client)
    client.cookies.clear()
    r = client.get("/enroll/1/actividad", follow_redirects=False)
    assert r.status_code in (303, 401, 302)


def test_network_test_page_public_and_logged(client):
    r = client.get("/prueba-conexion")
    assert r.status_code == 200 and "Conexión correcta" in r.text
    assert events("prueba_red")


def test_change_public_url(client, monkeypatch, tmp_path):
    import envfile
    monkeypatch.setattr(envfile, "ENV", tmp_path / ".env")
    csrf, pid, token = setup_android(client)
    add_agent_apk(client)
    r = client.post("/config/public-url", data={"csrf": csrf, "url": "http://localhost:8000", "next_url": "/enroll/1"},
                    follow_redirects=False)
    assert r.status_code == 303
    from app.config import get_settings
    assert get_settings().public_url == "http://mdm.test:8000"
    r = client.post("/config/public-url", data={"csrf": csrf, "url": "http://192.168.10.5:8000/", "next_url": "/enroll/1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/enroll/1"
    assert get_settings().public_url == "http://192.168.10.5:8000"
    assert "MDM_PUBLIC_URL=http://192.168.10.5:8000" in (tmp_path / ".env").read_text()
    _, p = payload_for(client)
    assert p["android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION"] == "http://192.168.10.5:8000/agent/android.apk"
    # sin CSRF no se puede cambiar
    r = client.post("/config/public-url", data={"url": "http://10.0.0.9:8000"}, follow_redirects=False)
    assert r.status_code in (400, 403)
    assert get_settings().public_url == "http://192.168.10.5:8000"


def test_enroll_page_shows_diagnostics(client):
    setup_android(client)
    add_agent_apk(client)
    html = client.get("/enroll/1").text
    for txt in ("¿Qué pasó en la activación?", "Prueba la red con un celular", "Dirección que va dentro del QR",
                "/diagnostico/prueba.png", "http://mdm.test:8000/prueba-conexion"):
        assert txt in html, txt
    assert client.get("/diagnostico/prueba.png").content[:4] == b"\x89PNG"


DEBUG_CERT = b"0\x82\x01\x0a-CN=Android Debug,O=Android,C=US-certificado-de-prueba"


def test_debug_signed_agent_is_flagged(client):
    setup_android(client)
    add_agent_apk(client, make_signed_apk(DEBUG_CERT))
    html = client.get("/devices/add/android").text + client.get("/enroll/1").text
    assert "El agente cargado es un APK de PRUEBAS (debug)" in html


def test_release_signed_agent_not_flagged(client, tmp_path):
    from app.services import is_debug_signed
    setup_android(client)
    add_agent_apk(client, make_signed_apk())
    page = client.get("/devices/add/android").text + client.get("/enroll/1").text
    assert "Firma del agente verificada" in page and "APK de PRUEBAS" not in page
    f = tmp_path / "d.apk"
    f.write_bytes(make_signed_apk(DEBUG_CERT))
    assert is_debug_signed(f)
    f.write_bytes(b"no es apk")
    assert not is_debug_signed(f)
