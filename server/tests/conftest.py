import os, sys, tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("MDM_DATA_DIR", tmp)
    # MDM_TEST_DATABASE_URL=postgresql+psycopg://... corre las mismas pruebas sobre PostgreSQL
    pg = os.environ.get("MDM_TEST_DATABASE_URL")
    monkeypatch.setenv("MDM_DATABASE_URL", pg or f"sqlite:///{tmp}/t.db")
    monkeypatch.setenv("MDM_PUBLIC_URL", "http://mdm.test:8000")
    monkeypatch.setenv("MDM_SECRET_KEY", "test-secret")
    # El broker MQTT sólo se enciende en las pruebas de avisos (ver test_avisos.py), en un puerto libre.
    monkeypatch.setenv("MDM_MQTT_ENABLED", os.environ.get("MDM_TEST_MQTT", "false"))
    monkeypatch.setenv("MDM_MQTT_PORT", os.environ.get("MDM_MQTT_PORT_OVERRIDE", "0"))
    from app import config, db
    config.get_settings.cache_clear()
    db._engine = None; db._SessionLocal = None
    eng = db.init_engine()
    if pg:  # base limpia en cada prueba
        from app import models  # noqa: F401
        db.Base.metadata.drop_all(eng)
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app()) as c:
        yield c
