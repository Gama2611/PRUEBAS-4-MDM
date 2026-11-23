from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSONB en PostgreSQL (binario, indexable); JSON normal en SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Policy(Base):
    """Perfil de configuración. `settings` se valida con AndroidPolicy / PCPolicy."""
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    platform: Mapped[str] = mapped_column(String(16))  # android | windows | linux
    description: Mapped[str] = mapped_column(String(255), default="")
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="policy")


class App(Base):
    """APK (o instalador) subido al servidor."""
    __tablename__ = "apps"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_name: Mapped[str] = mapped_column(String(255), index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    version_name: Mapped[str] = mapped_column(String(64), default="")
    version_code: Mapped[int] = mapped_column(BigInteger, default=0)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    is_agent: Mapped[bool] = mapped_column(Boolean, default=False)  # APK del agente MDM
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"))
    name_prefix: Mapped[str] = mapped_column(String(50), default="TAB")
    max_uses: Mapped[int] = mapped_column(Integer, default=0)  # 0 = ilimitado
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    policy: Mapped[Policy] = relationship()


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(16))
    hardware_id: Mapped[str] = mapped_column(String(128), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    policy_id: Mapped[int | None] = mapped_column(ForeignKey("policies.id", ondelete="SET NULL"), nullable=True)
    manufacturer: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    os_version: Mapped[str] = mapped_column(String(64), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    location: Mapped[str] = mapped_column(String(100), default="")  # p.ej. "Extrusión L3"
    status: Mapped[dict] = mapped_column(JSONType, default=dict)  # batería, IP, wifi, apps...
    applied_policy_version: Mapped[int] = mapped_column(Integer, default=0)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)

    policy: Mapped[Policy | None] = relationship(back_populates="devices")
    commands: Mapped[list["Command"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", order_by="Command.id.desc()"
    )


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    # pending -> sent -> done | failed ; cancelled
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    device: Mapped[Device] = relationship(back_populates="commands")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")


# =====================================================================
#  Control de equipos (bitácora de turnos, inventario, actas) — v1.2
#  Tablas nuevas: no modifican las del MDM.
# =====================================================================
class Setting(Base):
    """Datos de la empresa para los formatos (nombre, RIF, ciudad...)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Asset(Base):
    """Equipo del inventario (hoja Inventario_Tablets). Puede enlazarse a un equipo del MDM."""
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # Código activo, ej. VP-TAB-001
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), unique=True, nullable=True)
    brand: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")            # Serial físico (etiqueta)
    software_serial: Mapped[str] = mapped_column(String(64), default="", server_default="")  # Serial del Software
    area: Mapped[str] = mapped_column(String(40), default="")              # "S/A" o vacío = sin asignar
    software: Mapped[str] = mapped_column(String(120), default="")
    accessories: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(40), default="Operativo")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    device: Mapped["Device | None"] = relationship()

    # Si el campo está vacío y el equipo está enlazado al MDM, se usa el dato que reporta el MDM.
    @property
    def eff_brand(self) -> str:
        return self.brand or (self.device.manufacturer if self.device else "")

    @property
    def eff_model(self) -> str:
        return self.model or (self.device.model if self.device else "")

    @property
    def eff_software_serial(self) -> str:
        """Serial del Software: el escrito en el inventario o, si está vacío, el que reporta el MDM."""
        return self.software_serial or (self.device.serial if self.device else "")

    @property
    def eff_serial(self) -> str:
        """Serial para la bitácora y el acta: el físico; si no se cargó, el del software."""
        return self.serial or self.eff_software_serial

    @property
    def unassigned(self) -> bool:
        """S/A: sin área asignada y, por lo tanto, sin personal asignado."""
        return not self.area or self.area == "S/A"


class Worker(Base):
    """Personal que recibe equipos (operadores, supervisores)."""
    __tablename__ = "workers"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    id_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # Cédula de identidad
    position: Mapped[str] = mapped_column(String(80), default="")
    shift: Mapped[str] = mapped_column(String(40), default="")
    area: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShiftLog(Base):
    """Fila de la hoja Control_Turnos_Tablets: entrega y devolución de un equipo en un turno."""
    __tablename__ = "shift_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True)
    asset_code: Mapped[str] = mapped_column(String(40))  # copia: el historial no cambia si se edita el inventario
    serial: Mapped[str] = mapped_column(String(64), default="")
    area: Mapped[str] = mapped_column(String(40))
    shift: Mapped[str] = mapped_column(String(40))
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    operator_name: Mapped[str] = mapped_column(String(120))
    operator_id_number: Mapped[str] = mapped_column(String(20), default="")
    delivered_at: Mapped[str] = mapped_column(String(5))  # HH:MM
    returned_at: Mapped[str | None] = mapped_column(String(5), nullable=True)
    return_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    returned_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    asset: Mapped["Asset | None"] = relationship()


class HandoverAct(Base):
    """Acta de entrega individual (hoja Acta_Entrega_Individual). Guarda copia de los datos firmados."""
    __tablename__ = "handover_acts"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    date: Mapped[date] = mapped_column(Date)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    asset_code: Mapped[str] = mapped_column(String(40))
    brand_model: Mapped[str] = mapped_column(String(130), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    software: Mapped[str] = mapped_column(String(120), default="")
    worker_name: Mapped[str] = mapped_column(String(120))
    worker_id_number: Mapped[str] = mapped_column(String(20))
    worker_position: Mapped[str] = mapped_column(String(80), default="")
    worker_shift: Mapped[str] = mapped_column(String(40), default="")
    area: Mapped[str] = mapped_column(String(40), default="")
    condition: Mapped[str] = mapped_column(String(20), default="Óptimo")
    accessories: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    delivered_by_name: Mapped[str] = mapped_column(String(120), default="")
    delivered_by_id_number: Mapped[str] = mapped_column(String(20), default="")
    delivered_by_position: Mapped[str] = mapped_column(String(80), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------------- Avisos por MQTT (v1.4)
class Notice(Base):
    """Aviso enviado a las tablets por MQTT (red local, sin Internet)."""
    __tablename__ = "notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="normal")    # normal | alta | urgente
    target_kind: Mapped[str] = mapped_column(String(16), default="todas")  # todas | area | tablet
    target_value: Mapped[str] = mapped_column(String(80), default="")
    target_label: Mapped[str] = mapped_column(String(160), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list["NoticeDelivery"]] = relationship(
        back_populates="notice", cascade="all, delete-orphan", order_by="NoticeDelivery.device_name")


class NoticeDelivery(Base):
    """Estado de un aviso en una tablet: pendiente → recibido → leído."""
    __tablename__ = "notice_deliveries"
    __table_args__ = (UniqueConstraint("notice_id", "device_id", name="uq_notice_device"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    device_name: Mapped[str] = mapped_column(String(100), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notice: Mapped[Notice] = relationship(back_populates="deliveries")
    device: Mapped[Device] = relationship()
