"""Lectura/escritura segura de server/.env (compartido por los scripts de arranque)."""
from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
EXAMPLE = HERE / ".env.example"
SECRET_PLACEHOLDERS = {"", "CAMBIAR-ESTA-CLAVE", "pon-aqui-una-clave-larga-y-aleatoria"}


def read_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolera el BOM del Bloc de notas
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":  # tolera valores entre comillas
                    v = v[1:-1]
                data[k.strip()] = v
    return data


def set_env_value(key: str, value: str) -> None:
    """Cambia (o agrega) una variable de .env sin tocar las demás líneas ni comentarios."""
    lines = ENV.read_text(encoding="utf-8-sig").splitlines() if ENV.exists() else []
    out, done = [], False
    for line in lines:
        s = line.strip()
        if not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = ENV.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV)
    if os.name != "nt":
        os.chmod(ENV, 0o600)  # contiene contraseñas


def ensure_env() -> bool:
    """Crea .env desde .env.example si no existe y pone una clave secreta aleatoria. True si lo creó."""
    created = False
    if not ENV.exists():
        if EXAMPLE.exists():
            shutil.copyfile(EXAMPLE, ENV)
        else:
            ENV.write_text("MDM_PUBLIC_URL=auto\nMDM_PORT=8000\n", encoding="utf-8")
        created = True
        print("✓ Archivo de configuración server\\.env creado a partir de .env.example.")
    if read_env().get("MDM_SECRET_KEY", "") in SECRET_PLACEHOLDERS:
        set_env_value("MDM_SECRET_KEY", secrets.token_urlsafe(48))
    return created
