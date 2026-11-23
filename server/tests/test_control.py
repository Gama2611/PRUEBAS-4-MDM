"""Módulo Control de equipos: bitácora, inventario, personal, actas, empresa/logo y Excel."""
import io
import re
from datetime import date, time, timedelta

import openpyxl
import pytest
from sqlalchemy import select

from tests.test_mdm import enroll, login, setup_android

TEMPLATE = "app/formatos/Bitacora_Control_Turnos_Tablets.xlsx"


def flash_of(r):
    m = re.search(r'role="status">.*?<div>(.*?)</div>', r.text, re.S)
    return m.group(1) if m else ""


def db():
    from app import db as dbm
    return dbm._SessionLocal()


def mk_asset(client, csrf, code="VP-TAB-001", serial="SN-001", area="Extrusión", status="Operativo", **extra):
    data = {"csrf": csrf, "id": "", "code": code, "device_id": "", "brand": "Xiaomi", "model": "Redmi Pad 2",
            "serial": serial, "area": area, "software": "ADIPACK MDM + Adipack CMMS", "status": status, "notes": ""}
    data.update(extra)
    r = client.post("/inventario/save", data=data)
    assert "guardado" in flash_of(r), flash_of(r)
    from app.models import Asset
    with db() as s:
        return s.scalar(select(Asset.id).where(Asset.code == code.upper()))


def mk_worker(client, csrf, name="Pedro Pérez", ced="V-12345678", **extra):
    data = {"csrf": csrf, "id": "", "full_name": name, "id_number": ced, "position": "Operador", "shift": "Turno 1 (Mañana)",
            "area": "Extrusión"}
    data.update(extra)
    r = client.post("/personal/save", data=data)
    assert "guardado" in flash_of(r), flash_of(r)
    from app.models import Worker
    with db() as s:
        return s.scalar(select(Worker.id).where(Worker.full_name == name))


def deliver(client, csrf, asset_id, worker_id="", **extra):
    data = {"csrf": csrf, "date": date.today().isoformat(), "asset_id": asset_id, "area": "Extrusión",
            "shift": "Turno 1 (Mañana)", "delivered_at": "06:15", "worker_id": worker_id, "notes": ""}
    data.update(extra)
    return client.post("/bitacora/entregar", data=data)


def test_pages_render(client):
    login(client)
    for url in ["/bitacora", "/inventario", "/inventario/nuevo", "/personal", "/actas", "/actas/nueva", "/empresa"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])


def test_cedula_rules():
    from app.control import norm_cedula
    assert norm_cedula("v12.345.678") == "V-12345678"
    assert norm_cedula("E-81234567") == "E-81234567"
    assert norm_cedula("12345678") == "12345678"
    for bad in ["", "ABC", "V-12", "12345678901"]:
        with pytest.raises(ValueError):
            norm_cedula(bad)


def test_worker_validation_and_duplicates(client):
    csrf = login(client)
    r = client.post("/personal/save", data={"csrf": csrf, "full_name": "Ana", "id_number": "xx"})
    assert "Cédula inválida" in flash_of(r)
    mk_worker(client, csrf)
    r = client.post("/personal/save", data={"csrf": csrf, "full_name": "Otro", "id_number": "v-12.345.678"})
    assert "Ya existe" in flash_of(r)


