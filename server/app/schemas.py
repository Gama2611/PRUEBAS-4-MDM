"""Esquemas validados: políticas por plataforma, comandos y API de agentes."""
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def _check_pkg(v: str) -> str:
    v = v.strip()
    if v and not PKG_RE.match(v):
        raise ValueError(f"Nombre de paquete Android inválido: {v!r}")
    return v


# ----------------------------------------------------------------- políticas
class AndroidPolicy(BaseModel):
    # --- Modo kiosco (lock task) ---
    kiosk_enabled: bool = True
    kiosk_package: str = ""  # p.ej. com.adipack.mes
    kiosk_extra_packages: list[str] = Field(default_factory=list)  # apps auxiliares permitidas
    kiosk_exit_pin: str = ""  # se envía al agente sólo como hash
    kiosk_show_home_button: bool = False
    kiosk_show_notifications: bool = False
    kiosk_show_system_info: bool = True  # hora/batería en la barra
    kiosk_allow_power_menu: bool = True

    # --- Restricciones ---
    disable_camera: bool = False
    disable_screenshots: bool = True
    disable_status_bar: bool = True
    disable_keyguard: bool = True  # sin pantalla de bloqueo en kiosco
    disable_factory_reset: bool = True
    disable_safe_boot: bool = True
    disable_usb_file_transfer: bool = True
    disable_debugging: bool = False  # True en producción (bloquea ADB)
    disable_install_unknown_sources: bool = True
    disable_add_user: bool = True
    disable_config_wifi: bool = False
    disable_bluetooth: bool = False
    stay_on_while_plugged: bool = True
    auto_grant_permissions: bool = True
    system_updates: Literal["automatic", "windowed", "postpone"] = "windowed"
    update_window_start_min: int = Field(120, ge=0, le=1439)  # 02:00
    update_window_end_min: int = Field(300, ge=0, le=1439)  # 05:00

    # --- Wi-Fi corporativo ---
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_security: Literal["WPA", "WEP", "NONE"] = "WPA"

    # --- Apps obligatorias (ids de la tabla apps) y configuración gestionada ---
    required_app_ids: list[int] = Field(default_factory=list)
    app_config: dict[str, Any] = Field(default_factory=dict)  # restricciones para kiosk_package

    sync_interval_sec: int = Field(60, ge=15, le=3600)

    @field_validator("kiosk_package")
    @classmethod
    def _pkg(cls, v: str) -> str:
        return _check_pkg(v)

    @field_validator("kiosk_extra_packages")
    @classmethod
    def _pkgs(cls, v: list[str]) -> list[str]:
        return [p for p in (_check_pkg(x) for x in v) if p]

    @field_validator("kiosk_exit_pin")
    @classmethod
    def _pin(cls, v: str) -> str:
        if v and not re.fullmatch(r"\d{4,12}", v):
            raise ValueError("El PIN de salida debe tener entre 4 y 12 dígitos")
        return v

    @model_validator(mode="after")
    def _kiosk_needs_pkg(self):
        if self.kiosk_enabled and not self.kiosk_package:
            raise ValueError("Con el modo kiosco activo debes indicar el paquete de la app (kiosk_package)")
        if self.wifi_ssid and self.wifi_security != "NONE" and len(self.wifi_password) < 8 and self.wifi_security == "WPA":
            raise ValueError("La clave WPA debe tener al menos 8 caracteres")
        return self


class PCPolicy(BaseModel):
    kiosk_enabled: bool = False
    kiosk_url: str = ""  # p.ej. http://mes.adipack.local
    kiosk_browser: Literal["edge", "chrome", "chromium", "firefox"] = "edge"
    allow_scripts: bool = False  # permite el comando run_script
    inventory_interval_min: int = Field(60, ge=5, le=1440)
    sync_interval_sec: int = Field(60, ge=15, le=3600)

    @model_validator(mode="after")
    def _kiosk_needs_url(self):
        if self.kiosk_enabled and not re.match(r"^https?://", self.kiosk_url):
            raise ValueError("Con kiosco activo indica una URL http(s):// válida")
        return self


POLICY_MODELS: dict[str, type[BaseModel]] = {
    "android": AndroidPolicy,
    "windows": PCPolicy,
    "linux": PCPolicy,
}

# ----------------------------------------------------------------- comandos
COMMANDS: dict[str, dict] = {
    # tipo: plataformas, si es destructivo, etiqueta
    "sync": {"platforms": {"android", "windows", "linux"}, "danger": False, "label": "Sincronizar ahora"},
    "lock": {"platforms": {"android", "windows", "linux"}, "danger": False, "label": "Bloquear pantalla"},
    "reboot": {"platforms": {"android", "windows", "linux"}, "danger": False, "label": "Reiniciar"},
    "message": {"platforms": {"android", "windows", "linux"}, "danger": False, "label": "Enviar mensaje"},
    "exit_kiosk": {"platforms": {"android", "windows", "linux"}, "danger": False, "label": "Salir del kiosco (temporal)"},
    "install_app": {"platforms": {"android"}, "danger": False, "label": "Instalar app"},
    "uninstall_app": {"platforms": {"android"}, "danger": False, "label": "Desinstalar app"},
    "clear_app_data": {"platforms": {"android"}, "danger": True, "label": "Borrar datos de app"},
    "collect_inventory": {"platforms": {"windows", "linux"}, "danger": False, "label": "Inventario completo"},
    "run_script": {"platforms": {"windows", "linux"}, "danger": True, "label": "Ejecutar script"},
    "shutdown": {"platforms": {"windows", "linux"}, "danger": True, "label": "Apagar"},
    "wipe": {"platforms": {"android"}, "danger": True, "label": "Borrado de fábrica"},
}


# ----------------------------------------------------------------- API agentes
class EnrollIn(BaseModel):
    enrollment_token: str = Field(min_length=8, max_length=64)
    platform: Literal["android", "windows", "linux"]
    hardware_id: str = Field(min_length=1, max_length=128)
    manufacturer: str = Field("", max_length=64)
    model: str = Field("", max_length=64)
    serial: str = Field("", max_length=64)
    os_version: str = Field("", max_length=64)
    agent_version: str = Field("", max_length=32)
    name: str = Field("", max_length=100)


class EnrollOut(BaseModel):
    device_id: int
    device_token: str
    name: str


class CheckinIn(BaseModel):
    agent_version: str = Field("", max_length=32)
    os_version: str = Field("", max_length=64)
    applied_policy_version: int = 0
    status: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def _limit(cls, v: dict) -> dict:
        import json
        if len(json.dumps(v, default=str)) > 256_000:
            raise ValueError("status demasiado grande")
        return v


class CommandOut(BaseModel):
    id: int
    type: str
    params: dict[str, Any]


class CheckinOut(BaseModel):
    device_id: int
    name: str
    policy_version: int
    policy: dict[str, Any] | None
    commands: list[CommandOut]
    server_time: str


class CommandResultIn(BaseModel):
    success: bool
    output: str = Field("", max_length=20_000)
