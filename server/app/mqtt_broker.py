"""Broker MQTT 3.1.1 integrado (sin dependencias), pensado para una red local sin Internet.

Soporta lo que se necesita para avisos confiables:
  * CONNECT con usuario/contraseña (autenticación y permisos por usuario, vía una función externa).
  * PUBLISH QoS 0, 1 y 2 de entrada; entrega a suscriptores con QoS 0 o 1 (QoS 2 se concede como 1).
  * SUBSCRIBE / UNSUBSCRIBE con comodines «+» y «#», mensajes retenidos.
  * Last Will (mensaje de despedida si la conexión se cae), keep-alive, PINGREQ.
  * Sesiones persistentes (clean session = 0): los mensajes QoS 1 se guardan mientras el
    cliente está desconectado y se entregan (o reintentan con DUP) al reconectar.

Es genérico: no sabe nada del MDM. Quién entra y qué puede publicar/suscribir lo decide la función
``authenticate(usuario, contraseña, client_id) -> Identity | None``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("adipack.mqtt.broker")

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14

MAX_PACKET = 256 * 1024          # ningún aviso necesita más
MAX_INFLIGHT = 20                # QoS 1 sin confirmar por cliente
MAX_QUEUE = 1000                 # mensajes guardados por cliente desconectado
MAX_SESSIONS = 2000              # sesiones persistentes recordadas
MAX_WRITE_BUFFER = 2 * 1024 * 1024
CONNECT_TIMEOUT = 10.0


class ProtocolError(Exception):
    pass


# ---------------------------------------------------------------- utilidades del protocolo
def encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 65535:
        raise ValueError("texto demasiado largo")
    return len(b).to_bytes(2, "big") + b


def packet(ptype: int, flags: int, body: bytes = b"") -> bytes:
    return bytes([(ptype << 4) | flags]) + encode_len(len(body)) + body


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def u8(self) -> int:
        if self.i + 1 > len(self.d):
            raise ProtocolError("paquete truncado")
        v = self.d[self.i]
        self.i += 1
        return v

    def u16(self) -> int:
        if self.i + 2 > len(self.d):
            raise ProtocolError("paquete truncado")
        v = int.from_bytes(self.d[self.i:self.i + 2], "big")
        self.i += 2
        return v

    def binary(self) -> bytes:
        n = self.u16()
        if self.i + n > len(self.d):
            raise ProtocolError("paquete truncado")
        v = self.d[self.i:self.i + n]
        self.i += n
        return v

    def string(self) -> str:
        try:
            s = self.binary().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("texto UTF-8 inválido") from exc
        if "\x00" in s:
            raise ProtocolError("texto con carácter nulo")
        return s

    def rest(self) -> bytes:
        v = self.d[self.i:]
        self.i = len(self.d)
        return v

    def done(self) -> bool:
        return self.i >= len(self.d)


def valid_topic(t: str) -> bool:
    return bool(t) and "+" not in t and "#" not in t and "\x00" not in t and len(t.encode()) <= 65535


def valid_filter(f: str) -> bool:
    if not f or "\x00" in f or len(f.encode()) > 65535:
        return False
    parts = f.split("/")
    for i, p in enumerate(parts):
        if "#" in p and (p != "#" or i != len(parts) - 1):
            return False
        if "+" in p and p != "+":
            return False
    return True


def topic_matches(filt: str, topic: str) -> bool:
    """Coincidencia MQTT 3.1.1 (los temas que empiezan con $ no coinciden con comodines al inicio)."""
    if topic.startswith("$") and filt[:1] in ("+", "#"):
        return False
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


# ---------------------------------------------------------------- modelo
@dataclass
class Identity:
    """Quién es el cliente y qué puede hacer. ``can_publish``/``can_subscribe`` reciben tema/filtro."""
    name: str
    can_publish: Callable[[str], bool] = lambda t: True
    can_subscribe: Callable[[str], bool] = lambda f: True


@dataclass
class Message:
    topic: str
    payload: bytes
    qos: int
    retain: bool = False


@dataclass
class Session:
    client_id: str
    clean: bool
    subs: dict = field(default_factory=dict)              # filtro -> qos concedido
    queue: deque = field(default_factory=lambda: deque(maxlen=MAX_QUEUE))  # (Message, qos)
    inflight: OrderedDict = field(default_factory=OrderedDict)  # pid -> (Message, qos)
    next_pid: int = 1
    conn: Optional["Connection"] = None
    last_seen: float = field(default_factory=time.time)

    def alloc_pid(self) -> int:
        for _ in range(65535):
            pid = self.next_pid
            self.next_pid = 1 if pid >= 65535 else pid + 1
            if pid not in self.inflight:
                return pid
        raise RuntimeError("sin identificadores de paquete libres")


class Connection:
    def __init__(self, broker: "Broker", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.broker, self.reader, self.writer = broker, reader, writer
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.session: Optional[Session] = None
        self.identity: Optional[Identity] = None
        self.keepalive = 0
        self.will: Optional[Message] = None
        self.closed = False
        self.qos2_in: set[int] = set()
        self.username = ""

    # -- envío
    def send(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.writer.write(data)
            if self.writer.transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                log.warning("Cliente %s no lee sus mensajes: se desconecta", self.client_id)
                self.abort()
        except Exception:  # noqa: BLE001
            self.abort()

    def abort(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.writer.transport.abort()
            except Exception:  # noqa: BLE001
                pass

    @property
    def client_id(self) -> str:
        return self.session.client_id if self.session else "?"

    # -- lectura
    async def read_packet(self, timeout: Optional[float], expect: int = 0) -> tuple[int, int, bytes]:
        async def _read():
            h = await self.reader.readexactly(1)
            if expect and h[0] != (expect << 4):
                raise ProtocolError("no es un cliente MQTT")
            mult, length = 1, 0
            for _ in range(4):
                b = (await self.reader.readexactly(1))[0]
                length += (b & 0x7F) * mult
                if not b & 0x80:
                    break
                mult *= 128
            else:
                raise ProtocolError("longitud inválida")
            if length > MAX_PACKET:
                raise ProtocolError(f"paquete demasiado grande ({length} bytes)")
            body = await self.reader.readexactly(length) if length else b""
            return h[0] >> 4, h[0] & 0x0F, body

        if timeout:
            return await asyncio.wait_for(_read(), timeout)
        return await _read()


# ---------------------------------------------------------------- broker
class Broker:
    def __init__(self, host: str, port: int,
                 authenticate: Callable[[str, bytes, str], Optional[Identity]],
                 on_event: Callable[[str, dict], None] | None = None):
        self.host, self.port = host, port
        self.authenticate = authenticate
        self.on_event = on_event or (lambda kind, data: None)
        self.sessions: dict[str, Session] = {}
        self.retained: dict[str, Message] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: set[Connection] = set()
        self.started_at = 0.0
        self.counters = {"conexiones": 0, "mensajes_entrada": 0, "mensajes_salida": 0, "rechazos": 0}

    # -- ciclo de vida
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, reuse_address=True)
        sock = self._server.sockets[0].getsockname() if self._server.sockets else None
        if sock:
            self.port = sock[1]
        self.started_at = time.time()
        log.info("Broker MQTT escuchando en %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for c in list(self._conns):
                c.abort()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    def online_clients(self) -> list[dict]:
        out = []
        for s in list(self.sessions.values()):
            c = s.conn
            if c is not None and not c.closed:
                out.append({"client_id": s.client_id, "user": c.username, "peer": c.peer})
        return out

    # -- conexión
    async def _handle(self, reader, writer) -> None:
        conn = Connection(self, reader, writer)
        self._conns.add(conn)
        clean_exit = False
        try:
            if not await self._do_connect(conn):
                return
            while not conn.closed:
                timeout = conn.keepalive * 1.5 if conn.keepalive else None
                ptype, flags, body = await conn.read_packet(timeout)
                if conn.closed or conn.session is None:  # reemplazada por otra conexión del mismo cliente
                    break
                if ptype == DISCONNECT:
                    clean_exit = True
                    break
                self._dispatch(conn, ptype, flags, body)
        except asyncio.TimeoutError:
            log.info("Cliente %s sin actividad (keep-alive vencido)", conn.client_id)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except ProtocolError as exc:
            log.warning("Cliente %s desconectado por error de protocolo: %s", conn.client_id, exc)
        except Exception:  # noqa: BLE001
            log.exception("Error inesperado con el cliente %s", conn.client_id)
        finally:
            self._close(conn, clean_exit)

    async def _do_connect(self, conn: Connection) -> bool:
        try:
            ptype, flags, body = await conn.read_packet(CONNECT_TIMEOUT, expect=CONNECT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ProtocolError):
            conn.abort()
            return False
        if ptype != CONNECT:
            conn.abort()
            return False
        r = _Reader(body)
        proto, level = r.string(), r.u8()
        cflags = r.u8()
        keepalive = r.u16()
        if not ((proto == "MQTT" and level == 4) or (proto == "MQIsdp" and level == 3)):
            conn.send(packet(CONNACK, 0, b"\x00\x01"))  # versión no soportada
            conn.abort()
            return False
        if cflags & 0x01:
            raise ProtocolError("bit reservado en CONNECT")
        clean = bool(cflags & 0x02)
        will_flag, will_qos, will_retain = bool(cflags & 0x04), (cflags >> 3) & 0x03, bool(cflags & 0x20)
        has_pass, has_user = bool(cflags & 0x40), bool(cflags & 0x80)
        if will_qos > 2 or (not will_flag and (will_qos or will_retain)) or (has_pass and not has_user):
            raise ProtocolError("banderas de CONNECT inválidas")
        client_id = r.string()
        will = None
        if will_flag:
            wt = r.string()
            wm = r.binary()
            if not valid_topic(wt):
                raise ProtocolError("tema del Last Will inválido")
            will = Message(wt, wm, min(will_qos, 1), will_retain)
        username = r.string() if has_user else ""
        password = r.binary() if has_pass else b""

        def refuse(code: int) -> bool:
            self.counters["rechazos"] += 1
            conn.send(packet(CONNACK, 0, bytes([0, code])))
            conn.abort()
            self.on_event("rechazado", {"client_id": client_id, "user": username, "peer": conn.peer, "code": code})
            return False

        if not client_id:
            if not clean:
                return refuse(2)  # identificador rechazado
            client_id = "auto-" + uuid.uuid4().hex[:12]
        try:
            identity = self.authenticate(username, password, client_id)
        except Exception:  # noqa: BLE001
            log.exception("Error al autenticar a %s", username)
            identity = None
        if identity is None:
            return refuse(4 if has_user else 5)
        if will is not None and not identity.can_publish(will.topic):
            return refuse(5)

        # Toma de control: si ya hay una conexión con ese client_id, se cierra SIN publicar su Last Will
        old = self.sessions.get(client_id)
        if old is not None and old.conn is not None:
            old.conn.will = None
            old.conn.abort()
            old.conn.session = None
            old.conn = None
        if clean or old is None:
            session = Session(client_id, clean)
            self.sessions[client_id] = session
            present = False
        else:
            session = old
            present = True
        session.clean = clean
        session.conn = conn
        session.last_seen = time.time()
        conn.session, conn.identity, conn.keepalive, conn.will, conn.username = session, identity, keepalive, will, username
        self.counters["conexiones"] += 1
        conn.send(packet(CONNACK, 0, bytes([1 if present else 0, 0])))
        self._trim_sessions()
        # Reintentar lo pendiente de una sesión anterior
        for pid, (msg, qos) in list(session.inflight.items()):
            self._send_publish(conn, msg, qos, pid, dup=True)
        self._drain(session)
        self.on_event("conectado", {"client_id": client_id, "user": username, "peer": conn.peer})
        return True

    def _close(self, conn: Connection, clean_exit: bool) -> None:
        conn.abort()
        self._conns.discard(conn)
        s = conn.session
        if s is None:
            return  # no llegó a conectarse, o fue reemplazada por otra conexión
        if conn.will is not None and not clean_exit:
            self.publish(conn.will.topic, conn.will.payload, conn.will.qos, conn.will.retain)
        s.conn = None
        s.last_seen = time.time()
        if s.clean:
            self.sessions.pop(s.client_id, None)
        self.on_event("desconectado", {"client_id": s.client_id, "user": conn.username, "peer": conn.peer,
                                       "limpio": clean_exit})

    def _trim_sessions(self) -> None:
        if len(self.sessions) <= MAX_SESSIONS:
            return
        offline = sorted((s for s in self.sessions.values() if s.conn is None), key=lambda s: s.last_seen)
        for s in offline[:len(self.sessions) - MAX_SESSIONS]:
            self.sessions.pop(s.client_id, None)

    # -- paquetes del cliente
    def _dispatch(self, conn: Connection, ptype: int, flags: int, body: bytes) -> None:
        s = conn.session
        if ptype == PUBLISH:
            self._on_publish(conn, flags, body)
        elif ptype == PUBACK:
            pid = _Reader(body).u16()
            s.inflight.pop(pid, None)
            self._drain(s)
        elif ptype == PUBREL:
            if flags != 0x02:
                raise ProtocolError("PUBREL con banderas inválidas")
            pid = _Reader(body).u16()
            conn.qos2_in.discard(pid)
            conn.send(packet(PUBCOMP, 0, pid.to_bytes(2, "big")))
        elif ptype in (PUBREC, PUBCOMP):
            pass  # el broker nunca envía QoS 2
        elif ptype == SUBSCRIBE:
            self._on_subscribe(conn, flags, body)
        elif ptype == UNSUBSCRIBE:
            if flags != 0x02:
                raise ProtocolError("UNSUBSCRIBE con banderas inválidas")
            r = _Reader(body)
            pid = r.u16()
            while not r.done():
                s.subs.pop(r.string(), None)
            conn.send(packet(UNSUBACK, 0, pid.to_bytes(2, "big")))
        elif ptype == PINGREQ:
            conn.send(packet(PINGRESP, 0))
        else:
            raise ProtocolError(f"paquete inesperado tipo {ptype}")

    def _on_publish(self, conn: Connection, flags: int, body: bytes) -> None:
        qos, retain = (flags >> 1) & 0x03, bool(flags & 0x01)
        if qos == 3:
            raise ProtocolError("QoS 3 no existe")
        r = _Reader(body)
        topic = r.string()
        pid = r.u16() if qos else 0
        payload = r.rest()
        if not valid_topic(topic):
            raise ProtocolError(f"tema inválido para publicar: {topic!r}")
        allowed = conn.identity.can_publish(topic)
        if not allowed:
            log.warning("Cliente %s (%s) sin permiso para publicar en %s", conn.client_id, conn.username, topic)
        self.counters["mensajes_entrada"] += 1
        if qos == 2:
            first = pid not in conn.qos2_in
            conn.qos2_in.add(pid)
            if first and allowed:
                self.publish(topic, payload, 2, retain)
            conn.send(packet(PUBREC, 0, pid.to_bytes(2, "big")))
            return
        if allowed:
            self.publish(topic, payload, qos, retain)
        if qos == 1:
            conn.send(packet(PUBACK, 0, pid.to_bytes(2, "big")))

    def _on_subscribe(self, conn: Connection, flags: int, body: bytes) -> None:
        if flags != 0x02:
            raise ProtocolError("SUBSCRIBE con banderas inválidas")
        r = _Reader(body)
        pid = r.u16()
        granted, new = [], []
        while not r.done():
            filt, req = r.string(), r.u8()
            if req > 2 or req & 0xFC:
                raise ProtocolError("QoS solicitado inválido")
            if not valid_filter(filt) or not conn.identity.can_subscribe(filt):
                if valid_filter(filt):
                    log.warning("Cliente %s (%s) sin permiso para suscribirse a %s", conn.client_id, conn.username, filt)
                granted.append(0x80)
                continue
            q = min(req, 1)
            conn.session.subs[filt] = q
            granted.append(q)
            new.append((filt, q))
        if not granted:
            raise ProtocolError("SUBSCRIBE sin filtros")
        conn.send(packet(SUBACK, 0, pid.to_bytes(2, "big") + bytes(granted)))
        for filt, q in new:
            for msg in list(self.retained.values()):
                if topic_matches(filt, msg.topic):
                    self._deliver(conn.session, msg, min(msg.qos, q), retain=True)

    # -- enrutamiento
    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Publicar desde el propio broker (o reenviar lo recibido). Debe llamarse en el hilo del broker."""
        msg = Message(topic, bytes(payload), min(qos, 2), retain)
        if retain:
            if payload:
                self.retained[topic] = Message(topic, msg.payload, min(qos, 1), True)
            else:
                self.retained.pop(topic, None)
        for s in list(self.sessions.values()):
            best = -1
            for filt, q in s.subs.items():
                if q > best and topic_matches(filt, topic):
                    best = q
            if best >= 0:
                self._deliver(s, msg, min(msg.qos, best))

    def _deliver(self, s: Session, msg: Message, qos: int, retain: bool = False) -> None:
        qos = min(qos, 1)
        conn = s.conn
        if conn is None or conn.closed:
            if not s.clean and qos >= 1:
                s.queue.append((Message(msg.topic, msg.payload, msg.qos, retain), qos))
            return
        if qos == 0:
            self._send_publish(conn, Message(msg.topic, msg.payload, 0, retain), 0, 0)
            return
        if len(s.inflight) >= MAX_INFLIGHT:
            s.queue.append((Message(msg.topic, msg.payload, msg.qos, retain), qos))
            return
        pid = s.alloc_pid()
        m = Message(msg.topic, msg.payload, msg.qos, retain)
        s.inflight[pid] = (m, qos)
        self._send_publish(conn, m, qos, pid)

    def _drain(self, s: Session) -> None:
        while s.queue and s.conn is not None and not s.conn.closed and len(s.inflight) < MAX_INFLIGHT:
            m, qos = s.queue.popleft()
            if qos == 0:
                self._send_publish(s.conn, m, 0, 0)
                continue
            pid = s.alloc_pid()
            s.inflight[pid] = (m, qos)
            self._send_publish(s.conn, m, qos, pid)

    def _send_publish(self, conn: Connection, msg: Message, qos: int, pid: int, dup: bool = False) -> None:
        flags = (0x08 if dup and qos else 0) | (qos << 1) | (0x01 if msg.retain else 0)
        body = enc_str(msg.topic) + (pid.to_bytes(2, "big") if qos else b"") + msg.payload
        self.counters["mensajes_salida"] += 1
        conn.send(packet(PUBLISH, flags, body))


# ---------------------------------------------------------------- hilo propio
class BrokerThread:
    """Corre el broker en su propio hilo y bucle asyncio (independiente del servidor web)."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[BaseException] = None
        self._ready = threading.Event()

    def start(self, timeout: float = 10) -> None:
        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.broker.start())
            except BaseException as exc:  # noqa: BLE001 - p. ej. el puerto ya está en uso
                self.error = exc
                self._ready.set()
                self.loop.close()
                return
            self._ready.set()
            try:
                self.loop.run_forever()
            finally:
                try:
                    self.loop.run_until_complete(self.broker.stop())
                finally:
                    self.loop.close()

        self.thread = threading.Thread(target=run, name="mqtt-broker", daemon=True)
        self.thread.start()
        self._ready.wait(timeout)
        if self.error:
            raise self.error

    def call(self, fn, *args) -> None:
        """Ejecuta ``fn`` dentro del hilo del broker (seguro desde cualquier hilo)."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(5)
