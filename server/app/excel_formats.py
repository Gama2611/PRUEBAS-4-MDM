"""Exporta los datos a la plantilla oficial «Bitácora Control Turnos Tablets».

Se usa el propio Excel de la empresa como plantilla (app/formatos/…xlsx): los estilos, anchos,
celdas combinadas, fórmulas de resumen, validaciones y formatos condicionales salen idénticos.
Si hay más filas que las del formato (200 movimientos / 50 equipos), la tabla se amplía copiando
el estilo de la última fila y se ajustan fórmulas, listas desplegables y colores condicionales.
"""
from __future__ import annotations

import copy
import io
import re
import warnings
from pathlib import Path

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation

from .control import ACCESORIOS, CONDICIONES, MESES, acta_area_options, area_excel, hora_obj

TEMPLATE = Path(__file__).parent / "formatos" / "Bitacora_Control_Turnos_Tablets.xlsx"
CTRL, INV, ACTA = "Control_Turnos_Tablets", "Inventario_Tablets", "Acta_Entrega_Individual"
CTRL_FIRST, CTRL_LAST = 10, 209   # filas de datos del formato original
INV_FIRST, INV_LAST = 9, 58
# Columnas de la hoja Inventario_Tablets (formato v1.3, igual al Excel de la empresa)
INV_COLUMNS = ["Código Activo", "Marca", "Modelo", "Serial Fisico", "Serial del Software", "Área Asignada",
               "Software de Control", "Accesorios Incluidos", "Estatus Actual", "Observaciones Técnicas"]
