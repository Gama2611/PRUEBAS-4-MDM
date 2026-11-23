"""Formato de inventario de la empresa (v1.3): 10 columnas, «Serial del Software», áreas MANTENIMIENTO y S/A.

Se usa el archivo REAL de la empresa (tests/fixtures/Inventario_Tablets_formato_empresa.xlsx): se importa,
se vuelve a exportar y el resultado se compara celda por celda con el original.
"""
import io
import warnings
from datetime import date
from pathlib import Path

import openpyxl
from sqlalchemy import select

from test_control import db, deliver, flash_of, mk_asset, mk_worker
from test_mdm import login, setup_android

FIX = Path(__file__).parent / "fixtures"
EMPRESA = FIX / "Inventario_Tablets_formato_empresa.xlsx"
ANTERIOR = FIX / "Bitacora_formato_anterior_v1_2.xlsx"


def _load(data):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return openpyxl.load_workbook(io.BytesIO(data) if isinstance(data, bytes) else data)


def import_file(client, csrf, path_or_bytes, name="Inventario_Tablets_2026-11-17.xlsx"):
    data = path_or_bytes.read_bytes() if isinstance(path_or_bytes, Path) else path_or_bytes
    r = client.post("/inventario/importar-excel", data={"csrf": csrf}, files={"file": (name, data, "application/octet-stream")})
    return flash_of(r)


def assets_by_code():
    from sqlalchemy.orm import selectinload

    from app.models import Asset
    with db() as s:
        return {a.code: a for a in s.scalars(select(Asset).options(selectinload(Asset.device)))}


def enroll_serial(client, token, hw, serial):
    r = client.post("/api/v1/enroll", json={"enrollment_token": token, "platform": "android", "hardware_id": hw,
                                            "serial": serial, "manufacturer": "Xiaomi", "model": "25040RP0AL",
                                            "agent_version": "1.0"})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_company_file_exactly(client):
    csrf = login(client)
    msg = import_file(client, csrf, EMPRESA)
    assert "7 equipo(s) nuevo(s)" in msg and "Revisa" not in msg, msg
    a = assets_by_code()
    t1, t2, t3 = a["VP-TAB-001"], a["VP-TAB-002"], a["VP-TAB-003"]
    assert (t1.brand, t1.model, t1.serial, t1.software_serial) == ("Xiaomi", "25040RP0AL (REDMI PAD 2)", "65520/W6S600937",
                                                                  "dmirl78d8h75pvg6")
    assert (t1.area, t2.area, t3.area) == ("Impresión", "Mantenimiento", "S/A")
    assert not t1.unassigned and t3.unassigned and sum(x.unassigned for x in a.values()) == 5
    assert t1.accessories == "FORRO + VIDRIO TEMPLADO + CARGADOR Y CABLE"
    assert t1.software == "ADIPACK MDM + Adipack CMMS" and t1.status == "Operativo" and t1.notes == "Equipo MDM TAB-01"
    # Volver a importar no duplica
    assert "0 equipo(s) nuevo(s), 7 actualizado(s)" in import_file(client, csrf, EMPRESA)
    page = client.get("/inventario").text
    assert "S/A · Sin asignar" in page and "dmirl78d8h75pvg6" in page and "65520/W6S600937" in page


def test_round_trip_export_identical_to_company_file(client):
    """Importar el archivo de la empresa y exportarlo: mismas celdas, estilos, medidas, listas y colores."""
    csrf = login(client)
    import_file(client, csrf, EMPRESA)
    out = _load(client.get("/inventario/export.xlsx").content)
    assert out.sheetnames == ["Inventario_Tablets"]
    ws, src = out["Inventario_Tablets"], _load(EMPRESA)["Inventario_Tablets"]

    def style(c):
        f, fl, b, al = c.font, c.fill, c.border, c.alignment
        return (f.name, f.sz, f.b, f.color.rgb if f.color else None, fl.fill_type, fl.fgColor.rgb,
                tuple(getattr(b, x).style for x in ("left", "right", "top", "bottom")),
                al.horizontal, al.vertical, al.wrap_text, c.number_format)

    for r in range(1, 59):
        for c in range(1, 11):
            x, y = ws.cell(r, c), src.cell(r, c)
            assert x.value == y.value, (x.coordinate, x.value, y.value)
            assert style(x) == style(y), x.coordinate
        assert ws.row_dimensions[r].height == src.row_dimensions[r].height
    for col in "ABCDEFGHIJ":
        assert ws.column_dimensions[col].width == src.column_dimensions[col].width, col
    assert sorted(map(str, ws.merged_cells.ranges)) == sorted(map(str, src.merged_cells.ranges))
    assert ws.freeze_panes == "A9" and ws.page_setup.orientation == "landscape" and ws.page_setup.scale == 49
    assert ws.print_area == "'Inventario_Tablets'!$A$1:$J$15"
    dvs = {str(d.sqref): d.formula1 for d in ws.data_validations.dataValidation}
    assert dvs["F9:F58"] == '"EXTRUSIÓN,IMPRESIÓN,SELLADO,MANTENIMIENTO,RESPALDO / TI,S/A"'
    assert dvs["I9:I58"] == '"Operativo,En revisión / Mantenimiento,Dañado / Fuera de servicio"'
    assert [str(f.sqref) for f in ws.conditional_formatting] == ["I9:I58"]
    assert len(ws._images) == 1


