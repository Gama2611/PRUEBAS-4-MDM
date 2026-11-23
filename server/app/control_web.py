"""Pantallas de Control de equipos: bitácora de turnos, inventario, personal, actas y empresa/logo."""
from __future__ import annotations

import io
import warnings
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import excel_formats as xf
from .control import (ACCESORIOS, AREA_SA, AREAS, AREAS_ACTA, AREAS_INV, acta_area_options, COMPANY_DEFAULTS, COMPANY_LABELS, CONDICIONES, ESTADOS_DEV,
                      ESTATUS_INV, TURNOS, LogoError, _fold, area_label, company, current_shift, delete_logo,
                      find_mdm_device, hora_12, import_mdm_devices, local_now, logo_path, next_asset_code,
                      norm_area, norm_cedula, parse_fecha, parse_hora, save_company, save_logo, today,
                      worker_by_cedula)
from .db import get_db
from .models import Asset, Device, HandoverAct, ShiftLog, Worker
from .services import audit
from .web import back, check_csrf, flash, render, require_admin, templates

router = APIRouter()
templates.env.globals.update(AREAS=AREAS, AREAS_INV=AREAS_INV, AREAS_ACTA=AREAS_ACTA, acta_area_options=acta_area_options, AREA_SA=AREA_SA, area_label=area_label, TURNOS=TURNOS, ESTADOS_DEV=ESTADOS_DEV,
                             ESTATUS_INV=ESTATUS_INV, ACCESORIOS=ACCESORIOS, CONDICIONES=CONDICIONES)
templates.env.filters["hora12"] = hora_12
MAX_IMPORT_MB = 10


def _f(form, key: str, maxlen: int = 255) -> str:
    return str(form.get(key, "") or "").strip()[:maxlen]


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(data, media_type=xf.XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


# ============================================================ logo y empresa
@router.get("/branding/logo.png")
def branding_logo():
    """Logo de la empresa (público: también se muestra en la pantalla de inicio de sesión)."""
    p = logo_path()
    if p is None:
        raise HTTPException(404, "Sin logo")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.get("/empresa")
def empresa_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "empresa.html", comp=company(db), labels=COMPANY_LABELS, has_logo=logo_path() is not None,
                  stamp=int(datetime.now().timestamp()))


