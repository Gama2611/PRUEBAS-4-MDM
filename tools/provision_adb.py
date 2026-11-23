#!/usr/bin/env python3
"""Aprovisiona tablets Android por USB (plan B si el QR falla en Xiaomi/HyperOS).

    python tools/provision_adb.py --server http://192.168.1.10:8000 --token TOKEN \
        --agent adipack-mdm-agent.apk --app mes.apk --all

Requisitos en CADA tablet (recién restablecida, sin cuentas):
  1. Ajustes > Acerca de la tablet > tocar 7 veces "Versión de HyperOS" (Opciones de desarrollador).
  2. Opciones de desarrollador: activar "Depuración USB" e "Instalar vía USB".
     (Xiaomi pide iniciar sesión con cuenta Mi para "Instalar vía USB": hazlo, actívalo y
      LUEGO CIERRA SESIÓN y quita la cuenta; Device Owner exige 0 cuentas.)
  3. Aceptar la huella RSA del PC al conectar el cable.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

AGENT_PKG = "com.adipack.mdm.agent"
ADMIN = f"{AGENT_PKG}/.AdminReceiver"


def adb(serial: str | None, *args: str, timeout: int = 120) -> tuple[int, str]:
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"Tiempo agotado: {' '.join(cmd)}"
    return r.returncode, (r.stdout + r.stderr).strip()


def devices() -> list[str]:
    _, out = adb(None, "devices")
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]


def provision(serial: str, a) -> bool:
    print(f"\n=== {serial} ===")
    _, model = adb(serial, "shell", "getprop", "ro.product.model")
    _, android = adb(serial, "shell", "getprop", "ro.build.version.release")
    print(f"Modelo: {model}  Android {android}")

    _, owners = adb(serial, "shell", "dpm", "list-owners")
    already = AGENT_PKG in owners
    if not already:
        _, acc = adb(serial, "shell", "dumpsys", "account")
        m = re.search(r"Accounts:\s*(\d+)", acc)
        if m and int(m.group(1)) > 0:
            print(f"✗ La tablet tiene {m.group(1)} cuenta(s) (Google/Mi). Quítalas o restablece de fábrica.")
            return False
        if "Device Owner" in owners or "DeviceOwner" in owners:
            print(f"✗ Ya hay otro Device Owner:\n{owners}")
            return False

    print("→ Instalando agente…")
    rc, out = adb(serial, "install", "-r", "-g", a.agent, timeout=300)
    if rc != 0 or "Success" not in out:
        print(f"✗ No se instaló el agente: {out}\n  (¿'Instalar vía USB' activo en opciones de desarrollador?)")
        return False

    if not already:
        print("→ Asignando Device Owner…")
        rc, out = adb(serial, "shell", "dpm", "set-device-owner", ADMIN)
        if rc != 0 or "Success" not in out:
            print(f"✗ set-device-owner falló: {out}")
            return False
    else:
        print("✓ El agente ya era Device Owner")

    if a.app:
        print("→ Instalando app MES/CMMS…")
        rc, out = adb(serial, "install", "-r", "-g", a.app, timeout=300)
        print(("✓ " if "Success" in out else "⚠ ") + out.splitlines()[-1] if out else "")

    print("→ Configurando servidor y token…")
    rc, out = adb(serial, "shell", "am", "start", "-n", f"{AGENT_PKG}/.SetupActivity",
                  "--es", "server_url", a.server, "--es", "enrollment_token", a.token)
    if rc != 0 or "Error" in out:
        print(f"✗ {out}")
        return False
    print("✓ Listo. La tablet se enrolará y entrará en kiosco en ~1 minuto. Revisa la consola.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, help="URL del MDM, p.ej. http://192.168.1.10:8000")
    ap.add_argument("--token", required=True, help="Token de enrolamiento (pantalla Enrolar)")
    ap.add_argument("--agent", required=True, help="APK firmado del agente")
    ap.add_argument("--app", help="APK del MES/CMMS (opcional, recomendado en Xiaomi)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--serial", help="Serial ADB de una tablet concreta")
    g.add_argument("--all", action="store_true", help="Todas las tablets conectadas")
    a = ap.parse_args()
    if not shutil.which("adb"):
        print("adb no está en el PATH (instala Android Platform Tools)")
        return 2
    if not re.match(r"^https?://", a.server):
        print("--server debe empezar por http:// o https://")
        return 2
    targets = [a.serial] if a.serial else devices()
    if not a.all and not a.serial and len(targets) > 1:
        print(f"Hay {len(targets)} tablets conectadas; usa --all o --serial")
        return 2
    if not targets:
        print("No hay tablets conectadas/autorizadas (adb devices)")
        return 2
    results = {s: provision(s, a) for s in targets}
    print("\nResumen:", ", ".join(f"{s}={'OK' if ok else 'FALLÓ'}" for s, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