def test_mdm_link_by_software_serial_and_notes(client):
    csrf, pid, token = setup_android(client)
    enroll_serial(client, token, "hw-1", "dmirl78d8h75pvg6")   # TAB-01 por serial del software
    enroll_serial(client, token, "hw-2", "OTRO-SERIAL")        # TAB-02 sólo por «Equipo MDM TAB-02»
    msg = import_file(client, csrf, EMPRESA)
    assert "2 equipo(s) quedaron enlazados" in msg, msg
    a = assets_by_code()
    assert a["VP-TAB-001"].device.name == "TAB-01" and a["VP-TAB-002"].device.name == "TAB-02"
    assert a["VP-TAB-003"].device is None
    # Volver a importar no cambia los enlaces: un equipo del MDM nunca queda en dos fichas
    assert "quedaron enlazados" not in import_file(client, csrf, EMPRESA)
    assert len({x.device_id for x in assets_by_code().values() if x.device_id}) == 2


def test_import_previous_9_column_format_still_works(client):
    csrf = login(client)
    wb = _load(ANTERIOR)
    ws = wb["Inventario_Tablets"]
    ws["A9"], ws["D9"], ws["E9"], ws["F9"], ws["G9"], ws["H9"] = "VP-TAB-010", "SN-OLD", "Sellado", "ADIPACK MDM", "Cable USB", "Operativo"
    buf = io.BytesIO()
    wb.save(buf)
    msg = import_file(client, csrf, buf.getvalue(), "viejo.xlsx")
    assert "nuevo" in msg and "Revisa" not in msg, msg
    t = assets_by_code()["VP-TAB-010"]
    assert (t.serial, t.software_serial, t.area, t.software, t.accessories) == ("SN-OLD", "", "Sellado", "ADIPACK MDM", "Cable USB")


def test_import_unknown_area_and_status_are_reported(client):
    csrf = login(client)
    wb = _load(EMPRESA)
    ws = wb["Inventario_Tablets"]
    ws["F9"], ws["I10"] = "LAMINADO", "Perdida"
    buf = io.BytesIO()
    wb.save(buf)
    msg = import_file(client, csrf, buf.getvalue())
    assert "VP-TAB-001: área «LAMINADO» no reconocida, quedó S/A" in msg and "VP-TAB-002: estatus «Perdida»" in msg, msg
    a = assets_by_code()
    assert a["VP-TAB-001"].area == "S/A" and a["VP-TAB-002"].status == "Operativo"


def test_import_without_headers_does_not_touch_inventory(client):
    csrf = login(client)
    wb = openpyxl.Workbook()
    wb.active.title = "Inventario_Tablets"
    wb.active["A1"], wb.active["B1"] = "VP-TAB-001", "algo"
    buf = io.BytesIO()
    wb.save(buf)
    msg = import_file(client, csrf, buf.getvalue())
    assert "no encontré los encabezados" in msg and assets_by_code() == {}