@router.post("/empresa", dependencies=[Depends(check_csrf)])
async def empresa_save(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    values = {k: _f(form, k, 200) for k in COMPANY_DEFAULTS}
    for k in ("company_name", "company_rif", "city", "plant", "asset_prefix"):
        if not values[k]:
            flash(request, f"«{COMPANY_LABELS[k]}» es obligatorio.", "err")
            return back("/empresa")
    if values["handover_by_id_number"]:
        try:
            values["handover_by_id_number"] = norm_cedula(values["handover_by_id_number"])
        except ValueError as exc:
            flash(request, f"Entregado por: {exc}", "err")
            return back("/empresa")
    save_company(db, values)
    audit(db, user, "empresa_save", values["company_name"])
    db.commit()
    flash(request, "Datos de la empresa guardados. Se usan en la bitácora, el inventario y las actas.")
    return back("/empresa")


@router.post("/empresa/logo", dependencies=[Depends(check_csrf)])
async def empresa_logo(request: Request, file: UploadFile, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        save_logo(await file.read())
    except LogoError as exc:
        flash(request, str(exc), "err")
        return back("/empresa")
    finally:
        await file.close()
    audit(db, user, "logo_upload", file.filename or "")
    db.commit()
    flash(request, "Logo actualizado. Aparece en la consola, en las actas impresas y en los Excel exportados.")
    return back("/empresa")


@router.post("/empresa/logo/borrar", dependencies=[Depends(check_csrf)])
def empresa_logo_delete(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    delete_logo()
    audit(db, user, "logo_delete")
    db.commit()
    flash(request, "Logo eliminado.")
    return back("/empresa")


# ============================================================ personal
@router.get("/personal")
def personal_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db),
                  q: str = "", edit: int = 0):
    stmt = select(Worker).order_by(Worker.active.desc(), Worker.full_name)
    workers = list(db.scalars(stmt))
    if q:
        ql = q.lower()
        workers = [w for w in workers if ql in f"{w.full_name} {w.id_number} {w.position} {w.area}".lower()]
    return render(request, "personal.html", workers=workers, q=q, w=db.get(Worker, edit) if edit else None)


@router.post("/personal/save", dependencies=[Depends(check_csrf)])
async def personal_save(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    wid = _f(form, "id")
    name = _f(form, "full_name", 120)
    if len(name) < 3:
        flash(request, "Escribe el nombre y apellido.", "err")
        return back(f"/personal?edit={wid}" if wid else "/personal")
    try:
        ced = norm_cedula(_f(form, "id_number", 30))
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(f"/personal?edit={wid}" if wid else "/personal")
    w = db.get(Worker, int(wid)) if wid.isdigit() else None
    dup = worker_by_cedula(db, ced)
    if dup and (w is None or dup.id != w.id):
        flash(request, f"Ya existe una persona con la cédula {ced}: {dup.full_name}.", "err")
        return back(f"/personal?edit={wid}" if wid else "/personal")
    if w is None:
        w = Worker(full_name=name, id_number=ced)
        db.add(w)
    w.full_name, w.id_number = name, ced
    w.position = _f(form, "position", 80)
    w.shift = _f(form, "shift", 40) if _f(form, "shift") in TURNOS else ""
    w.area = _f(form, "area", 40) if _f(form, "area") in AREAS_INV else ""
    db.flush()
    audit(db, user, "worker_save", f"{w.full_name} {w.id_number}")
    db.commit()
    flash(request, f"{w.full_name} guardado.")
    return back("/personal")


@router.post("/personal/{worker_id}/toggle", dependencies=[Depends(check_csrf)])
def personal_toggle(worker_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    w = db.get(Worker, worker_id)
    if w:
        w.active = not w.active
        audit(db, user, "worker_toggle", f"{w.full_name} activo={w.active}")
        db.commit()
        flash(request, f"{w.full_name} {'activado' if w.active else 'desactivado (ya no aparece en las listas)'}.")
    return back("/personal")


# ============================================================ inventario
def _assets(db: Session) -> list[Asset]:
    return list(db.scalars(select(Asset).order_by(Asset.code)))


def _out_now(db: Session) -> dict[int, ShiftLog]:
    """Equipos entregados y aún no devueltos (por asset_id)."""
    return {lg.asset_id: lg for lg in db.scalars(select(ShiftLog).where(ShiftLog.returned_at.is_(None),
                                                                        ShiftLog.asset_id.is_not(None)))}


@router.get("/inventario")
def inventario_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    assets = _assets(db)
    stats = {"total": len(assets), **{s: sum(a.status == s for a in assets) for s in ESTATUS_INV}}
    unlinked = db.scalar(select(func.count(Device.id)).where(
        Device.platform == "android", Device.retired.is_(False),
        Device.id.not_in(select(Asset.device_id).where(Asset.device_id.is_not(None))))) or 0
    holders: dict[int, HandoverAct] = {}
    for act in db.scalars(select(HandoverAct).where(HandoverAct.asset_id.is_not(None))
                          .order_by(HandoverAct.date, HandoverAct.id)):
        holders[act.asset_id] = act  # la última acta de cada equipo
    return render(request, "inventario.html", assets=assets, stats=stats, out=_out_now(db), unlinked=unlinked,
                  holders=holders)


def _asset_form(request: Request, db: Session, a: Asset | None):
    linked = set(db.scalars(select(Asset.device_id).where(Asset.device_id.is_not(None))))
    devices = [d for d in db.scalars(select(Device).where(Device.retired.is_(False)).order_by(Device.name))
               if d.id not in linked or (a and d.id == a.device_id)]
    comp = company(db)
    return render(request, "inventario_form.html", a=a, devices=devices,
                  suggested=next_asset_code(db, comp["asset_prefix"]), comp=comp)


@router.get("/inventario/nuevo")
def inventario_new(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    return _asset_form(request, db, None)


@router.get("/inventario/export.xlsx")
def inventario_export(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    data = xf.build_inventory(_assets(db), company(db), logo_path())
    audit(db, user, "export_inventario", "")
    db.commit()
    return _xlsx(data, f"Inventario_Tablets_{today():%Y-%m-%d}.xlsx")


@router.get("/inventario/{asset_id}")
def inventario_edit(asset_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "Ese equipo no está en el inventario.")
    return _asset_form(request, db, a)


@router.post("/inventario/save", dependencies=[Depends(check_csrf)])
async def inventario_save(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    aid = _f(form, "id")
    retry = f"/inventario/{aid}" if aid else "/inventario/nuevo"
    code = _f(form, "code", 40).upper()
    if not code:
        flash(request, "El código de activo es obligatorio.", "err")
        return back(retry)
    a = db.get(Asset, int(aid)) if aid.isdigit() else None
    dup = db.scalar(select(Asset).where(Asset.code == code))
    if dup and (a is None or dup.id != a.id):
        flash(request, f"El código {code} ya existe en el inventario.", "err")
        return back(retry)
    status = _f(form, "status")
    area = norm_area(_f(form, "area"))
    if status not in ESTATUS_INV:
        flash(request, "Elige el estatus del equipo.", "err")
        return back(retry)
    if area is None:
        flash(request, "Área inválida.", "err")
        return back(retry)
    dev_id = _f(form, "device_id")
    device_id = int(dev_id) if dev_id.isdigit() else None
    if device_id:
        other = db.scalar(select(Asset).where(Asset.device_id == device_id))
        if other and (a is None or other.id != a.id):
            flash(request, f"Ese equipo del MDM ya está enlazado a {other.code}.", "err")
            return back(retry)
    if a is None:
        a = Asset(code=code)
        db.add(a)
    a.code, a.device_id, a.area, a.status = code, device_id, area, status
    a.brand, a.model, a.serial = _f(form, "brand", 64), _f(form, "model", 64), _f(form, "serial", 64)
    a.software_serial = _f(form, "software_serial", 64)
    a.software, a.notes = _f(form, "software", 120), _f(form, "notes", 2000)
    a.accessories = _f(form, "accessories", 255)
    db.flush()
    audit(db, user, "asset_save", f"{a.code} {a.status}")
    db.commit()
    flash(request, f"Equipo {a.code} guardado.")
    return back("/inventario")


@router.post("/inventario/{asset_id}/delete", dependencies=[Depends(check_csrf)])
def inventario_delete(asset_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Asset, asset_id)
    if a:
        if db.scalar(select(ShiftLog).where(ShiftLog.asset_id == a.id, ShiftLog.returned_at.is_(None))):
            flash(request, f"{a.code} está entregado ahora mismo. Registra la devolución antes de eliminarlo.", "err")
            return back(f"/inventario/{asset_id}")
        audit(db, user, "asset_delete", a.code)
        db.delete(a)  # la bitácora y las actas conservan su copia del código y serial
        db.commit()
        flash(request, "Equipo eliminado del inventario. Su historial en la bitácora se conserva.")
    return back("/inventario")


@router.post("/inventario/importar-mdm", dependencies=[Depends(check_csrf)])
def inventario_import_mdm(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    created = import_mdm_devices(db)
    audit(db, user, "asset_import_mdm", f"{len(created)} equipos")
    db.commit()
    flash(request, f"{len(created)} tablet(s) del MDM agregadas al inventario: {', '.join(a.code for a in created)}."
          if created else "Todas las tablets del MDM ya están en el inventario.")
    return back("/inventario")


def _cell_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return parse_fecha(v.strip())
        except ValueError:
            return None
    return None


def _cell_time(v) -> str | None:
    if v is None or v == "":
        return None
    if hasattr(v, "hour") and hasattr(v, "minute"):
        return f"{v.hour:02d}:{v.minute:02d}"
    if isinstance(v, float) and 0 <= v < 1:  # hora guardada como fracción del día
        mins = round(v * 24 * 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"
    s = str(v).strip().upper().replace(".", "")
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
            return f"{t.hour:02d}:{t.minute:02d}"
        except ValueError:
            continue
    return None


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # un serial numérico no debe quedar como «12345.0»
    return str(v).strip()


# Encabezados reconocidos en la hoja Inventario_Tablets (formato anterior de 9 columnas y el actual de 10).
def _inv_key(header: str) -> str | None:
    h = _fold(header)
    if not h:
        return None
    if h.startswith("CODIGO"):
        return "code"
    if h.startswith("MARCA"):
        return "brand"
    if h.startswith("MODELO"):
        return "model"
    if h.startswith("SERIAL"):
        return "software_serial" if "SOFTWARE" in h else "serial"
    if h.startswith("AREA"):
        return "area"
    if h.startswith("SOFTWARE"):
        return "software"
    if h.startswith("ACCESORIO"):
        return "accessories"
    if h.startswith("ESTATUS") or h.startswith("ESTADO"):
        return "status"
    if h.startswith("OBSERVACION"):
        return "notes"
    return None


def _inventory_header(rows) -> tuple[int | None, dict[str, int]]:
    """Busca la fila de encabezados (la que empieza con «Código…») y devuelve {campo: índice de columna}."""
    for idx, row in enumerate(rows[:20]):
        if row and _fold(_txt(row[0])).startswith("CODIGO"):
            cols: dict[str, int] = {}
            for i, v in enumerate(row):
                key = _inv_key(_txt(v))
                if key and key not in cols:
                    cols[key] = i
            if {"code", "area", "status"} <= cols.keys():
                return idx, cols
    return None, {}


@router.post("/inventario/importar-excel", dependencies=[Depends(check_csrf)])
async def inventario_import_excel(request: Request, file: UploadFile, user: str = Depends(require_admin),
                                  db: Session = Depends(get_db)):
    """Importa el Excel «Bitácora Control Turnos Tablets» que ya se venía llenando a mano."""
    import openpyxl

    data = await file.read(MAX_IMPORT_MB * 1024 * 1024 + 1)
    await file.close()
    if len(data) > MAX_IMPORT_MB * 1024 * 1024:
        flash(request, f"El archivo supera {MAX_IMPORT_MB} MB.", "err")
        return back("/inventario")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 - archivo dañado o que no es Excel
        flash(request, "No se pudo leer el archivo. Debe ser el Excel .xlsx de la bitácora.", "err")
        return back("/inventario")
    if xf.INV not in wb.sheetnames and xf.CTRL not in wb.sheetnames:
        flash(request, f"El archivo no tiene las hojas {xf.INV} ni {xf.CTRL}.", "err")
        return back("/inventario")
    n_new = n_upd = n_logs = n_skip = n_link = 0
    warns: list[str] = []
    if xf.INV in wb.sheetnames:
        inv_rows = list(wb[xf.INV].iter_rows(max_col=30, values_only=True))
        hdr_idx, cols = _inventory_header(inv_rows)
        if hdr_idx is None:
            warns.append(f"no encontré los encabezados de la hoja {xf.INV} (la fila con «Código Activo», «Área "
                         "Asignada», «Estatus Actual»…), por eso no se importó el inventario")
            inv_rows = []
        for row in inv_rows[(hdr_idx or 0) + 1:]:
            def cell(key):
                i = cols.get(key)
                return _txt(row[i]) if i is not None and i < len(row) else ""
            code = cell("code").upper()[:40]
            if not code or code.startswith("="):
                continue
            a = db.scalar(select(Asset).where(Asset.code == code))
            if a is None:
                a = Asset(code=code, status="Operativo", area=AREA_SA)
                db.add(a)
                n_new += 1
            else:
                n_upd += 1
            a.brand = cell("brand")[:64] or a.brand
            a.model = cell("model")[:64] or a.model
            a.serial = cell("serial")[:64] or a.serial
            if "software_serial" in cols:
                a.software_serial = cell("software_serial")[:64] or a.software_serial
            if "area" in cols:
                area = norm_area(cell("area"))
                if area is None:
                    warns.append(f"{code}: área «{cell('area')}» no reconocida, quedó S/A")
                    area = AREA_SA
                a.area = area
            a.software = cell("software")[:120] or a.software
            a.accessories = cell("accessories")[:255] or a.accessories
            if cell("status"):
                st = next((x for x in ESTATUS_INV if _fold(x) == _fold(cell("status"))), None)
                if st is None:
                    warns.append(f"{code}: estatus «{cell('status')}» no reconocido, se dejó «{a.status or 'Operativo'}»")
                a.status = st or a.status or "Operativo"
            a.notes = cell("notes")[:2000] or a.notes
            db.flush()
            if a.device_id is None:
                dev = find_mdm_device(db, a.software_serial, a.notes)
                if dev is not None:
                    a.device_id = dev.id
                    db.flush()
                    n_link += 1
    if xf.CTRL in wb.sheetnames:
        for row in wb[xf.CTRL].iter_rows(min_row=xf.CTRL_FIRST, max_col=13, values_only=True):
            row = tuple(row) + (None,) * (13 - len(row))
            d, code, name = _cell_date(row[0]), _txt(row[1]).upper()[:40], _txt(row[5])[:120]
            delivered = _cell_time(row[7])
            if not (d and code and name and delivered):
                if any(c not in (None, "") for c in row[:2] + row[3:]):
                    n_skip += 1
                continue
            exists = db.scalar(select(ShiftLog).where(ShiftLog.date == d, ShiftLog.asset_code == code,
                                                      ShiftLog.operator_name == name, ShiftLog.delivered_at == delivered))
            if exists:
                continue
            asset = db.scalar(select(Asset).where(Asset.code == code))
            ced = ""
            try:
                ced = norm_cedula(_txt(row[6])) if _txt(row[6]) else ""
            except ValueError:
                ced = _txt(row[6])[:20]
            serial = _txt(row[2])
            if serial in ("No registrado",) or serial.startswith("="):
                serial = ""
            db.add(ShiftLog(date=d, asset_id=asset.id if asset else None, asset_code=code,
                            serial=serial[:64] or (asset.eff_serial if asset else ""),
                            area=_txt(row[3])[:40], shift=_txt(row[4])[:40], operator_name=name,
                            operator_id_number=ced, delivered_at=delivered, returned_at=_cell_time(row[9]),
                            return_status=_txt(row[10])[:40] or None, notes=_txt(row[12])[:2000],
                            created_by=f"{user} (importado)", returned_by=f"{user} (importado)" if row[9] else ""))
            n_logs += 1
    audit(db, user, "import_excel", f"{file.filename}: {n_new} nuevos, {n_upd} actualizados, {n_link} enlazados al MDM, "
                                    f"{n_logs} movimientos")
    db.commit()
    msg = f"Importado: {n_new} equipo(s) nuevo(s), {n_upd} actualizado(s) y {n_logs} movimiento(s) de la bitácora."
    if n_link:
        msg += f" {n_link} equipo(s) quedaron enlazados a su tablet del MDM por el serial del software."
    if warns:
        msg += " Revisa: " + "; ".join(warns[:8]) + ("…" if len(warns) > 8 else "") + "."
    if n_skip:
        msg += f" Se omitieron {n_skip} fila(s) incompletas (faltaba fecha, código, operador u hora de entrega)."
    flash(request, msg)
    return back("/inventario")


# ============================================================ bitácora de turnos
def _filters(desde: str, hasta: str):
    t = today()
    try:
        d1 = parse_fecha(desde) if desde else t - timedelta(days=30)
        d2 = parse_fecha(hasta) if hasta else t
    except ValueError:
        d1, d2 = t - timedelta(days=30), t
    if d1 > d2:
        d1, d2 = d2, d1
    return d1, d2


def _logs_query(d1: date, d2: date, area: str, turno: str, estado: str, q: str):
    stmt = select(ShiftLog).where(ShiftLog.date >= d1, ShiftLog.date <= d2)
    if area in AREAS:
        stmt = stmt.where(ShiftLog.area == area)
    if turno in TURNOS:
        stmt = stmt.where(ShiftLog.shift == turno)
    if estado == "pendiente":
        stmt = stmt.where(ShiftLog.returned_at.is_(None))
    elif estado in ESTADOS_DEV:
        stmt = stmt.where(ShiftLog.return_status == estado)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ShiftLog.asset_code.ilike(like), ShiftLog.operator_name.ilike(like),
                              ShiftLog.operator_id_number.ilike(like)))
    return stmt


@router.get("/bitacora")
def bitacora_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db),
                  desde: str = "", hasta: str = "", area: str = "", turno: str = "", estado: str = "", q: str = ""):
    d1, d2 = _filters(desde, hasta)
    logs = list(db.scalars(_logs_query(d1, d2, area, turno, estado, q)
                           .order_by(ShiftLog.date.desc(), ShiftLog.delivered_at.desc(), ShiftLog.id.desc()).limit(1000)))
    stats = {
        "total": len(logs),
        **{a: sum(lg.area == a for lg in logs) for a in AREAS},
        "optimo": sum(lg.return_status == "Óptimo" for lg in logs),
        "leve": sum(lg.return_status == "Con detalle leve" for lg in logs),
        "alertas": sum(lg.return_status in ("Dañada", "Extraviada") for lg in logs),
    }
    open_logs = list(db.scalars(select(ShiftLog).where(ShiftLog.returned_at.is_(None))
                                .order_by(ShiftLog.date, ShiftLog.delivered_at)))
    out_ids = {lg.asset_id for lg in open_logs}
    assets = [a for a in _assets(db) if a.status == "Operativo"]
    workers = list(db.scalars(select(Worker).where(Worker.active.is_(True)).order_by(Worker.full_name)))
    now = local_now()
    return render(request, "bitacora.html", logs=logs, stats=stats, open_logs=open_logs, assets=assets,
                  out_ids=out_ids, workers=workers, d1=d1, d2=d2, area=area, turno=turno, estado=estado, q=q,
                  today=today(), now_hhmm=now.strftime("%H:%M"), shift_now=current_shift(now))


@router.post("/bitacora/entregar", dependencies=[Depends(check_csrf)])
async def bitacora_deliver(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        d = parse_fecha(_f(form, "date"))
        hhmm = parse_hora(_f(form, "delivered_at"), "Hora de entrega")
        aid = _f(form, "asset_id")
        asset = db.get(Asset, int(aid)) if aid.isdigit() else None
        if asset is None:
            raise ValueError("Elige el equipo que se entrega.")
        if asset.unassigned:
            raise ValueError(f"{asset.code} está S/A (sin asignar): todavía no tiene área ni personal. Asígnale un "
                             "área en el Inventario o hazle su acta de entrega, y luego regístralo aquí.")
        area, turno = _f(form, "area"), _f(form, "shift")
        if area not in AREAS:
            raise ValueError("Elige el área operativa.")
        if turno not in TURNOS:
            raise ValueError("Elige el turno.")
        wid = _f(form, "worker_id")
        worker = db.get(Worker, int(wid)) if wid.isdigit() else None
        if worker is None:  # persona nueva escrita a mano
            name = _f(form, "operator_name", 120)
            if len(name) < 3:
                raise ValueError("Elige al operador de la lista o escribe su nombre y cédula.")
            ced = norm_cedula(_f(form, "operator_id_number", 30))
            worker = worker_by_cedula(db, ced)
            if worker is None:
                worker = Worker(full_name=name, id_number=ced, area=area, shift=turno)
                db.add(worker)
                db.flush()
    except ValueError as exc:
        flash(request, f"No se registró la entrega. {exc}", "err")
        return back("/bitacora")
    busy = db.scalar(select(ShiftLog).where(ShiftLog.asset_id == asset.id, ShiftLog.returned_at.is_(None)))
    if busy:
        flash(request, f"{asset.code} ya está entregado a {busy.operator_name} desde el {busy.date:%d/%m} a las "
                       f"{hora_12(busy.delivered_at)}. Registra primero su devolución.", "err")
        return back("/bitacora")
    lg = ShiftLog(date=d, asset_id=asset.id, asset_code=asset.code, serial=asset.eff_serial, area=area, shift=turno,
                  worker_id=worker.id, operator_name=worker.full_name, operator_id_number=worker.id_number,
                  delivered_at=hhmm, notes=_f(form, "notes", 2000), created_by=user)
    db.add(lg)
    audit(db, user, "turno_entrega", f"{asset.code} → {worker.full_name} ({turno})")
    db.commit()
    flash(request, f"Entrega registrada: {asset.code} a {worker.full_name}, {turno}, {hora_12(hhmm)}.")
    return back("/bitacora")


@router.post("/bitacora/{log_id}/devolver", dependencies=[Depends(check_csrf)])
async def bitacora_return(log_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    lg = db.get(ShiftLog, log_id)
    if lg is None:
        raise HTTPException(404, "Movimiento no encontrado.")
    if lg.returned_at:
        flash(request, "Ese equipo ya tenía registrada la devolución.", "err")
        return back("/bitacora")
    try:
        hhmm = parse_hora(_f(form, "returned_at"), "Hora de devolución")
        estado = _f(form, "return_status")
        if estado not in ESTADOS_DEV:
            raise ValueError("Elige el estado en que se devolvió el equipo.")
    except ValueError as exc:
        flash(request, f"No se registró la devolución. {exc}", "err")
        return back("/bitacora")
    lg.returned_at, lg.return_status, lg.returned_by = hhmm, estado, user
    extra = _f(form, "notes", 2000)
    if extra:
        lg.notes = f"{lg.notes} | {extra}" if lg.notes else extra
    msg = f"Devolución registrada: {lg.asset_code} — {estado}."
    if estado in ("Dañada", "Extraviada") and lg.asset:
        lg.asset.status = "Dañado / Fuera de servicio"
        nota = f"[{lg.date:%d/%m/%Y}] Devuelta como «{estado}» en bitácora ({lg.operator_name})."
        lg.asset.notes = f"{lg.asset.notes}\n{nota}".strip()
        msg += f" ⚠ {lg.asset_code} quedó como «Dañado / Fuera de servicio» en el inventario."
    audit(db, user, "turno_devolucion", f"{lg.asset_code} {estado}")
    db.commit()
    flash(request, msg, "err" if estado in ("Dañada", "Extraviada") else "ok")
    return back("/bitacora")


@router.post("/bitacora/{log_id}/anular", dependencies=[Depends(check_csrf)])
def bitacora_delete(log_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    lg = db.get(ShiftLog, log_id)
    if lg:
        audit(db, user, "turno_anular", f"#{lg.id} {lg.date} {lg.asset_code} {lg.operator_name} {lg.delivered_at}")
        db.delete(lg)
        db.commit()
        flash(request, "Movimiento anulado (queda constancia en Auditoría).")
    return back("/bitacora")


@router.get("/bitacora/export.xlsx")
def bitacora_export(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db),
                    desde: str = "", hasta: str = "", area: str = "", turno: str = "", estado: str = "", q: str = ""):
    d1, d2 = _filters(desde, hasta)
    logs = list(db.scalars(_logs_query(d1, d2, area, turno, estado, q)
                           .order_by(ShiftLog.date, ShiftLog.delivered_at, ShiftLog.id)))
    data = xf.build_bitacora(logs, _assets(db), company(db), logo_path())
    audit(db, user, "export_bitacora", f"{d1} a {d2}: {len(logs)} filas")
    db.commit()
    return _xlsx(data, f"Bitacora_Control_Turnos_Tablets_{d1:%Y-%m-%d}_a_{d2:%Y-%m-%d}.xlsx")


# ============================================================ actas de entrega
@router.get("/actas")
def actas_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    acts = db.scalars(select(HandoverAct).order_by(HandoverAct.id.desc()).limit(500)).all()
    return render(request, "actas.html", acts=acts)


@router.get("/actas/nueva")
def acta_new(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db),
             asset: int = 0, worker: int = 0):
    assets = [a for a in _assets(db) if a.status != "Dañado / Fuera de servicio"]
    workers = list(db.scalars(select(Worker).where(Worker.active.is_(True)).order_by(Worker.full_name)))
    return render(request, "acta_form.html", assets=assets, workers=workers, comp=company(db), today=today(),
                  sel_asset=asset, sel_worker=worker)


def _next_act_number(db: Session, year: int) -> str:
    prefix = f"AE-{year}-"
    n = 0
    for num in db.scalars(select(HandoverAct.number).where(HandoverAct.number.like(f"{prefix}%"))):
        tail = num[len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return f"{prefix}{n + 1:04d}"


@router.post("/actas/save", dependencies=[Depends(check_csrf)])
async def acta_save(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        d = parse_fecha(_f(form, "date"), "Fecha de entrega")
        aid, wid = _f(form, "asset_id"), _f(form, "worker_id")
        asset = db.get(Asset, int(aid)) if aid.isdigit() else None
        worker = db.get(Worker, int(wid)) if wid.isdigit() else None
        if asset is None:
            raise ValueError("Elige el equipo que se entrega.")
        if worker is None:
            raise ValueError("Elige al trabajador que recibe el equipo (si no está, agrégalo en Personal).")
        # Si no se eligió: el área del equipo; si el equipo está S/A, la del trabajador.
        area = (_f(form, "area") or (asset.area if asset.area in AREAS_ACTA else "")
                or (worker.area if worker.area in AREAS_ACTA else ""))
        if area not in AREAS_ACTA:
            raise ValueError("Elige el área asignada (Extrusión, Impresión, Sellado o Mantenimiento).")
        condition = _f(form, "condition")
        if condition not in CONDICIONES:
            raise ValueError("Elige la condición física del equipo.")
        by_ced = _f(form, "delivered_by_id_number", 30)
        by_ced = norm_cedula(by_ced) if by_ced else ""
    except ValueError as exc:
        flash(request, f"No se generó el acta. {exc}", "err")
        return back("/actas/nueva")
    shift = _f(form, "shift") if _f(form, "shift") in TURNOS else worker.shift
    act = HandoverAct(
        number=_next_act_number(db, d.year), date=d, asset_id=asset.id, worker_id=worker.id,
        asset_code=asset.code, brand_model=f"{asset.eff_brand} {asset.eff_model}".strip(),
        serial=asset.eff_serial, software=asset.software, worker_name=worker.full_name,
        worker_id_number=worker.id_number, worker_position=_f(form, "worker_position", 80) or worker.position,
        worker_shift=shift or "", area=area, condition=condition,
        accessories=[x for x in form.getlist("accessories") if x in ACCESORIOS], notes=_f(form, "notes", 1000),
        delivered_by_name=_f(form, "delivered_by_name", 120), delivered_by_id_number=by_ced,
        delivered_by_position=_f(form, "delivered_by_position", 80), created_by=user)
    db.add(act)
    if asset.unassigned:  # S/A: con el acta el equipo queda asignado a esa área (y a esa persona)
        asset.area = area
    # Recordar quién entrega para la próxima acta
    save_company(db, {"handover_by_name": act.delivered_by_name, "handover_by_id_number": act.delivered_by_id_number,
                      "handover_by_position": act.delivered_by_position})
    db.flush()
    audit(db, user, "acta_new", f"{act.number} {act.asset_code} → {act.worker_name}")
    db.commit()
    flash(request, f"Acta {act.number} generada. Imprímela, fírmala y archívala.")
    return back(f"/actas/{act.id}")


@router.get("/actas/{act_id}")
def acta_view(act_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    act = db.get(HandoverAct, act_id)
    if not act:
        raise HTTPException(404, "Esa acta no existe.")
    comp = company(db)
    texts = xf.acta_texts(comp)
    texts["intro"] = xf.acta_paragraph(texts["intro"], act)
    return render(request, "acta_print.html", act=act, comp=comp, t=texts, has_logo=logo_path() is not None,
                  marks=xf.marks)


@router.get("/actas/{act_id}/acta.xlsx")
def acta_xlsx(act_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    act = db.get(HandoverAct, act_id)
    if not act:
        raise HTTPException(404, "Esa acta no existe.")
    data = xf.build_acta(act, company(db), logo_path())
    return _xlsx(data, f"Acta_Entrega_{act.number}_{act.asset_code}.xlsx")


@router.get("/formatos/bitacora-en-blanco.xlsx")
def blank_template(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    """El formato en blanco (con la empresa y el logo), para imprimir y llenar a mano si no hay sistema."""
    data = xf.build_bitacora([], _assets(db), company(db), logo_path())
    return _xlsx(data, "Bitacora_Control_Turnos_Tablets_en_blanco.xlsx")

