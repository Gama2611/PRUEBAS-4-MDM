"""Configuración central del servidor MDM (variables de entorno con prefijo MDM_)."""
import socket
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def detect_lan_ip() -> str:
    """IP de esta PC en la red local (la que usarán las tablets). No envía paquetes."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MDM_", env_file=".env", extra="ignore")

    # URL con la que tablets y PCs llegan al servidor. "auto" = http://<IP de esta PC>:<port>
    public_url: str = "auto"
    port: int = 8000
    # SQLite por defecto; en producción: postgresql+psycopg://user:pass@host/mdm
    database_url: str = "sqlite:///./data/mdm.db"
    data_dir: Path = Path("./data")
    # OBLIGATORIO cambiarla en producción (firma las cookies de sesión). iniciar_mdm.py la genera sola.
    secret_key: str = "CAMBIAR-ESTA-CLAVE"
    initial_admin_user: str = "admin"
    initial_admin_password: str = "CambiarYa!2026"
    # Zona horaria que se fija en la tablet al activarla con el QR.
    time_zone: str = "America/Caracas"
    # Segundos sin check-in para considerar un equipo "desconectado".
    offline_after_seconds: int = 300
    # Cookies "Secure" (activar cuando el servidor esté detrás de HTTPS).
    secure_cookies: bool = False
    max_upload_mb: int = 300
    # Avisos por MQTT (broker integrado, red local sin Internet)
    mqtt_enabled: bool = True
    mqtt_port: int = 1883
    mqtt_bind: str = "0.0.0.0"

    @model_validator(mode="after")
    def _resolve_url(self):
        if self.public_url.strip().lower() in ("", "auto"):
            self.public_url = f"http://{detect_lan_ip()}:{self.port}"
        self.public_url = self.public_url.rstrip("/")
        if not self.database_url.strip():  # .env recién copiado, antes de CREAR_BASE_DATOS.bat
            self.database_url = "sqlite:///./data/mdm.db"
        return self

    @property
    def apk_dir(self) -> Path:
        return self.data_dir / "apks"


@lru_cache
def get_settings() -> Settings:
    return Settings()