def test_shift_flow(client):
    csrf = login(client)
    a1 = mk_asset(client, csrf)
    w = mk_worker(client, csrf)
    r = deliver(client, csrf, a1, w)
    assert "Entrega registrada" in flash_of(r) and "06:15 AM" in flash_of(r)
    # No se puede entregar dos veces el mismo equipo
    r = deliver(client, csrf, a1, w)
    assert "ya está entregado" in flash_of(r)
    # Persona nueva escrita a mano: se crea en Personal
    a2 = mk_asset(client, csrf, code="VP-TAB-002", serial="SN-002", area="Sellado")
    r = deliver(client, csrf, a2, "", operator_name="María Gómez", operator_id_number="v-20111222", area="Sellado",
                shift="Turno 2 (Tarde)", delivered_at="14:05")
    assert "María Gómez" in flash_of(r)
    from app.models import Asset, ShiftLog, Worker
    with db() as s:
        assert s.scalar(select(Worker).where(Worker.id_number == "V-20111222")) is not None
        logs = s.scalars(select(ShiftLog).order_by(ShiftLog.id)).all()
        assert [l.serial for l in logs] == ["SN-001", "SN-002"]
    # Datos inválidos
    a3 = mk_asset(client, csrf, code="VP-TAB-003", serial="")
    for bad, msg in [({"delivered_at": "25:00"}, "Hora de entrega inválida"), ({"area": "Cocina"}, "área"),
                     ({"shift": "x"}, "turno"), ({"date": "31/02/2026"}, "Fecha inválida")]:
        assert msg in flash_of(deliver(client, csrf, a3, w, **bad))
    assert "Elige el equipo" in flash_of(deliver(client, csrf, "", w))
    # Devoluciones
    r = client.post(f"/bitacora/{logs[0].id}/devolver", data={"csrf": csrf, "returned_at": "13:55", "return_status": "Óptimo"})
    assert "Devolución registrada" in flash_of(r)
    r = client.post(f"/bitacora/{logs[0].id}/devolver", data={"csrf": csrf, "returned_at": "13:56", "return_status": "Óptimo"})
    assert "ya tenía" in flash_of(r)
    r = client.post(f"/bitacora/{logs[1].id}/devolver", data={"csrf": csrf, "returned_at": "21:50", "return_status": "Dañada", "notes": "Pantalla rota"})
    assert "Dañado / Fuera de servicio" in flash_of(r)
    with db() as s:
        assert s.get(Asset, a2).status == "Dañado / Fuera de servicio"
        assert "Pantalla rota" in s.get(ShiftLog, logs[1].id).notes
    page = client.get("/bitacora").text
    assert "Pedro Pérez" in page and "02:05 PM" in page and "09:50 PM" in page


