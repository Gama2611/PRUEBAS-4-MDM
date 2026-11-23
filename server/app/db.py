from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    settings = get_settings()
    url = url or settings.database_url
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        # Pool moderado: 7 tablets + PCs + consola. Reciclar evita conexiones cortadas por firewall/red.
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800, pool_timeout=30,
                      connect_args={"application_name": "adipack_mdm", "connect_timeout": 10})
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # integridad referencial + concurrencia
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine():
    return _engine or init_engine()


def get_db() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_session() -> Session:
    """Sesión independiente (para registrar eventos fuera de una petición, p. ej. al terminar una descarga)."""
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal()


# Columnas agregadas en versiones nuevas a tablas que ya existen en la base del cliente.
# create_all() crea tablas nuevas pero NO agrega columnas, por eso se agregan aquí (una sola vez).
ADDED_COLUMNS = [
    ("assets", "software_serial", "VARCHAR(64) NOT NULL DEFAULT ''"),  # v1.3: «Serial del Software»
]


def upgrade_schema(engine) -> list[str]:
    """Agrega las columnas que falten. Seguro de ejecutar en cada arranque (SQLite y PostgreSQL)."""
    added = []
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if table not in tables:
                continue  # create_all la crea completa
            if column in {c["name"] for c in insp.get_columns(table)}:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            added.append(f"{table}.{column}")
    return added


def create_schema(engine) -> None:
    """Crea las tablas nuevas y actualiza las existentes."""
    upgrade_schema(engine)
    Base.metadata.create_all(engine)
