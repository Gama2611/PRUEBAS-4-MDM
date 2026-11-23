"""Diagnóstico de red para la activación por QR: IPs de esta PC, Firewall de Windows y URL pública.

Todo en lenguaje simple: estas funciones alimentan la «Lista de verificación» de la consola.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

from .config import detect_lan_ip, get_settings

# Rangos típicos de adaptadores virtuales (WSL, Hyper-V, VirtualBox, Docker) que las tablets nunca alcanzan.
_VIRTUAL_HINTS = ("172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                  "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "192.168.56.")


def local_ipv4s() -> list[dict]:
    """IPs IPv4 privadas de esta PC. Usa psutil si está (da el nombre del adaptador); si no, socket."""
    found: dict[str, str] = {}
    try:
        import psutil  # opcional

        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET:
                    found.setdefault(a.address, name)
    except Exception:  # noqa: BLE001
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.setdefault(info[4][0], "")
        except OSError:
            pass
    preferred = detect_lan_ip()
    if preferred and preferred not in found:
        found[preferred] = ""
    out = []
    for ip, name in found.items():
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or not addr.is_private:
            continue
        low = name.lower()
        virtual = ip.startswith(_VIRTUAL_HINTS) or any(
            k in low for k in ("vethernet", "wsl", "virtualbox", "vmware", "docker", "hyper-v", "loopback"))
        out.append({"ip": ip, "name": name, "virtual": virtual, "preferred": ip == preferred,
                    "url": f"http://{ip}:{get_settings().port}"})
    out.sort(key=lambda d: (d["virtual"], not d["preferred"], d["ip"]))
    return out


def current_host() -> str:
    return urlparse(get_settings().public_url).hostname or ""


_fw_cache: dict[int, tuple[float, bool | None]] = {}


def firewall_rule_ok(port: int) -> bool | None:
    """True/False si existe la regla de entrada para el puerto; None si no es Windows o no se pudo saber."""
    if not sys.platform.startswith("win"):
        return None
    cached = _fw_cache.get(port)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    value = None
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name=ADIPACK MDM {port}"],
                           capture_output=True, text=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = r.returncode == 0 and f"ADIPACK MDM {port}" in (r.stdout or "")
    except Exception:  # noqa: BLE001
        value = None
    _fw_cache[port] = (time.monotonic(), value)
    return value


def set_public_url(url: str) -> str:
    """Valida, guarda en .env (MDM_PUBLIC_URL) y aplica en caliente la dirección que va dentro del QR."""
    url = (url or "").strip().rstrip("/")
    if url.lower() == "auto":
        try:
            from envfile import set_env_value

            set_env_value("MDM_PUBLIC_URL", "auto")
        except ImportError:  # pragma: no cover
            pass
        resolved = f"http://{detect_lan_ip()}:{get_settings().port}"
        get_settings().public_url = resolved
        return resolved
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("La dirección debe ser como http://192.168.1.50:8000")
    if u.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        raise ValueError("Esa es una dirección local de esta PC: las tablets no pueden llegar a ella.")
    if u.path not in ("", "/") or u.query:
        raise ValueError("Sólo protocolo, IP o nombre y puerto, sin rutas. Ejemplo: http://192.168.1.50:8000")
    try:
        from envfile import set_env_value  # server/envfile.py

        set_env_value("MDM_PUBLIC_URL", url)
    except ImportError:  # pragma: no cover - ejecución fuera de la carpeta server
        pass
    get_settings().public_url = url
    return url