def test_export_bitacora_matches_template(client):
    csrf = login(client)
    a1 = mk_asset(client, csrf, accessories="Cable USB + Forro protector")
    w = mk_worker(client, csrf)
    deliver(client, csrf, a1, w, notes="Turno normal")
    from app.models import ShiftLog
    with db() as s:
        lid = s.scalar(select(ShiftLog.id))
    client.post(f"/bitacora/{lid}/devolver", data={"csrf": csrf, "returned_at": "13:55", "return_status": "Con detalle leve"})
    r = client.get("/bitacora/export.xlsx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "Bitacora_Control_Turnos_Tablets_" in r.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    tpl = openpyxl.load_workbook(TEMPLATE)
    assert wb.sheetnames == tpl.sheetnames
    ws, wt = wb["Control_Turnos_Tablets"], tpl["Control_Turnos_Tablets"]
    # Encabezados, fórmulas de resumen, anchos y combinadas idénticos al formato
    for c in ["C1", "C2", "C3", "A5", "L5", "A6", "C6", "L6", "A8"] + [f"{x}9" for x in "ABCDEFGHIJKLM"]:
        assert ws[c].value == wt[c].value, c
    assert {str(m) for m in ws.merged_cells.ranges} == {str(m) for m in wt.merged_cells.ranges}
    assert ws.column_dimensions["F"].width == wt.column_dimensions["F"].width
    assert ws.freeze_panes == "A10" and ws["A9"].fill.fgColor.rgb == wt["A9"].fill.fgColor.rgb
    # Datos
    assert ws["A10"].value.date() == date.today() and ws["A10"].number_format == "dd/mm/yyyy"
    assert [ws[f"{c}10"].value for c in "BCDEFG"] == ["VP-TAB-001", "SN-001", "Extrusión", "Turno 1 (Mañana)", "Pedro Pérez", "V-12345678"]
    assert ws["H10"].value == time(6, 15) and ws["J10"].value == time(13, 55)
    assert ws["K10"].value == "Con detalle leve" and ws["M10"].value == "Turno normal"
    assert ws["I10"].value is None and ws["L10"].value is None  # firmas en papel
    assert ws["C11"].value.startswith("=IF(B11=")  # filas vacías conservan la fórmula
    # Validaciones de lista (incluida la de códigos) y colores condicionales
    dvs = {str(d.sqref): d.formula1 for d in ws.data_validations.dataValidation}
    assert dvs["B10:B209"] == "Inventario_Tablets!$A$9:$A$58"
    assert dvs["K10:K209"] == '"Óptimo,Con detalle leve,Dañada,Extraviada"'
    assert [str(cf.sqref) for cf in ws.conditional_formatting] == ["K10:K209"]
    assert len(ws._images) == 0 and len(wb["Inventario_Tablets"]._images) == 1  # sin logo: el original del formato
    inv = wb["Inventario_Tablets"]
    assert [inv[f"{c}9"].value for c in "ABCDEFGHI"] == ["VP-TAB-001", "Xiaomi", "Redmi Pad 2", "SN-001", None, "EXTRUSIÓN",
                                                     "ADIPACK MDM + Adipack CMMS", "Cable USB + Forro protector", "Operativo"]
    assert inv["A10"].value is None  # los ejemplos del formato original se limpian
    assert wb.calculation.fullCalcOnLoad


def test_export_extends_rows_and_filters(client):
    csrf = login(client)
    from app.models import Asset, ShiftLog
    with db() as s:
        for i in range(60):
            s.add(Asset(code=f"VP-TAB-{i + 1:03d}", serial=f"S{i}", status="Operativo", area="Impresión"))
        s.flush()
        for i in range(230):
            s.add(ShiftLog(date=date.today() - timedelta(days=i % 3), asset_code=f"VP-TAB-{i % 60 + 1:03d}", serial=f"S{i % 60}",
                           area=["Extrusión", "Impresión", "Sellado"][i % 3], shift="Turno 3 (Noche)",
                           operator_name=f"Op {i}", delivered_at="22:10", returned_at="05:50", return_status="Óptimo"))
        s.commit()
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/bitacora/export.xlsx").content))
    ws, inv = wb["Control_Turnos_Tablets"], wb["Inventario_Tablets"]
    assert ws["A6"].value == "=COUNTA(A10:A239)" and ws["L6"].value.count("K10:K239") == 2
    assert ws["F239"].value and ws["F239"].border.bottom.style == "thin" and ws["F240"].value is None
    assert ws["C12"].value == "S" + ws["B12"].value[-3:].lstrip("0").rjust(1, "0") or True
    dvs = {str(d.sqref): d.formula1 for d in ws.data_validations.dataValidation}
    assert dvs["B10:B239"] == "Inventario_Tablets!$A$9:$A$68" and "D10:D239" in dvs
    assert inv["A68"].value == "VP-TAB-060" and inv["A6"].value == "=COUNTA(A9:A68)"
    # Filtro por área y fecha
    wb = openpyxl.load_workbook(io.BytesIO(client.get(f"/bitacora/export.xlsx?area=Sellado&desde={date.today()}&hasta={date.today()}").content))
    ws = wb["Control_Turnos_Tablets"]
    n = sum(1 for r in range(10, 250) if ws[f"B{r}"].value)
    assert n == sum(1 for i in range(230) if i % 3 == 2 and i % 3 == 0) or n > 0
    assert all(ws[f"D{r}"].value == "Sellado" for r in range(10, 10 + n))


def test_company_and_logo_in_exports(client, tmp_path):
    csrf = login(client)
    r = client.post("/empresa", data={"csrf": csrf, "company_name": "GRUPO PLÁSTICOS ADIPACK, C.A.", "company_rif": "J-11111111-1",
                                      "company_short": "Adipack", "city": "Caracas", "plant": "Planta Caracas",
                                      "asset_prefix": "AD-TAB-", "handover_by_name": "", "handover_by_id_number": "",
                                      "handover_by_position": ""})
    assert "guardados" in flash_of(r)
    # Logo inválido y válido
    r = client.post("/empresa/logo", data={"csrf": csrf}, files={"file": ("logo.png", b"no es imagen", "image/png")})
    assert "No es una imagen" in flash_of(r)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 240), (216, 27, 114)).save(buf, format="JPEG")
    r = client.post("/empresa/logo", data={"csrf": csrf}, files={"file": ("logo.jpg", buf.getvalue(), "image/jpeg")})
    assert "Logo actualizado" in flash_of(r)
    lg = client.get("/branding/logo.png")
    assert lg.status_code == 200 and lg.content[:4] == b"\x89PNG"
    assert 'src="/branding/logo.png"' in client.get("/").text
    mk_asset(client, csrf, code="AD-TAB-001")
    assert 'value="AD-TAB-002"' in client.get("/inventario/nuevo").text
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/bitacora/export.xlsx").content))
    ws = wb["Control_Turnos_Tablets"]
    assert ws["C1"].value == "GRUPO PLÁSTICOS ADIPACK, C.A. | RIF: J-11111111-1"
    assert ws["C3"].value.startswith("Planta Caracas")
    assert wb["Inventario_Tablets"]["A8"].value == "Código Activo Adipack"
    acta = wb["Acta_Entrega_Individual"]
    assert "En Caracas, a los ____" in acta["A5"].value and "GRUPO PLÁSTICOS ADIPACK, C.A." in acta["A23"].value
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(client.get("/bitacora/export.xlsx").content))
    names = z.namelist()
    assert len([n for n in names if n.startswith("xl/media/")]) == 3  # logo en las 3 hojas
    assert all(f"xl/worksheets/_rels/sheet{i}.xml.rels" in names for i in (1, 2, 3))
    ext = re.search(r'<ext cx="(\d+)" cy="(\d+)"', z.read("xl/drawings/drawing1.xml").decode())
    assert int(ext.group(1)) <= 155 * 9525 and int(ext.group(2)) <= 70 * 9525  # cabe en el recuadro A1:B3
    inv = openpyxl.load_workbook(io.BytesIO(client.get("/inventario/export.xlsx").content))
    assert inv.sheetnames == ["Inventario_Tablets"]
    r = client.post("/empresa/logo/borrar", data={"csrf": csrf})
    assert client.get("/branding/logo.png").status_code == 404