INV_NCOLS = len(INV_COLUMNS)
ORIG = {"name": "INVERSIONES VENPACK, C.A.", "rif": "J-29582172-7", "plant": "Planta Guarenas", "city": "Guarenas",
        "short": "Venpack"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _load():
    with warnings.catch_warnings():
        # El formato trae una lista desplegable «extendida» que openpyxl no lee; se vuelve a crear abajo.
        warnings.simplefilter("ignore", UserWarning)
        return openpyxl.load_workbook(TEMPLATE)


def _apply_company(wb, comp: dict) -> None:
    """Sustituye razón social, RIF, planta, ciudad y nombre corto en los textos fijos del formato."""
    pairs = [
        (ORIG["name"], comp["company_name"]),
        (ORIG["rif"], comp["company_rif"]),
        (ORIG["plant"], comp["plant"]),
        (f"Código Activo {ORIG['short']}", f"Código Activo {comp['company_short']}".strip()),
        (f"En {ORIG['city']},", f"En {comp['city']},"),
    ]
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and not c.value.startswith("="):
                    v = c.value
                    for a, b in pairs:
                        if a != b:
                            v = v.replace(a, b)
                    c.value = v


def _add_logo(ws, logo: Path | None) -> None:
    """Coloca el logo en A1 dentro del mismo recuadro que el formato original (≈155 × 70 px)."""
    if not logo:
        return  # sin logo cargado se conserva el del formato original (hoja Inventario_Tablets)
    ws._images = []  # el logo de la empresa reemplaza al del formato
    img = XLImage(str(logo))
    box_w, box_h = 155, 70
    ratio = min(box_w / img.width, box_h / img.height)
    img.width, img.height = int(img.width * ratio), int(img.height * ratio)
    # Mismo margen que el logo del formato original (desplazado 3 px a la derecha y 5 px hacia abajo)
    emu = 9525
    img.anchor = OneCellAnchor(_from=AnchorMarker(col=0, colOff=3 * emu, row=0, rowOff=5 * emu),
                               ext=XDRPositiveSize2D(img.width * emu, img.height * emu))
    ws.add_image(img)


def _extend(ws, first_data: int, last_tpl: int, last_needed: int, ncols: int, summary_rows: range) -> int:
    """Amplía la tabla si hay más filas que las del formato. Devuelve la última fila final."""
    if last_needed <= last_tpl:
        return last_tpl
    height = ws.row_dimensions[last_tpl].height
    for r in range(last_tpl + 1, last_needed + 1):
        for col in range(1, ncols + 1):
            src = ws.cell(last_tpl, col)
            dst = ws.cell(r, col)
            dst._style = copy.copy(src._style)
        ws.row_dimensions[r].height = height
    pat = re.compile(rf"(:\$?[A-Z]{{1,3}}\$?){last_tpl}\b")
    for r in summary_rows:  # fórmulas de resumen (CONTARA / CONTAR.SI)
        for col in range(1, ncols + 1):
            c = ws.cell(r, col)
            if isinstance(c.value, str) and c.value.startswith("="):
                c.value = pat.sub(rf"\g<1>{last_needed}", c.value)
    for dv in ws.data_validations.dataValidation:
        dv.sqref = MultiCellRange(pat.sub(rf"\g<1>{last_needed}", str(dv.sqref)))
    new_cf = ConditionalFormattingList()
    for cf in ws.conditional_formatting:
        rng = pat.sub(rf"\g<1>{last_needed}", str(cf.sqref))
        for rule in cf.rules:
            new_cf.add(rng, rule)
    ws.conditional_formatting = new_cf
    return last_needed


def _inv_range_fix(text: str, inv_last: int) -> str:
    """Ajusta referencias Inventario_Tablets!$A$9:$X$58 al nuevo final del inventario."""
    return re.sub(r"(Inventario_Tablets!\$[A-Z]\$9:\$[A-Z]\$)58\b", rf"\g<1>{inv_last}", text)


def _serial_formula(row: int, inv_last: int) -> str:
    return (f'=IF(B{row}="","",IFERROR(VLOOKUP(B{row},Inventario_Tablets!$A$9:$D${inv_last},4,FALSE),'
            f'"No registrado"))')


def _fill_inventory(ws, assets) -> int:
    inv_last = _extend(ws, INV_FIRST, INV_LAST, INV_FIRST + len(assets) - 1, INV_NCOLS, range(1, INV_FIRST))
    for r in range(INV_FIRST, inv_last + 1):
        for col in range(1, INV_NCOLS + 1):
            ws.cell(r, col).value = None
    code_font = copy.copy(ws.cell(INV_FIRST, 1).font)  # el formato trae el código en negrita
    for i, a in enumerate(assets):
        r = INV_FIRST + i
        ws.cell(r, 1).font = copy.copy(code_font)
        vals = [a.code, a.eff_brand, a.eff_model, a.serial, a.eff_software_serial, area_excel(a.area), a.software,
                a.accessories, a.status, a.notes]
        for col, v in enumerate(vals, start=1):
            ws.cell(r, col).value = v if v not in ("", None) else None
    # Área de impresión: hasta la última fila con datos (como el archivo de la empresa)
    last_row = INV_FIRST + len(assets) - 1 if assets else inv_last
    ws.print_area = f"A1:J{last_row}"
    return inv_last


def _fill_control(ws, logs, inv_last: int) -> int:
    ctrl_last = _extend(ws, CTRL_FIRST, CTRL_LAST, CTRL_FIRST + len(logs) - 1, 13, range(1, CTRL_FIRST))
    for r in range(CTRL_FIRST, ctrl_last + 1):
        for col in range(1, 14):
            ws.cell(r, col).value = None
        ws.cell(r, 3).value = _serial_formula(r, inv_last)  # filas vacías: la fórmula original
    for i, lg in enumerate(logs):
        r = CTRL_FIRST + i
        ws.cell(r, 1).value = lg.date
        ws.cell(r, 2).value = lg.asset_code
        ws.cell(r, 3).value = lg.serial or "No registrado"  # valor real (no depende de recalcular)
        ws.cell(r, 4).value = lg.area
        ws.cell(r, 5).value = lg.shift
        ws.cell(r, 6).value = lg.operator_name
        ws.cell(r, 7).value = lg.operator_id_number or None
        ws.cell(r, 8).value = hora_obj(lg.delivered_at)
        ws.cell(r, 10).value = hora_obj(lg.returned_at)
        ws.cell(r, 11).value = lg.return_status or None
        ws.cell(r, 13).value = lg.notes or None
        # I (Firma Operador) y L (Firma Supervisor) quedan en blanco para firmar en papel.
    # Lista desplegable de códigos (la del formato original no la conserva openpyxl): se recrea.
    dv = DataValidation(type="list", formula1=f"Inventario_Tablets!$A${INV_FIRST}:$A${inv_last}", allow_blank=True)
    dv.add(f"B{CTRL_FIRST}:B{ctrl_last}")
    ws.add_data_validation(dv)
    return ctrl_last


def _finish(wb, logo: Path | None) -> bytes:
    for ws in wb.worksheets:
        _add_logo(ws, logo)
    wb.calculation.fullCalcOnLoad = True  # Excel recalcula los totales al abrir
    wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_bitacora(logs, assets, comp: dict, logo: Path | None) -> bytes:
    """Libro completo: Control_Turnos_Tablets + Inventario_Tablets + Acta_Entrega_Individual (en blanco)."""
    wb = _load()
    _apply_company(wb, comp)
    inv_last = _fill_inventory(wb[INV], assets)
    _fill_control(wb[CTRL], logs, inv_last)
    acta = wb[ACTA]
    for coord in ("B14", "E14", "B15"):
        if isinstance(acta[coord].value, str):
            acta[coord].value = _inv_range_fix(acta[coord].value, inv_last)
    dv = DataValidation(type="list", formula1=f"Inventario_Tablets!$A${INV_FIRST}:$A${inv_last}", allow_blank=True)
    dv.add("B13")
    acta.add_data_validation(dv)
    return _finish(wb, logo)


def build_inventory(assets, comp: dict, logo: Path | None) -> bytes:
    """Sólo la hoja Inventario_Tablets."""
    wb = _load()
    _apply_company(wb, comp)
    _fill_inventory(wb[INV], assets)
    wb.remove(wb[CTRL])
    wb.remove(wb[ACTA])
    return _finish(wb, logo)


def marks(options, selected, sep: str) -> str:
    """«[ X ] Extrusión     [   ] Impresión» como en el formato."""
    sel = set(selected if isinstance(selected, (list, tuple, set)) else [selected])
    return sep.join(f"[ X ] {o}" if o in sel else f"[   ] {o}" for o in options)


def acta_paragraph(template_text: str, act) -> str:
    d = act.date
    return re.sub(r"a los _+ días del mes de _+ del año \d{4}",
                  f"a los {d.day} días del mes de {MESES[d.month - 1]} del año {d.year}", template_text)


def build_acta(act, comp: dict, logo: Path | None) -> bytes:
    """Hoja Acta_Entrega_Individual llena con los datos del acta."""
    wb = _load()
    _apply_company(wb, comp)
    wb.remove(wb[CTRL])
    wb.remove(wb[INV])
    ws = wb[ACTA]
    ws["A5"].value = acta_paragraph(ws["A5"].value or "", act)
    ws["B8"].value = act.worker_name
    ws["E8"].value = act.worker_id_number
    ws["B9"].value = act.worker_position or None
    ws["E9"].value = act.worker_shift or None
    ws["B10"].value = marks(acta_area_options(act.area), act.area, "     ")
    ws["E10"].value = act.date.strftime("%d / %m / %Y")
    ws["B13"].value = act.asset_code
    ws["B14"].value = act.brand_model or None
    ws["E14"].value = act.serial or None
    ws["B15"].value = act.software or None
    ws["E15"].value = marks(CONDICIONES, act.condition, "    ")
    ws["B16"].value = marks(ACCESORIOS, act.accessories or [], "      ")
    ws["B17"].value = act.notes or None
    if act.delivered_by_name:
        ws["A29"].value = f"Nombre: {act.delivered_by_name}"
    if act.delivered_by_id_number:
        ws["A30"].value = f"C.I.: {act.delivered_by_id_number}"
    if act.delivered_by_position:
        ws["A31"].value = f"Cargo: {act.delivered_by_position}"
    ws["D29"].value = f"Nombre: {act.worker_name}"
    ws["D30"].value = f"C.I.: {act.worker_id_number}"
    if act.worker_position:
        ws["D31"].value = f"Cargo: {act.worker_position}"
    return _finish(wb, logo)


def acta_texts(comp: dict) -> dict:
    """Textos fijos del acta (párrafo y cláusulas) ya con los datos de la empresa, para la versión imprimible."""
    wb = _load()
    _apply_company(wb, comp)
    ws = wb[ACTA]
    return {
        "intro": ws["A5"].value or "",
        "clauses": [ws[f"A{r}"].value or "" for r in range(20, 25)],
        "closing": ws["A26"].value or "",
        "title": ws["C2"].value or "",
        "subtitle": ws["C3"].value or "",
        "header": ws["C1"].value or "",
        "code_label": ws["A13"].value or "Código Activo:",
        "delivered_title": ws["A27"].value or "",
    }
