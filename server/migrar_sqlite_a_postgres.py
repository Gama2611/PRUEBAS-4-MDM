#!/usr/bin/env python3
"""Copia los datos de la base SQLite de prueba (data/mdm.db) a PostgreSQL.

Úsalo SÓLO si ya usaste el MDM con SQLite y quieres conservar equipos, políticas, apps y tokens.
Requisitos: haber ejecutado antes crear_base_datos.py y que el MDM esté APAGADO.

    python migrar_sqlite_a_postgres.py                 # usa data/mdm.db y MDM_DATABASE_URL de .env
    python migrar_sqlite_a_postgres.py --forzar        # vacía PostgreSQL antes de copiar
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from app import models  # noqa: E402,F401
from app.config import get_settings  # noqa: E402
from app.db import Base, create_schema  # noqa: E402

# Orden que respeta las llaves foráneas
TABLES = ["admins", "policies", "apps", "enrollment_tokens", "devices", "commands", "audit_log",
          "settings", "workers", "assets", "shift_logs", "handover_acts",  # control de equipos (v1.2+)
          "notices", "notice_deliveries"]  # avisos MQTT (v1.4+)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", type=Path, default=HERE / "data" / "mdm.db")
    ap.add_argument("--forzar", action="store_true", help="Borra los datos que ya tenga PostgreSQL")
    a = ap.parse_args()
    dst_url = get_settings().database_url
    if not dst_url.startswith("postgresql"):
        print("✗ MDM_DATABASE_URL no es PostgreSQL: ejecuta primero crear_base_datos.py")
        return 2
    if not a.sqlite.exists():
        print(f"✗ No existe {a.sqlite}: no hay nada que migrar.")
        return 2

    src = create_engine(f"sqlite:///{a.sqlite}")
    dst = create_engine(dst_url)
    create_schema(src)  # una base SQLite de una versión anterior queda con todas las tablas y columnas
    create_schema(dst)
    with dst.begin() as d:
        used = {t: d.execute(select(func.count()).select_from(Base.metadata.tables[t])).scalar() for t in TABLES}
        if any(used.values()) and not a.forzar:
            print(f"✗ PostgreSQL ya tiene datos {used}. Usa --forzar para reemplazarlos.")
            return 1
        if any(used.values()):
            d.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    with src.connect() as s, dst.begin() as d:
        for t in TABLES:
            table = Base.metadata.tables[t]
            rows = [dict(r._mapping) for r in s.execute(select(table))]
            if rows:
                d.execute(table.insert(), rows)
            # Ajusta el contador de ids para que los nuevos registros no choquen
            if "id" in table.c:  # «settings» usa la clave como identificador, no tiene contador
                d.execute(text(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                               f"COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"))
            print(f"  ✓ {t}: {len(rows)} filas")
    print("✓ Migración terminada. Los archivos APK siguen en data/apks (no cambian).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