def test_sa_rules_bitacora_and_acta(client):
    csrf = login(client)
    import_file(client, csrf, EMPRESA)
    a = assets_by_code()
    w = mk_worker(client, csrf, name="Carlos Mora", ced="V-15111222", area="Sellado")
    # S/A no se puede entregar en la bitácora
    r = deliver(client, csrf, a["VP-TAB-003"].id, w)
    assert "S/A (sin asignar)" in flash_of(r)
    assert "(S/A: sin asignar)" in client.get("/bitacora").text
    # Con área asignada sí
    r = deliver(client, csrf, a["VP-TAB-001"].id, w, area="Impresión")
    assert "Entrega registrada" in flash_of(r)
    # El acta de una tablet S/A le asigna el área (y la persona)
    r = client.post("/actas/save", data={"csrf": csrf, "worker_id": w, "asset_id": a["VP-TAB-003"].id, "area": "Sellado",
                                         "condition": "Nuevo", "date": "2026-11-17"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/actas/"), flash_of(r)
    t3 = assets_by_code()["VP-TAB-003"]
    assert t3.area == "Sellado" and not t3.unassigned
    inv = client.get("/inventario").text
    assert "Carlos Mora" in inv
    # Acta de Mantenimiento: casilla «Mantenimiento» marcada además de las 3 del formato
    r = client.post("/actas/save", data={"csrf": csrf, "worker_id": w, "asset_id": a["VP-TAB-002"].id, "area": "",
                                         "condition": "Óptimo", "date": "2026-11-17"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/actas/"), flash_of(r)
    from app.models import HandoverAct
    with db() as s:
        act = s.scalar(select(HandoverAct).where(HandoverAct.asset_code == "VP-TAB-002"))
    assert act.area == "Mantenimiento"
    acta = _load(client.get(f"/actas/{act.id}/acta.xlsx").content)["Acta_Entrega_Individual"]
    assert "[ X ] Mantenimiento" in acta["B10"].value and "[   ] Extrusión" in acta["B10"].value
    assert "Mantenimiento" in client.get(f"/actas/{act.id}").text
    # Un equipo con área no cambia de área por un acta
    assert assets_by_code()["VP-TAB-002"].area == "Mantenimiento"


def test_form_keeps_free_text_accessories_and_software_serial(client):
    csrf = login(client)
    aid = mk_asset(client, csrf, code="VP-TAB-020", area="S/A", accessories="FORRO + VIDRIO TEMPLADO",
                   software_serial="abc123xyz")
    a = assets_by_code()["VP-TAB-020"]
    assert a.accessories == "FORRO + VIDRIO TEMPLADO" and a.software_serial == "abc123xyz" and a.area == "S/A"
    page = client.get(f"/inventario/{aid}").text
    assert 'value="FORRO + VIDRIO TEMPLADO"' in page and "S/A · Sin asignar" in page
    r = client.post("/inventario/save", data={"csrf": csrf, "id": aid, "code": "VP-TAB-020", "area": "Cocina",
                                              "status": "Operativo"})
    assert "Área inválida" in flash_of(r)


def test_blank_acta_formula_reads_software_column(client):
    login(client)
    wb = _load(client.get("/formatos/bitacora-en-blanco.xlsx").content)
    b15 = wb["Acta_Entrega_Individual"]["B15"].value
    assert "Inventario_Tablets!$A$9:$G$58,7," in b15  # «Software de Control» es la columna G
    assert wb["Inventario_Tablets"]["E8"].value == "Serial del Software"
    assert "$A$9:$D$58,4," in wb["Control_Turnos_Tablets"]["C11"].value  # «Serial Fisico» sigue en D


def test_upgrade_adds_column_to_existing_database(tmp_path):
    """Base de la v1.2 (sin «software_serial»): al arrancar se agrega la columna y los datos se conservan."""
    from sqlalchemy import create_engine, inspect, text

    from app import models  # noqa: F401
    from app.db import Base, create_schema
    eng = create_engine(f"sqlite:///{tmp_path / 'v12.db'}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE assets DROP COLUMN software_serial"))
        c.execute(text("INSERT INTO assets (code, brand, model, serial, area, software, accessories, status, notes, "
                       "created_at, updated_at) VALUES ('VP-TAB-001','Xiaomi','Pad','SN','','','','Operativo','', "
                       "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    assert "software_serial" not in {c["name"] for c in inspect(eng).get_columns("assets")}
    create_schema(eng)
    create_schema(eng)  # dos veces: no falla
    assert "software_serial" in {c["name"] for c in inspect(eng).get_columns("assets")}
    with eng.connect() as c:
        assert c.execute(text("SELECT code, software_serial FROM assets")).one() == ("VP-TAB-001", "")
