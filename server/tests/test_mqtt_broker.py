"""Pruebas del broker MQTT integrado usando paho-mqtt (cliente de referencia) y sockets crudos."""
import queue
import socket
import struct
import time

import paho.mqtt.client as mqtt
import pytest

from app.mqtt_broker import Broker, BrokerThread, Identity, enc_str, packet, topic_matches, valid_filter

USERS = {"servidor": b"clave-servidor", "tablet-1": b"clave-1", "tablet-2": b"clave-2"}


def auth(user, password, client_id):
    if USERS.get(user) != password:
        return None
    if user == "servidor":
        return Identity("servidor")
    n = user.split("-")[1]
    return Identity(user,
                    can_publish=lambda t: t in (f"ack/{n}", f"estado/{n}"),
                    can_subscribe=lambda f: f == f"avisos/{n}")


@pytest.fixture()
def broker():
    b = Broker("127.0.0.1", 0, auth)
    th = BrokerThread(b)
    th.start()
    yield b
    th.stop()


class Cli:
    def __init__(self, port, user, pw, cid=None, clean=True, will=None, keepalive=30):
        self.msgs = queue.Queue()
        self.rc = queue.Queue()
        self.subs = queue.Queue()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid or "", clean_session=clean)
        self.c.username_pw_set(user, pw)
        if will:
            self.c.will_set(*will)
        self.c.on_connect = lambda c, u, f, rc, p: self.rc.put((rc.value, f.session_present))
        self.c.on_message = lambda c, u, m: self.msgs.put((m.topic, m.payload, m.qos, m.retain))
        self.c.on_subscribe = lambda c, u, mid, rcs, p: self.subs.put([r.value for r in rcs])
        self.c.connect("127.0.0.1", port, keepalive)
        self.c.loop_start()

    def connected(self):
        return self.rc.get(timeout=5)

    def get(self, timeout=3):
        return self.msgs.get(timeout=timeout)

    def none(self, wait=0.5):
        time.sleep(wait)
        return self.msgs.empty()

    def close(self):
        self.c.disconnect()
        self.c.loop_stop()


def raw_connect(port, cid, user, pw, keepalive=30, will=None, clean=True):
    flags = (0x02 if clean else 0) | 0x80 | 0x40
    payload = enc_str(cid)
    if will:
        flags |= 0x04 | (1 << 3) | 0x20  # will QoS1 retenido
        payload += enc_str(will[0]) + struct.pack(">H", len(will[1])) + will[1]
    payload += enc_str(user) + struct.pack(">H", len(pw)) + pw
    body = enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + payload
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(packet(1, 0, body))
    ack = s.recv(4)
    assert ack[:2] == b"\x20\x02", ack
    return s, ack[3]


def test_topic_matching_rules():
    assert topic_matches("a/+/c", "a/b/c") and not topic_matches("a/+/c", "a/b/d")
    assert topic_matches("a/#", "a") and topic_matches("a/#", "a/b/c") and topic_matches("#", "x/y")
    assert not topic_matches("#", "$SYS/x") and not topic_matches("+/x", "$SYS/x")
    assert topic_matches("+", "a") and not topic_matches("+", "a/b")
    assert valid_filter("a/+/#") and not valid_filter("a/#/b") and not valid_filter("a+") and not valid_filter("")


def test_bad_password_and_unknown_user(broker):
    s, rc = raw_connect(broker.port, "x", "tablet-1", b"mala")
    assert rc == 4
    s.close()
    c = Cli(broker.port, "nadie", "x")
    assert c.connected()[0] == 134  # paho: «Bad user name or password»
    c.close()


def test_publish_subscribe_qos_and_acl(broker):
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    t1 = Cli(broker.port, "tablet-1", "clave-1", "tab1")
    assert srv.connected()[0] == 0 and t1.connected()[0] == 0
    t1.c.subscribe([("avisos/1", 1), ("avisos/2", 1)])
    assert t1.subs.get(timeout=3) == [1, 128]  # no puede espiar los avisos de otra tablet
    srv.c.subscribe("ack/+", 1)
    srv.subs.get(timeout=3)
    srv.c.publish("avisos/1", b"hola", qos=1)
    srv.c.publish("avisos/2", b"secreto", qos=1)
    assert t1.get() == ("avisos/1", b"hola", 1, False)
    assert t1.none()
    t1.c.publish("ack/1", b"ok", qos=1)
    t1.c.publish("ack/2", b"falso", qos=1)  # sin permiso: se descarta
    t1.c.publish("avisos/1", b"x", qos=0)   # sin permiso
    assert srv.get() == ("ack/1", b"ok", 1, False)
    assert srv.none() and t1.none()
    srv.c.publish("avisos/1", b"q0", qos=0)
    assert t1.get() == ("avisos/1", b"q0", 0, False)
    srv.c.publish("avisos/1", "ñandú €".encode(), qos=2)  # QoS 2 de entrada, se entrega una vez
    assert t1.get()[1] == "ñandú €".encode() and t1.none()
    srv.close(); t1.close()


