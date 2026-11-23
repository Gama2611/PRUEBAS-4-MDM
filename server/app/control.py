"""Control de equipos: listas del formato, datos de empresa, logo y utilidades.

Las listas son EXACTAMENTE las de las validaciones del Excel original, para que las fórmulas
CONTAR.SI de la hoja (Extrusión, Óptimo, ...) cuenten bien los datos exportados.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, time, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Asset, Device, Setting, Worker

AREAS = ["Extrusión", "Impresión", "Sellado"]                     # Control_Turnos D, Acta
AREA_SA = "S/A"                                                    # Sin asignar: sin área ni personal
AREAS_INV = AREAS + ["Mantenimiento", "Respaldo / TI", AREA_SA]    # Inventario F
AREAS_ACTA = AREAS + ["Mantenimiento"]                             # Acta (Mantenimiento sólo se marca si aplica)


def acta_area_options(selected: str | None) -> list[str]:
    """Casillas de «Área Asignada» del acta: las 3 del formato y, si el acta es de Mantenimiento, esa también."""
    return AREAS + ([selected] if selected and selected not in AREAS else [])
TURNOS = ["Turno 1 (Mañana)", "Turno 2 (Tarde)", "Turno 3 (Noche)"]
ESTADOS_DEV = ["Óptimo", "Con detalle leve", "Dañada", "Extraviada"]
ESTATUS_INV = ["Operativo", "En revisión / Mantenimiento", "Dañado / Fuera de servicio"]
ACCESORIOS = ["Cargador original", "Cable USB", "Forro protector", "Vidrio templado"]
CONDICIONES = ["Nuevo", "Óptimo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]

COMPANY_DEFAULTS = {
    "company_name": "INVERSIONES VENPACK, C.A.",
    "company_rif": "J-29582172-7",
    "company_short": "Venpack",
    "city": "Guarenas",
    "plant": "Planta Guarenas",
    "asset_prefix": "VP-TAB-",
    "handover_by_name": "",
    "handover_by_id_number": "",
    "handover_by_position": "",
}
COMPANY_LABELS = {
    "company_name": "Razón social", "company_rif": "RIF", "company_short": "Nombre corto (para «Código Activo …»)",
    "city": "Ciudad (para el acta: «En Guarenas, a los …»)", "plant": "Planta (subtítulos)",
    "asset_prefix": "Prefijo de códigos de activo",
    "handover_by_name": "Entregado por: nombre", "handover_by_id_number": "Entregado por: cédula",
    "handover_by_position": "Entregado por: cargo",
}

CEDULA_RE = re.compile(r"^([VEJPG])?-?(\d{5,10})$")
HORA_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ------------------------------------------------------------------ empresa
def company(db: Session) -> dict:
    data = dict(COMPANY_DEFAULTS)
    for s in db.scalars(select(Setting).where(Setting.key.in_(list(COMPANY_DEFAULTS)))):
        data[s.key] = s.value
    return data


def save_company(db: Session, values: dict) -> None:
    for k in COMPANY_DEFAULTS:
        if k in values:
            v = str(values[k]).strip()[:200]
            row = db.get(Setting, k)
            if row is None:
                db.add(Setting(key=k, value=v))
            else:
                row.value = v


# ------------------------------------------------------------------ logo
def branding_dir() -> Path:
    d = get_settings().data_dir / "branding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logo_path() -> Path | None:
    """Logo subido desde la consola; si no hay, el logo.png copiado a app/static."""
    up = branding_dir() / "logo.png"
    if up.exists():
        return up
    st = Path(__file__).parent / "static" / "logo.png"
    return st if st.exists() else None


class LogoError(ValueError):
    pass


def save_logo(data: bytes) -> None:
    """Valida la imagen, la normaliza a PNG (máx. 1200 px) y la guarda."""
    from PIL import Image, UnidentifiedImageError

    if not data:
        raise LogoError("El archivo está vacío.")
    if len(data) > 5 * 1024 * 1024:
        raise LogoError("El logo no debe superar 5 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise LogoError("No es una imagen válida. Usa PNG o JPG.") from exc
    if img.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
        raise LogoError("Formato no admitido. Usa PNG o JPG.")
    img = img.convert("RGBA")
    img.thumbnail((1200, 1200))
    tmp = branding_dir() / "logo.tmp.png"
    img.save(tmp, format="PNG", optimize=True)
    tmp.replace(branding_dir() / "logo.png")


def delete_logo() -> None:
    (branding_dir() / "logo.png").unlink(missing_ok=True)


# ------------------------------------------------------------------ fechas y validaciones
def local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(get_settings().time_zone))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def today() -> date:
    return local_now().date()


def current_shift(now: datetime | None = None) -> str:
    """Turno sugerido por la hora local: 06-14 mañana, 14-22 tarde, 22-06 noche (se puede cambiar)."""
    h = (now or local_now()).hour
    if 6 <= h < 14:
        return TURNOS[0]
    if 14 <= h < 22:
        return TURNOS[1]
    return TURNOS[2]


def norm_cedula(raw: str) -> str:
    """Normaliza la cédula: «v12345678», «V-12.345.678» → «V-12345678»; sólo dígitos se respetan."""
    s = re.sub(r"[\s.]", "", str(raw or "")).upper()
    m = CEDULA_RE.match(s)
    if not m:
        raise ValueError("Cédula inválida. Ejemplos válidos: V-12345678, E-81234567 o 12345678.")
    return f"{m.group(1)}-{m.group(2)}" if m.group(1) else m.group(2)


def parse_hora(raw: str, campo: str = "Hora") -> str:
    m = HORA_RE.match(str(raw or "").strip())
    if not m:
        raise ValueError(f"{campo} inválida. Usa el formato HH:MM (24 horas).")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_fecha(raw: str, campo: str = "Fecha") -> date:
    raw = str(raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{campo} inválida.")


def hora_obj(hhmm: str | None) -> time | None:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def hora_12(hhmm: str | None) -> str:
    """«14:05» → «02:05 PM» (como el formato del Excel)."""
    t = hora_obj(hhmm)
    if t is None:
        return ""
    return f"{(t.hour % 12) or 12:02d}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


# ------------------------------------------------------------------ inventario
def _fold(text: str) -> str:
    """Mayúsculas, sin acentos ni espacios repetidos: «Impresión» == «IMPRESION» == « impresión »."""
    import unicodedata

    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(t.upper().replace(" /", "/").replace("/ ", "/").split())


_SA_ALIASES = {"", "S/A", "SA", "S.A", "S.A.", "SIN ASIGNAR", "SIN AREA", "SIN AREA ASIGNADA", "N/A", "NA", "-", "--"}


def norm_area(raw) -> str | None:
    """Área del inventario en su forma oficial. Vacío o «S/A» → «S/A». None si no se reconoce."""
    f = _fold(raw)
    if f in _SA_ALIASES:
        return AREA_SA
    for a in AREAS_INV:
        if _fold(a) == f:
            return a
    return None


def area_excel(area: str | None) -> str:
    """Como se escribe en el Excel del inventario: en mayúsculas (IMPRESIÓN, MANTENIMIENTO, S/A)."""
    return (area or AREA_SA).upper()


def area_label(area: str | None) -> str:
    return "S/A · Sin asignar" if not area or area == AREA_SA else area


def next_asset_code(db: Session, prefix: str) -> str:
    n = 0
    for code in db.scalars(select(Asset.code).where(Asset.code.like(f"{prefix}%"))):
        tail = code[len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return f"{prefix}{n + 1:03d}"


def software_label(dev: Device | None, db: Session) -> str:
    """«ADIPACK MDM + Adipack CMMS» según la política del equipo."""
    if dev is None or dev.policy is None:
        return "ADIPACK MDM"
    pkg = (dev.policy.settings or {}).get("kiosk_package", "")
    if not pkg:
        return "ADIPACK MDM"
    from .models import App
    app = db.scalar(select(App).where(App.package_name == pkg).order_by(App.version_code.desc()))
    return f"ADIPACK MDM + {app.label if app else pkg}"[:120]


def area_from_location(location: str) -> str:
    loc = _fold(location)
    for a in AREAS_INV:
        if a != AREA_SA and _fold(a) in loc:
            return a
    return AREA_SA


def import_mdm_devices(db: Session) -> list[Asset]:
    """Crea en el inventario las tablets del MDM que aún no tienen ficha de inventario."""
    prefix = company(db)["asset_prefix"]
    linked = set(db.scalars(select(Asset.device_id).where(Asset.device_id.is_not(None))))
    created = []
    devices = db.scalars(select(Device).where(Device.retired.is_(False), Device.platform == "android")
                         .order_by(Device.name)).all()
    for dev in devices:
        if dev.id in linked:
            continue
        a = Asset(code=next_asset_code(db, prefix), device_id=dev.id, area=area_from_location(dev.location),
                  software_serial=(dev.serial or "")[:64], brand=(dev.manufacturer or "")[:64],
                  model=(dev.model or "")[:64], software=software_label(dev, db), status="Operativo",
                  accessories="", notes=f"Equipo MDM {dev.name}")
        db.add(a)
        db.flush()
        created.append(a)
    return created


def worker_by_cedula(db: Session, cedula: str) -> Worker | None:
    return db.scalar(select(Worker).where(Worker.id_number == cedula))


def find_mdm_device(db: Session, software_serial: str, notes: str = "") -> Device | None:
    """Equipo del MDM que corresponde a una fila del inventario: por «Serial del Software» o por
    «Equipo MDM TAB-01» en observaciones. Sólo equipos activos y que no estén enlazados a otra ficha."""
    linked = set(db.scalars(select(Asset.device_id).where(Asset.device_id.is_not(None))))
    if software_serial:
        dev = db.scalar(select(Device).where(Device.retired.is_(False),
                                             func.lower(Device.serial) == software_serial.strip().lower()))
        if dev and dev.id not in linked:
            return dev
    m = re.search(r"EQUIPO\s+MDM\s+([A-Z0-9][A-Z0-9_-]*)", _fold(notes))
    if m:
        dev = db.scalar(select(Device).where(Device.retired.is_(False), func.upper(Device.name) == m.group(1)))
        if dev and dev.id not in linked:
            return dev
    return None
