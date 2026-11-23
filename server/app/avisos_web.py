"""Pantalla «Avisos»: enviar mensajes a las tablets por MQTT y ver quién los recibió y leyó."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .control import AREA_SA, AREAS_INV
from .db import get_db
from .models import Asset, Notice, NoticeDelivery
from .mqtt_bus import (AVISOS_PACKAGE, INTEGRATION_USER, PRIORIDADES, VIGENCIAS, avisos_app, bus, create_notice,
                       devices_with_avisos, mqtt_password, utcnow)
from .services import audit
from .web import back, check_csrf, flash, render, require_admin

router = APIRouter()
AREAS_AVISO = [a for a in AREAS_INV if a != AREA_SA]


def _f(form, key: str, maxlen: int = 600) -> str:
    return str(form.get(key, "") or "").strip()[:maxlen]


def _aware(d):
    from datetime import timezone

    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


@router.get("/avisos")
def avisos_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    tablets = devices_with_avisos(db)
    areas_by_dev = dict(db.execute(select(Asset.device_id, Asset.area).where(Asset.device_id.is_not(None))).all())
    notices = db.scalars(select(Notice).order_by(Notice.id.desc()).limit(50)).all()
    counts = {}
    if notices:
        rows = db.execute(select(NoticeDelivery.notice_id, func.count(NoticeDelivery.id),
                                 func.count(NoticeDelivery.received_at), func.count(NoticeDelivery.read_at))
                          .where(NoticeDelivery.notice_id.in_([n.id for n in notices]))
                          .group_by(NoticeDelivery.notice_id)).all()
        counts = {r[0]: {"total": r[1], "recibido": r[2], "leido": r[3]} for r in rows}
    st = bus.status()
    from .models import App, Policy

    app_ids = set(db.scalars(select(App.id).where(App.package_name == AVISOS_PACKAGE)))
    policies_with_app = [p for p in db.scalars(select(Policy).where(Policy.platform == "android"))
                         if app_ids & set((p.settings or {}).get("required_app_ids", []))]
    from urllib.parse import urlparse

    from .config import get_settings

    return render(request, "avisos.html", tablets=tablets, policies_with_app=policies_with_app, areas_by_dev=areas_by_dev, notices=notices, counts=counts,
                  st=st, bus=bus, app=avisos_app(db), AVISOS_PACKAGE=AVISOS_PACKAGE, PRIORIDADES=PRIORIDADES,
                  VIGENCIAS=VIGENCIAS, AREAS_AVISO=AREAS_AVISO, now_utc=utcnow(), aware=_aware,
                  host=urlparse(get_settings().public_url).hostname, integ_user=INTEGRATION_USER,
                  integ_pass=mqtt_password(INTEGRATION_USER))


@router.post("/avisos/enviar", dependencies=[Depends(check_csrf)])
async def avisos_send(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    kind = _f(form, "destino", 16)
    value = {"area": _f(form, "area", 40), "tablet": _f(form, "tablet", 100)}.get(kind, "")
    try:
        hours = int(_f(form, "vigencia", 4) or 8)
        n = create_notice(db, user, _f(form, "titulo", 200), _f(form, "mensaje", 1000),
                          _f(form, "prioridad", 16) or "normal", kind, value, hours)
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return back("/avisos")
    audit(db, user, "aviso_enviado", f"#{n.id} «{n.title}» ({n.priority}) → {n.target_label}")
    db.commit()
    total = len(n.deliveries)
    if not bus.running:
        flash(request, f"Aviso guardado para {total} tablet(s), pero el servicio MQTT no está activo: "
                       f"{bus.broker_error or 'revisa MDM_MQTT_ENABLED en .env'}. Se enviará al activarlo.", "err")
        return back(f"/avisos/{n.id}")
    sent = bus.send_notice(db, n)
    online = sum(1 for d in n.deliveries if bus.is_online(d.device_id))
    msg = f"Aviso enviado a {total} tablet(s)."
    if online < total:
        msg += f" {total - online} no están conectadas ahora: lo recibirán apenas se conecten (hasta que venza)."
    flash(request, msg)
    return back(f"/avisos/{n.id}")


@router.get("/avisos/{notice_id}")
def aviso_detail(notice_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    n = db.get(Notice, notice_id)
    if n is None:
        raise HTTPException(404, "Ese aviso no existe.")
    return render(request, "aviso_detalle.html", n=n, bus=bus, PRIORIDADES=PRIORIDADES, now_utc=utcnow(), aware=_aware)


@router.post("/avisos/{notice_id}/retirar", dependencies=[Depends(check_csrf)])
def aviso_retire(notice_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    n = db.get(Notice, notice_id)
    if n is None:
        raise HTTPException(404, "Ese aviso no existe.")
    if not n.retired:
        n.retired, n.retired_at = True, utcnow()
        audit(db, user, "aviso_retirado", f"#{n.id} «{n.title}»")
        db.commit()
        bus.retire_notice(db, n)
        flash(request, "Aviso retirado: desaparece de las pantallas de las tablets conectadas "
                       "(y de las demás apenas se conecten).")
    return back(f"/avisos/{n.id}")


@router.post("/avisos/{notice_id}/reenviar", dependencies=[Depends(check_csrf)])
def aviso_resend(notice_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    n = db.get(Notice, notice_id)
    if n is None:
        raise HTTPException(404, "Ese aviso no existe.")
    if n.retired or _aware(n.expires_at) <= utcnow():
        flash(request, "Ese aviso ya está retirado o vencido: envía uno nuevo.", "err")
        return back(f"/avisos/{n.id}")
    sent = bus.send_notice(db, n)
    flash(request, f"Reenviado a {sent} tablet(s) que aún no lo habían confirmado." if sent else
          "No hay tablets pendientes, o el servicio MQTT no está conectado.")
    return back(f"/avisos/{n.id}")
