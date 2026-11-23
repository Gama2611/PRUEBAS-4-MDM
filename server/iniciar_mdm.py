#!/usr/bin/env python3
"""Arranque con un clic de ADIPACK MDM.

- La primera vez crea el archivo .env con una clave secreta aleatoria.
- Detecta la IP de esta PC en la red de planta (la que irá dentro del QR).
- En Windows intenta abrir el puerto en el Firewall.
- Abre la consola en el navegador y deja el servidor encendido.
No cierres esta ventana: si la cierras, el MDM se apaga (las tablets siguen en kiosco).
"""
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # .env, data/ y la base de datos viven junto a este archivo
sys.path.insert(0, str(HERE))

from envfile import ensure_env  # noqa: E402


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def open_firewall(port: int) -> None:
    if not sys.platform.startswith("win"):
        return
    name = f"ADIPACK MDM {port}"
    show = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                          capture_output=True, text=True)
    if show.returncode == 0 and name in show.stdout:
        print(f"✓ Firewall: puerto {port} ya permitido.")
        return
    add = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                          "action=allow", "protocol=TCP", f"localport={port}", "profile=any"], capture_output=True, text=True)
    if add.returncode == 0:
        print(f"✓ Firewall: puerto {port} abierto para las tablets.")
    else:
        print(f"⚠ El puerto {port} NO está abierto en el Firewall de Windows: las tablets no podrán activarse.\n"
              f"  Solución: doble clic en ABRIR_FIREWALL.bat (acepta el permiso de administrador) y vuelve a iniciar.")


def check_database(url: str) -> bool:
    """Comprueba la base antes de arrancar, con mensajes claros."""
    if url.startswith("sqlite"):
        print("⚠ Usando SQLite (base de prueba). Para PostgreSQL 'adipack_mdm' ejecuta CREAR_BASE_DATOS.bat.")
        return True
    from sqlalchemy import create_engine, text
    from urllib.parse import urlparse
    u = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 8})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        print(f"✓ Base de datos PostgreSQL '{u.path.lstrip('/')}' en {u.hostname}:{u.port or 5432} conectada.")
        return True
    except Exception as exc:  # noqa: BLE001 - mostramos el motivo exacto
        msg = str(exc).splitlines()[0]
        print(f"✗ No pude conectar a PostgreSQL ({u.hostname}:{u.port or 5432}/{u.path.lstrip('/')}).\n"
              f"  Motivo: {msg}\n"
              "  Revisa que el servicio 'postgresql' esté iniciado (services.msc) o vuelve a ejecutar CREAR_BASE_DATOS.bat.")
        return False


def main() -> int:
    ensure_env()
    from app.config import get_settings
    settings = get_settings()
    port = settings.port
    if not port_free(port):
        print(f"✗ El puerto {port} está ocupado: ¿el MDM ya está abierto en otra ventana?\n"
              f"  Ciérrala o cambia MDM_PORT en .env.")
        return 1
    if not check_database(settings.database_url):
        return 1
    open_firewall(port)
    if settings.mqtt_enabled:
        if not port_free(settings.mqtt_port):
            print(f"⚠ El puerto MQTT {settings.mqtt_port} está ocupado (¿Mosquitto u otro broker?). Los avisos a las "
                  f"tablets no funcionarán.\n  Detén ese programa o cambia MDM_MQTT_PORT en .env.")
        open_firewall(settings.mqtt_port)
    print("\n" + "=" * 64)
    print("  ADIPACK MDM ENCENDIDO")
    print(f"  Consola (en esta PC):        http://localhost:{port}")
    print(f"  Dirección para las tablets:  {settings.public_url}")
    if settings.mqtt_enabled:
        print(f"  Avisos MQTT (sin Internet):  puerto {settings.mqtt_port}")
    print("  No cierres esta ventana. Para apagar: Ctrl+C")
    print("=" * 64 + "\n")
    if settings.public_url.startswith(("http://127.", "http://localhost")):
        print("⚠ Esta PC no tiene IP de red. Conéctala a la red de planta y vuelve a iniciar.\n")
    threading.Timer(2.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