def test_retained_and_delete(broker):
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    srv.connected()
    srv.c.publish("estado/1", b"en_linea", qos=1, retain=True).wait_for_publish(3)
    other = Cli(broker.port, "servidor", "clave-servidor", "srv2")
    other.connected()
    other.c.subscribe("estado/#", 1)
    assert other.get() == ("estado/1", b"en_linea", 1, True)
    srv.c.publish("estado/1", b"otro", qos=1, retain=True)
    assert other.get() == ("estado/1", b"otro", 1, False)  # a suscriptores vivos llega sin retain
    srv.c.publish("estado/1", b"", qos=1, retain=True).wait_for_publish(3)
    other.get()
    third = Cli(broker.port, "servidor", "clave-servidor", "srv3")
    third.connected()
    third.c.subscribe("estado/#", 1)
    assert third.none()
    for c in (srv, other, third):
        c.close()


def test_last_will_on_abrupt_close_not_on_disconnect(broker):
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    srv.connected()
    srv.c.subscribe("estado/+", 1)
    srv.subs.get(timeout=3)
    s, rc = raw_connect(broker.port, "tab1", "tablet-1", b"clave-1", will=("estado/1", b"desconectado"))
    assert rc == 0
    s.close()  # se corta sin DISCONNECT
    assert srv.get() == ("estado/1", b"desconectado", 1, False)
    t = Cli(broker.port, "tablet-1", "clave-1", "tab1", will=("estado/1", b"desconectado", 1, True))
    t.connected()
    t.close()  # DISCONNECT limpio: sin Last Will
    assert srv.none(0.7)
    srv.close()


def test_will_on_forbidden_topic_refused(broker):
    s, rc = raw_connect(broker.port, "tab1", "tablet-1", b"clave-1", will=("estado/2", b"x"))
    assert rc == 5
    s.close()


def test_keepalive_timeout_publishes_will(broker):
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    srv.connected()
    srv.c.subscribe("estado/+", 1)
    srv.subs.get(timeout=3)
    s, rc = raw_connect(broker.port, "tab1", "tablet-1", b"clave-1", keepalive=1, will=("estado/1", b"caida"))
    t0 = time.time()
    assert srv.get(timeout=5)[1] == b"caida"
    assert 1.2 < time.time() - t0 < 3.5
    s.close()
    srv.close()


def test_persistent_session_queues_while_offline(broker):
    t = Cli(broker.port, "tablet-1", "clave-1", "tab1", clean=False)
    assert t.connected() == (0, False)
    t.c.subscribe("avisos/1", 1)
    t.subs.get(timeout=3)
    t.close()
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    srv.connected()
    for i in range(30):  # más que MAX_INFLIGHT: prueba la cola
        srv.c.publish("avisos/1", f"m{i}".encode(), qos=1)
    srv.c.publish("avisos/1", b"q0-se-pierde", qos=0)
    time.sleep(0.5)
    t2 = Cli(broker.port, "tablet-1", "clave-1", "tab1", clean=False)
    assert t2.connected() == (0, True)
    got = [t2.get()[1] for _ in range(30)]
    assert got == [f"m{i}".encode() for i in range(30)]
    assert t2.none()
    t2.close(); srv.close()


def test_takeover_closes_old_without_will(broker):
    srv = Cli(broker.port, "servidor", "clave-servidor", "srv")
    srv.connected()
    srv.c.subscribe("estado/+", 1)
    srv.subs.get(timeout=3)
    s1, _ = raw_connect(broker.port, "tab1", "tablet-1", b"clave-1", will=("estado/1", b"desconectado"))
    s2, rc = raw_connect(broker.port, "tab1", "tablet-1", b"clave-1", will=("estado/1", b"desconectado"))
    assert rc == 0
    s1.settimeout(3)
    assert s1.recv(10) == b""  # la conexión vieja se cerró
    assert srv.none(0.7)       # y su Last Will NO se publicó
    assert [c["client_id"] for c in broker.online_clients()].count("tab1") == 1
    s2.close()
    assert srv.get()[1] == b"desconectado"
    srv.close()


def test_garbage_and_oversized_packets_are_dropped(broker):
    s = socket.create_connection(("127.0.0.1", broker.port), timeout=3)
    s.sendall(b"GET / HTTP/1.1\r\n\r\n")
    assert s.recv(10) == b""
    s.close()
    s, rc = raw_connect(broker.port, "srv", "servidor", b"clave-servidor")
    s.sendall(bytes([0x30]) + b"\xff\xff\xff\x7f")  # longitud de 256 MB
    assert s.recv(10) == b""
    s.close()
    # El broker sigue atendiendo
    c = Cli(broker.port, "servidor", "clave-servidor", "ok")
    assert c.connected()[0] == 0
    c.close()