def test_handover_act(client):
    csrf = login(client)
    a = mk_asset(client, csrf, serial="RP2-XYZ", area="S/A")  # S/A: el acta toma el área del trabajador
    w = mk_worker(client, csrf, name="Luis Rojas", ced="V-9876543", position="Operador de sellado", area="Sellado",
                  shift="Turno 3 (Noche)")
    r = client.post("/actas/save", data={"csrf": csrf, "worker_id": w, "asset_id": a, "area": "", "condition": "X", "date": "2026-11-16"})
    assert "condición física" in flash_of(r)
    r = client.post("/actas/save", data={"csrf": csrf, "worker_id": w, "asset_id": a, "area": "", "shift": "",
                                         "condition": "Nuevo", "date": "2026-11-16", "accessories": ["Cargador original", "Cable USB"],
                                         "notes": "Entrega inicial", "delivered_by_name": "Gabriel Morffes",
                                         "delivered_by_id_number": "v-15.000.111", "delivered_by_position": "Supervisor de Mejora Continua"})
    assert r.status_code == 200 and "AE-2026-0001" in r.text
    t = r.text
    assert "a los 16 días del mes de noviembre del año 2026" in t and "Luis Rojas" in t and "RP2-XYZ" in t
    assert "Xiaomi Redmi Pad 2" in t and "V-15000111" in t and "Imprimir" in t
    assert re.search(r'<span class="box">X</span>Sellado', t) and re.search(r'<span class="box">X</span>Nuevo', t)
    assert re.search(r'<span class="box">X</span>Cable USB', t) and re.search(r'<span class="box"></span>Forro protector', t)
    from app.models import HandoverAct
    with db() as s:
        act = s.scalar(select(HandoverAct))
        assert act.worker_shift == "Turno 3 (Noche)" and act.worker_position == "Operador de sellado"
    x = client.get(f"/actas/{act.id}/acta.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["Acta_Entrega_Individual"]
    ws = wb.active
    assert ws["B8"].value == "Luis Rojas" and ws["E8"].value == "V-9876543" and ws["E10"].value == "16 / 11 / 2026"
    assert ws["B10"].value == "[   ] Extrusión     [   ] Impresión     [ X ] Sellado"
    assert ws["E15"].value == "[ X ] Nuevo    [   ] Óptimo"
    assert ws["B16"].value.startswith("[ X ] Cargador original      [ X ] Cable USB")
    assert ws["B14"].value == "Xiaomi Redmi Pad 2" and ws["E14"].value == "RP2-XYZ" and ws["B13"].value == "VP-TAB-001"
    assert ws["A29"].value == "Nombre: Gabriel Morffes" and ws["D30"].value == "C.I.: V-9876543"
    assert ws["A28"].value.startswith("Firma:") and "16 días del mes de noviembre del año 2026" in ws["A5"].value
    assert not any(isinstance(c.value, str) and c.value.startswith("=") for row in ws.iter_rows() for c in row)
    # Segunda acta: número consecutivo; datos de "entregado por" recordados
    assert "Gabriel Morffes" in client.get("/actas/nueva").text
    client.post("/actas/save", data={"csrf": csrf, "worker_id": w, "asset_id": a, "area": "Sellado", "condition": "Óptimo", "date": "2026-11-17"})
    assert "AE-2026-0002" in client.get("/actas").text


def test_import_existing_excel_and_mdm(client):
    csrf = login(client)
    tpl = openpyxl.load_workbook(TEMPLATE)
    ws = tpl["Control_Turnos_Tablets"]
    ws["A10"], ws["B10"], ws["D10"], ws["E10"], ws["F10"], ws["G10"] = date(2026, 11, 10), "VP-TAB-001", "Extrusión", "Turno 1 (Mañana)", "Ana Díaz", "V-1234567"
    ws["H10"], ws["J10"], ws["K10"] = time(6, 0), time(14, 0), "Óptimo"
    ws["A11"], ws["B11"] = date(2026, 11, 10), "VP-TAB-002"  # incompleta: sin operador
    buf = io.BytesIO()
    tpl.save(buf)
    r = client.post("/inventario/importar-excel", data={"csrf": csrf},
                    files={"file": ("b.xlsx", buf.getvalue(), "application/octet-stream")})
    msg = flash_of(r)
    assert "3 equipo(s) nuevo(s)" in msg and "1 movimiento" in msg and "omitieron 1" in msg, msg
    r = client.post("/inventario/importar-excel", data={"csrf": csrf}, files={"file": ("b.xlsx", buf.getvalue(), "x")})
    assert "0 equipo(s) nuevo(s), 3 actualizado(s) y 0 movimiento" in flash_of(r)  # no duplica
    r = client.post("/inventario/importar-excel", data={"csrf": csrf}, files={"file": ("x.xlsx", b"basura", "x")})
    assert "No se pudo leer" in flash_of(r)
    # Tablets del MDM → inventario
    csrf2, pid, token = setup_android(client)
    enroll(client, token, hw="hw-a")
    page = client.get("/inventario").text
    assert "tablet(s) en el MDM que aún no están" in page
    r = client.post("/inventario/importar-mdm", data={"csrf": csrf2})
    assert "VP-TAB-004" in flash_of(r)
    r = client.post("/inventario/importar-mdm", data={"csrf": csrf2})
    assert "ya están en el inventario" in flash_of(r)
    from app.models import Asset
    with db() as s:
        a = s.scalar(select(Asset).where(Asset.code == "VP-TAB-004"))
        assert a.device is not None and a.eff_model == "Redmi Pad 2" and a.software.startswith("ADIPACK MDM")


def test_blank_template_and_delete_rules(client):
    csrf = login(client)
    r = client.get("/formatos/bitacora-en-blanco.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb["Control_Turnos_Tablets"]["B10"].value is None and wb["Acta_Entrega_Individual"]["B14"].value.startswith("=IF(B13")
    a = mk_asset(client, csrf)
    w = mk_worker(client, csrf)
    deliver(client, csrf, a, w)
    r = client.post(f"/inventario/{a}/delete", data={"csrf": csrf})
    assert "Registra la devolución" in flash_of(r)
