#!/usr/bin/env python3
"""ADIPACK MDM — agente para PC (Windows / Linux).

Uso:
    python agent.py enroll --server http://mdm.adipack.local:8000 --token TOKEN
    python agent.py run            # bucle: check-in, comandos, kiosco
    python agent.py status

Sólo usa la biblioteca estándar (psutil es opcional para métricas).
Debe correr en la sesión del usuario (tarea programada "al iniciar sesión") para poder abrir el kiosco.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

AGENT_VERSION = "1.0.0"
IS_WIN = sys.platform.startswith("win")
PLATFORM = "windows" if IS_WIN else "linux"

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

log = logging.getLogger("adipack-agent")


# ------------------------------------------------------------------ configuración
def default_config_path() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "AdipackMDM"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "adipack-mdm"
    return base / "agent.json"


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, k, default=None):
        return self.data.get(k, default)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        if not IS_WIN:
            os.chmod(tmp, 0o600)  # contiene el token del equipo
        os.replace(tmp, self.path)  # escritura atómica


# ------------------------------------------------------------------ HTTP
class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code


def post(server: str, path: str, body: dict, token: str = "", timeout: int = 30) -> dict:
    req = urllib.request.Request(server.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"AdipackMDM-PC/{AGENT_VERSION}"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise HttpError(e.code, str(detail)[:300]) from None


# ------------------------------------------------------------------ inventario
def run(cmd: list[str], timeout: int = 60) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def powershell(script: str, timeout: int = 60) -> str:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout)


def hardware_id() -> str:
    if IS_WIN:
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return "win-" + winreg.QueryValueEx(k, "MachineGuid")[0]
        except OSError:
            pass
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return "linux-" + Path(p).read_text().strip()
        except OSError:
            continue
    return "host-" + socket.gethostname()


def serial_number() -> str:
    if IS_WIN:
        return powershell("(Get-CimInstance Win32_BIOS).SerialNumber")[:64]
    try:
        return Path("/sys/class/dmi/id/product_serial").read_text().strip()[:64]
    except OSError:
        return ""


def manufacturer_model() -> tuple[str, str]:
    if IS_WIN:
        out = powershell("$c=Get-CimInstance Win32_ComputerSystem; \"$($c.Manufacturer)|$($c.Model)\"")
        if "|" in out:
            a, b = out.split("|", 1)
            return a.strip()[:64], b.strip()[:64]
        return "", ""
    def rd(p):
        try:
            return Path(p).read_text().strip()[:64]
        except OSError:
            return ""
    return rd("/sys/class/dmi/id/sys_vendor"), rd("/sys/class/dmi/id/product_name")


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no envía paquetes; sólo elige interfaz
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def quick_status() -> dict:
    st = {"hostname": socket.gethostname(), "user": getpass.getuser(), "ip": local_ip(),
          "os": f"{platform.system()} {platform.release()} {platform.version()}"[:120]}
    if psutil:
        st["cpu_pct"] = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        st["ram_used_pct"] = vm.percent
        st["ram_total_mb"] = vm.total // 1_048_576
        st["uptime_min"] = int((time.time() - psutil.boot_time()) / 60)
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if bat:
            st["battery"], st["charging"] = int(bat.percent), bool(bat.power_plugged)
    du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\" if IS_WIN else "/")
    st["disk_free_gb"] = round(du.free / 1e9, 1)
    st["disk_total_gb"] = round(du.total / 1e9, 1)
    return st


def full_inventory() -> dict:
    inv = quick_status()
    inv["cpu"] = platform.processor() or run(["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"]).strip()
    if IS_WIN:
        sw = powershell(
            "Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,"
            "HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* -ErrorAction SilentlyContinue"
            " | Where-Object DisplayName | Select-Object DisplayName,DisplayVersion | ConvertTo-Json -Compress", 120)
        try:
            items = json.loads(sw) if sw else []
            items = items if isinstance(items, list) else [items]
            inv["software"] = sorted({f"{i.get('DisplayName')} {i.get('DisplayVersion') or ''}".strip() for i in items})
        except ValueError:
            inv["software"] = []
    else:
        out = run(["dpkg-query", "-W", "-f=${Package} ${Version}\n"], 120) or run(["rpm", "-qa"], 120)
        inv["software"] = out.splitlines()[:3000]
    if psutil:
        inv["disks"] = [{"mount": p.mountpoint, "fs": p.fstype} for p in psutil.disk_partitions(all=False)]
        inv["nics"] = {n: [a.address for a in addrs if a.family == socket.AF_INET]
                       for n, addrs in psutil.net_if_addrs().items()}
    return inv


# ------------------------------------------------------------------ kiosco
BROWSERS_WIN = {
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    "chromium": [r"%LocalAppData%\Chromium\Application\chrome.exe"],
    "firefox": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe"],
}
BROWSERS_LINUX = {
    "edge": ["microsoft-edge", "microsoft-edge-stable"], "chrome": ["google-chrome", "google-chrome-stable"],
    "chromium": ["chromium", "chromium-browser"], "firefox": ["firefox"],
}


def find_browser(name: str) -> str | None:
    if IS_WIN:
        for c in BROWSERS_WIN.get(name, []):
            p = os.path.expandvars(c)
            if os.path.isfile(p):
                return p
        return None
    for c in BROWSERS_LINUX.get(name, []):
        if p := shutil.which(c):
            return p
    return None


class Kiosk:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.launches: list[float] = []

    def suspended(self) -> bool:
        return time.time() < float(self.cfg.get("kiosk_suspended_until", 0))

    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.active():
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def tick(self, policy: dict) -> None:
        if not policy.get("kiosk_enabled") or self.suspended():
            self.stop()
            return
        if self.active():
            return
        now = time.time()
        self.launches = [t for t in self.launches if now - t < 60]
        if len(self.launches) >= 5:
            return  # el navegador se cierra en bucle: esperar
        exe = find_browser(policy.get("kiosk_browser", "edge"))
        if not exe:
            log.error("Navegador %s no encontrado", policy.get("kiosk_browser"))
            return
        url = policy["kiosk_url"]
        # Perfil dedicado => proceso propio que no se "fusiona" con el navegador del usuario.
        profile = self.cfg.path.parent / "kiosk-profile"
        if policy.get("kiosk_browser") == "firefox":
            args = [exe, "--kiosk", "--new-instance", "--profile", str(profile), url]
        else:
            args = [exe, "--kiosk", url, f"--user-data-dir={profile}", "--no-first-run",
                    "--disable-session-crashed-bubble", "--noerrdialogs", "--disable-pinch", "--overscroll-history-navigation=0"]
            if policy.get("kiosk_browser") == "edge":
                args += ["--edge-kiosk-type=fullscreen"]
        profile.mkdir(parents=True, exist_ok=True)
        self.launches.append(now)
        log.info("Abriendo kiosco: %s", url)
        self.proc = subprocess.Popen(args)


# ------------------------------------------------------------------ comandos
def show_message(text: str) -> None:
    if IS_WIN:
        safe = text.replace("'", "''")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                          "Add-Type -AssemblyName PresentationFramework;"
                          f"[System.Windows.MessageBox]::Show('{safe}','ADIPACK MDM')"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif shutil.which("zenity"):
        subprocess.Popen(["zenity", "--info", "--title=ADIPACK MDM", f"--text={text}"])
    elif shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "ADIPACK MDM", text])
    else:
        raise RuntimeError("No hay zenity ni notify-send")


def lock_screen() -> None:
    if IS_WIN:
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=True)
    else:
        for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]):
            if shutil.which(cmd[0]) and subprocess.run(cmd).returncode == 0:
                return
        raise RuntimeError("No se pudo bloquear la sesión")


def power(action: str) -> None:
    if IS_WIN:
        subprocess.run(["shutdown", "/r" if action == "reboot" else "/s", "/t", "15", "/c", "ADIPACK MDM"], check=False)
    else:
        subprocess.run(["systemctl", "reboot" if action == "reboot" else "poweroff"], check=False)


def run_script(script: str) -> tuple[bool, str]:
    suffix = ".ps1" if IS_WIN else ".sh"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        cmd = (["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path]
               if IS_WIN else ["/bin/bash", path])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0, f"exit={r.returncode}\n{r.stdout[-8000:]}\n{r.stderr[-4000:]}"
    except subprocess.TimeoutExpired:
        return False, "Tiempo agotado (300 s)"
    finally:
        os.unlink(path)


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.kiosk = Kiosk(cfg)
        self.next_inventory = 0.0
        self.pending_inventory: dict | None = None

    @property
    def policy(self) -> dict:
        return self.cfg.get("policy") or {}

    def execute(self, cmd: dict) -> tuple[bool, str, object]:
        t, p = cmd["type"], cmd.get("params") or {}
        after = None
        if t == "sync":
            return True, "Sincronizado", None
        if t == "lock":
            lock_screen(); return True, "Sesión bloqueada", None
        if t in ("reboot", "shutdown"):
            return True, f"{t} en 15 s", (lambda: power(t))
        if t == "message":
            show_message(str(p.get("text", ""))); return True, "Mensaje mostrado", None
        if t == "exit_kiosk":
            minutes = max(1, min(int(p.get("minutes", 10)), 240))
            self.cfg.data["kiosk_suspended_until"] = time.time() + minutes * 60
            self.cfg.save()
            self.kiosk.stop()
            return True, f"Kiosco suspendido {minutes} min", None
        if t == "collect_inventory":
            self.pending_inventory = full_inventory()
            return True, f"Inventario: {len(self.pending_inventory.get('software', []))} programas", None
        if t == "run_script":
            if not self.policy.get("allow_scripts"):
                return False, "La política local no permite scripts", None
            ok, out = run_script(str(p.get("script", "")))
            return ok, out, None
        return False, f"Comando no soportado en {PLATFORM}: {t}", after

    def sync(self) -> None:
        server, token = self.cfg.get("server_url"), self.cfg.get("device_token")
        status = quick_status()
        status["kiosk_active"] = self.kiosk.active()
        status["kiosk_suspended"] = self.kiosk.suspended()
        if time.time() >= self.next_inventory or self.pending_inventory:
            inv = self.pending_inventory or full_inventory()
            status["inventory"] = inv
            status["apps"] = [{"package": s, "version_name": "", "version_code": 0} for s in inv.get("software", [])[:1500]]
            self.pending_inventory = None
            self.next_inventory = time.time() + 60 * int(self.policy.get("inventory_interval_min", 60))
        resp = post(server, "/api/v1/checkin", {"agent_version": AGENT_VERSION, "os_version": status["os"][:64],
                                                "applied_policy_version": int(self.cfg.get("applied_version", 0)),
                                                "status": status}, token)
        if resp.get("policy") is not None:
            self.cfg.data["policy"] = resp["policy"]
            self.cfg.data["applied_version"] = resp["policy"].get("version", 0)
            log.info("Política v%s aplicada", self.cfg.data["applied_version"])
        self.cfg.data["device_name"] = resp.get("name", "")
        self.cfg.save()
        for cmd in resp.get("commands", []):
            try:
                ok, out, after = self.execute(cmd)
            except Exception as exc:  # noqa: BLE001 - se reporta al servidor
                ok, out, after = False, f"{type(exc).__name__}: {exc}", None
            try:
                post(server, f"/api/v1/commands/{cmd['id']}/result", {"success": ok, "output": out[:19000]}, token)
            except Exception as exc:  # noqa: BLE001
                log.error("No se pudo reportar comando %s: %s", cmd.get("id"), exc)
            if after:
                after()  # reinicio/apagado DESPUÉS de reportar

    def loop(self) -> None:
        next_sync = 0.0
        while True:
            now = time.time()
            if now >= next_sync:
                try:
                    self.sync()
                except HttpError as exc:
                    log.error("Servidor: %s", exc)
                    if exc.code == 401:
                        log.error("Token rechazado: equipo retirado. Re-enrolar con 'agent.py enroll'.")
                except Exception as exc:  # noqa: BLE001 - red caída, etc.: reintentar
                    log.warning("Sin conexión con el MDM: %s", exc)
                next_sync = now + int(self.policy.get("sync_interval_sec", 60))
            try:
                self.kiosk.tick(self.policy)  # funciona aun sin red, con la última política
            except Exception as exc:  # noqa: BLE001
                log.error("Kiosco: %s", exc)
            time.sleep(3)


# ------------------------------------------------------------------ CLI
def cmd_enroll(args, cfg: Config) -> int:
    man, model = manufacturer_model()
    body = {"enrollment_token": args.token, "platform": PLATFORM, "hardware_id": hardware_id(),
            "manufacturer": man, "model": model, "serial": serial_number(), "os_version": platform.platform()[:64],
            "agent_version": AGENT_VERSION, "name": args.name or ""}
    resp = post(args.server, "/api/v1/enroll", body)
    cfg.data.update(server_url=args.server.rstrip("/"), device_token=resp["device_token"], device_name=resp["name"],
                    applied_version=0, policy={})
    cfg.save()
    print(f"Enrolado como {resp['name']} (id {resp['device_id']}). Config: {cfg.path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agente ADIPACK MDM para PC")
    ap.add_argument("--config", type=Path, default=default_config_path())
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("--server", required=True)
    e.add_argument("--token", required=True)
    e.add_argument("--name", default="")
    sub.add_parser("run")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(args.config)
    if args.cmd == "enroll":
        return cmd_enroll(args, cfg)
    if not cfg.get("device_token"):
        print("No enrolado. Ejecuta: agent.py enroll --server URL --token TOKEN", file=sys.stderr)
        return 2
    if args.cmd == "status":
        print(json.dumps({k: v for k, v in cfg.data.items() if k != "device_token"}, indent=2))
        return 0
    Agent(cfg).loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
