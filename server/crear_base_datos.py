#!/usr/bin/env python3
"""Crea la base de datos PostgreSQL `adipack_mdm` para ADIPACK MDM.

Hace todo lo necesario, de forma segura y repetible (se puede ejecutar varias veces):
  1. Se conecta como superusuario (postgres) — la contraseña se pide por teclado y no se guarda.
  2. Crea el usuario de la aplicación `adipack_mdm_app` (sin privilegios de superusuario)
     con una contraseña aleatoria de 32 caracteres.
  3. Crea la base `adipack_mdm` (UTF8) cuyo dueño es ese usuario.
  4. Cierra el acceso público a la base y al esquema `public`.
  5. Ajusta parámetros por usuario: zona horaria UTC y tiempos máximos de espera.
  6. Opcional (--lectura): crea `adipack_mdm_lectura`, sólo lectura, para reportes/BI.
  7. Crea las tablas del MDM conectándose ya como `adipack_mdm_app` y verifica todo.
  8. Escribe MDM_DATABASE_URL en server/.env (conserva el resto del archivo).

Uso:
    python crear_base_datos.py                       # PostgreSQL en esta PC (localhost:5432)
    python crear_base_datos.py --host 192.168.1.20   # PostgreSQL en otro servidor
    python crear_base_datos.py --lectura             # además, usuario de sólo lectura
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

from envfile import ensure_env, read_env, set_env_value  # noqa: E402

try:
    import psycopg
    from psycopg import sql
except ImportError:
    print("Falta la librería psycopg. Ejecuta:  pip install \"psycopg[binary]\"")
    sys.exit(2)

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


# ------------------------------------------------------------------ .env
def existing_app_password(user: str, host: str, port: int, db: str) -> str | None:
    """Si .env ya apunta a esta misma base con este usuario, reutiliza su contraseña."""
    url = read_env().get("MDM_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    u = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    if u.username == user and (u.hostname or "") == host and (u.port or 5432) == port and u.path.lstrip("/") == db:
        return unquote(u.password or "") or None
    return None


# ------------------------------------------------------------------ pasos
def step(msg: str) -> None:
    print(f"→ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Crea la base PostgreSQL de ADIPACK MDM")
    ap.add_argument("--host", help="Por defecto MDM_PG_HOST de .env, o localhost")
    ap.add_argument("--port", type=int, help="Por defecto MDM_PG_PORT de .env, o 5432")
    ap.add_argument("--superuser", help="Usuario administrador de PostgreSQL (MDM_PG_SUPERUSER, o postgres)")
    ap.add_argument("--db", help="Nombre de la base (MDM_PG_DATABASE, o adipack_mdm)")
    ap.add_argument("--app-user", help="Usuario del MDM (MDM_PG_APP_USER, o adipack_mdm_app)")
    ap.add_argument("--lectura", action="store_true", help="Crear también el usuario adipack_mdm_lectura")
    ap.add_argument("--nueva-clave", action="store_true", help="Generar una contraseña nueva para el usuario de la app")
    ap.add_argument("--conservar-clave-postgres", action="store_true",
                    help="No borrar MDM_PG_SUPERUSER_PASSWORD de .env al terminar")
    a = ap.parse_args()

    # Prioridad: línea de comandos > server\.env > valores por defecto
    if ensure_env():
        print("  Escribe la contraseña de 'postgres' en server\\.env (línea MDM_PG_SUPERUSER_PASSWORD=)"
              " con el Bloc de notas y vuelve a ejecutar, o escríbela aquí abajo.")
    env = read_env()
    a.host = a.host or env.get("MDM_PG_HOST") or "localhost"
    try:
        a.port = a.port or int(env.get("MDM_PG_PORT") or 5432)
    except ValueError:
        print(f"✗ MDM_PG_PORT inválido en .env: {env.get('MDM_PG_PORT')!r}")
        return 2
    a.superuser = a.superuser or env.get("MDM_PG_SUPERUSER") or "postgres"
    a.db = a.db or env.get("MDM_PG_DATABASE") or "adipack_mdm"
    a.app_user = a.app_user or env.get("MDM_PG_APP_USER") or "adipack_mdm_app"

    for n in (a.db, a.app_user):
        if not NAME_RE.match(n):
            print(f"Nombre inválido: {n!r} (usa minúsculas, números y _)")
            return 2

    pass_from_env = False
    su_pass = os.environ.get("PGPASSWORD_SUPERUSER", "")
    if not su_pass and env.get("MDM_PG_SUPERUSER_PASSWORD"):
        su_pass, pass_from_env = env["MDM_PG_SUPERUSER_PASSWORD"], True
        print(f"→ Usando la contraseña de '{a.superuser}' escrita en server\\.env")
    if not su_pass:
        print(f"Contraseña del usuario '{a.superuser}' de PostgreSQL en {a.host}:{a.port}")
        print("  (Por seguridad NO se ve nada mientras escribes: escríbela completa y pulsa Enter.)")
        try:
            su_pass = getpass.getpass("  Contraseña: ")
        except (EOFError, KeyboardInterrupt):
            su_pass = ""
            print()
    if not su_pass:
        print("✗ No se indicó la contraseña. Escríbela en server\\.env (MDM_PG_SUPERUSER_PASSWORD=) y vuelve a ejecutar.")
        return 2

    # 1. Conexión como superusuario
    step(f"Conectando a PostgreSQL {a.host}:{a.port} como {a.superuser}")
    try:
        admin = psycopg.connect(host=a.host, port=a.port, user=a.superuser, password=su_pass,
                                dbname="postgres", autocommit=True, connect_timeout=10,
                                application_name="adipack_mdm_setup")
    except psycopg.OperationalError as exc:
        print(f"✗ No pude conectar: {str(exc).strip()}\n"
              f"  Revisa que el servicio PostgreSQL esté iniciado, el puerto ({a.port}) y la contraseña de "
              f"'{a.superuser}' (MDM_PG_SUPERUSER_PASSWORD en server\\.env).")
        return 1
    with admin:
        ok(f"Conectado (PostgreSQL {admin.execute('SHOW server_version').fetchone()[0]})")
        is_super = admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        if not is_super:
            print(f"✗ '{a.superuser}' no es superusuario; usa el usuario postgres.")
            return 1

        # 2. Usuario de la aplicación
        step(f"Usuario de la aplicación '{a.app_user}'")
        app_pass = None if a.nueva_clave else existing_app_password(a.app_user, a.host, a.port, a.db)
        role_exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (a.app_user,)).fetchone()
        if role_exists and app_pass:
            ok("Ya existe y .env tiene su contraseña: se reutiliza")
        else:
            app_pass = secrets.token_urlsafe(24)  # ~32 caracteres, sin símbolos problemáticos
            if role_exists:
                admin.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(a.app_user), sql.Literal(app_pass)))
                ok("Ya existía: se le asignó una contraseña nueva")
            else:
                admin.execute(sql.SQL(
                    "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION CONNECTION LIMIT 50").format(sql.Identifier(a.app_user), sql.Literal(app_pass)))
                ok("Creado (sin privilegios de administrador, máx. 50 conexiones)")

        # 3. Base de datos
        step(f"Base de datos '{a.db}'")
        db_row = admin.execute(
            "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s",
            (a.db,)).fetchone()
        if db_row is None:
            admin.execute(sql.SQL("CREATE DATABASE {} WITH OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(a.db), sql.Identifier(a.app_user)))
            ok("Creada en UTF8")
        else:
            owner, enc = db_row
            if enc != "UTF8":
                print(f"✗ La base '{a.db}' ya existe con codificación {enc}; debe ser UTF8. "
                      "Bórrala (si está vacía) o usa otro nombre con --db.")
                return 1
            if owner != a.app_user:
                admin.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(a.db), sql.Identifier(a.app_user)))
                ok(f"Ya existía; dueño cambiado de {owner} a {a.app_user}")
            else:
                ok("Ya existía (UTF8, dueño correcto): se conservan sus datos")

        # 4. Permisos a nivel de base
        step("Permisos")
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(a.db)))
        admin.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(a.db), sql.Identifier(a.app_user)))
        ok("Sólo el usuario de la app (y los administradores) pueden conectarse")

        # 5. Parámetros del usuario dentro de esta base
        for param, value in (("timezone", "UTC"),
                             ("statement_timeout", "60s"),
                             ("idle_in_transaction_session_timeout", "5min"),
                             ("lock_timeout", "15s")):
            admin.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} TO {}").format(
                sql.Identifier(a.app_user), sql.Identifier(a.db), sql.Identifier(param), sql.Literal(value)))
        ok("Zona horaria UTC y tiempos máximos (consulta 60 s, bloqueo 15 s, transacción inactiva 5 min)")

        # 6. Usuario de sólo lectura (opcional)
        ro_user, ro_pass = "adipack_mdm_lectura", None
        if a.lectura:
            step(f"Usuario de sólo lectura '{ro_user}'")
            ro_pass = secrets.token_urlsafe(18)
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ro_user,)).fetchone()
            verb = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else \
                   "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 10"
            admin.execute(sql.SQL(verb).format(sql.Identifier(ro_user), sql.Literal(ro_pass)))
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(a.db), sql.Identifier(ro_user)))
            admin.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} SET default_transaction_read_only TO on").format(
                sql.Identifier(ro_user), sql.Identifier(a.db)))
            ok("Creado")

    # 4b. Esquema public dentro de la base (como superusuario, conectado a la base nueva)
    with psycopg.connect(host=a.host, port=a.port, user=a.superuser, password=su_pass, dbname=a.db,
                         autocommit=True, connect_timeout=10) as dbadmin:
        dbadmin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        dbadmin.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(a.app_user)))
        dbadmin.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(a.app_user)))
        if a.lectura:
            dbadmin.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(ro_user)))
            dbadmin.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(ro_user)))
            # Las tablas que cree la app en el futuro también serán legibles:
            dbadmin.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}")
                            .format(sql.Identifier(a.app_user), sql.Identifier(ro_user)))
        ok("Esquema public: dueño la app, sin acceso para otros usuarios")

    # 7. Tablas, conectándose YA como el usuario de la aplicación
    url = f"postgresql+psycopg://{quote(a.app_user, safe='')}:{quote(app_pass, safe='')}@{a.host}:{a.port}/{a.db}"
    step("Creando tablas del MDM como el usuario de la app")
    os.environ["MDM_DATABASE_URL"] = url
    from sqlalchemy import inspect, text

    from app import models  # noqa: F401 - registra los modelos
    from app.db import Base, create_schema, init_engine
    engine = init_engine(url)
    create_schema(engine)
    with engine.connect() as conn:
        who = conn.execute(text("SELECT current_user, current_database(), current_setting('TimeZone')")).one()
    tables = sorted(inspect(engine).get_table_names())
    expected = sorted(Base.metadata.tables)
    missing = set(expected) - set(tables)
    if missing:
        print(f"✗ Faltan tablas: {missing}")
        return 1
    ok(f"Conectado como {who[0]} a {who[1]} (zona {who[2]}); tablas: {', '.join(tables)}")
    if a.lectura:
        with psycopg.connect(host=a.host, port=a.port, user=a.superuser, password=su_pass, dbname=a.db,
                             autocommit=True) as dbadmin:
            dbadmin.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(ro_user)))
    engine.dispose()

    # 8. .env
    step("Guardando la conexión en server/.env")
    set_env_value("MDM_DATABASE_URL", url)
    ok(f"MDM_DATABASE_URL apunta a {a.host}:{a.port}/{a.db} (contraseña guardada sólo en .env)")

    if pass_from_env and not a.conservar_clave_postgres:
        set_env_value("MDM_PG_SUPERUSER_PASSWORD", "")
        ok("Por seguridad, borré la contraseña de 'postgres' de server\\.env (el MDM no la necesita)")

    print(f"\n✓ LISTO. Base de datos PostgreSQL '{a.db}' creada y configurada.")
    print("  Ahora inicia el MDM con INICIAR_MDM.bat.")
    if ro_pass:
        print(f"\n  Usuario de sólo lectura para reportes: {ro_user}  contraseña: {ro_pass}")
        print("  (Anótala ahora: no se guarda en ningún archivo.)")
    print("\n  IMPORTANTE: respalda la base con RESPALDAR_BD.bat (ver manual).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
