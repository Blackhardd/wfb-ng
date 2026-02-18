"""
Обмен данными ГС <-> Дрон по UDP.
type: heartbeat, local: свои метрики, remote: что получили от пира (или null).
"""
import json
import time
from twisted.python import log
from twisted.internet import task
from twisted.internet.protocol import DatagramProtocol

HEARTBEAT_INTERVAL_SEC = 1.0
HEARTBEAT_GS_PORT = 14890
HEARTBEAT_DRONE_PORT = 14891
GS_IP = "10.5.0.1"
DRONE_IP = "10.5.0.2"


def _val(value):
    return value if value is not None else "n/a"


def _status(manager):
    status_manager = _status_manager(manager)
    return _val(status_manager.get_status() if status_manager else None)


def _parse(data):
    try:
        return json.loads(data.decode())
    except Exception:
        return None


def _encode(data):
    return json.dumps(data).encode()


def _attr(manager, name, default=None):
    return getattr(manager, name, default)


def _metrics_manager(manager):
    return _attr(manager, "metrics_manager")


def _status_manager(manager):
    return _attr(manager, "status_manager")


def _current_channel(manager):
    frequency_selection = _attr(manager, "frequency_selection")
    channels = _attr(frequency_selection, "channels") if frequency_selection else None
    return channels.current if channels else None


def _local(manager):
    metrics_manager = _metrics_manager(manager)
    rssi, per, snr = None, None, None # возвращаю сразу еперный театр 3 пустых коробки, спасибо pip8 за это
    if metrics_manager:
        metrics = metrics_manager.get_metrics()
        if metrics:
            rssi = metrics.get("rssi")
            per = metrics.get("per")
            snr = metrics.get("snr")
    snr_rounded = round(snr, 1) if snr is not None else "n/a"
    return {"rssi": _val(rssi), "per": _val(per), "snr": snr_rounded}


def _score(manager):
    current_channel = _current_channel(manager)
    if current_channel is None:
        return "n/a"
    return round(current_channel.score, 2)


def _remote_from_peer(peer_message):
    if not peer_message or not isinstance(peer_message, dict):
        return None # возвращаю пустую коробку, что бы не было ошибки
    if peer_message.get("type") != "heartbeat":
        return None # возвращаю пустую коробку, что бы не было ошибки
    peer_local = peer_message.get("local") or {}
    remote = {
        "timestamp": _val(peer_message.get("timestamp")),
        "status": _val(peer_message.get("status")),
        "rssi": _val(peer_local.get("rssi")),
        "per": _val(peer_local.get("per")),
        "snr": _val(peer_local.get("snr")),
        "score": _val(peer_message.get("score")),
    }
    return remote


# --- ГС ---

class HeartbeatGS(DatagramProtocol):
    def __init__(self, manager):
        self.manager = manager
        self._last_from_drone = None # по умолчанию пустая коробка где нет данных
        self._tick_loop = None # по умолчанию пустая коробка где нет данных

    def startProtocol(self):
        self._tick_loop = task.LoopingCall(self._tick)
        self._tick_loop.start(HEARTBEAT_INTERVAL_SEC, now=False)
        log.msg("[Heartbeat] GS UDP %d -> %s:%d" % (HEARTBEAT_GS_PORT, DRONE_IP, HEARTBEAT_DRONE_PORT))

    def stopProtocol(self):
        if self._tick_loop and self._tick_loop.running:
            self._tick_loop.stop()

    def _tick(self):
        data = {
            "type": "heartbeat",
            "timestamp": time.time(),
            "status": _status(self.manager),
            "local": _local(self.manager),
            "remote": _remote_from_peer(self._last_from_drone),
            "score": _val(_score(self.manager)),
        }
        try:
            self.transport.write(_encode(data), (DRONE_IP, HEARTBEAT_DRONE_PORT))
        except Exception as error:
            log.msg("[Heartbeat] send: %s" % error)

    def datagramReceived(self, data, addr):
        message = _parse(data)
        if message is None:
            return
        self._last_from_drone = message
        remote_local = message.get("local") or {}
        log.msg("[HBeat] GS <- Drone: rssi=%s per=%s snr=%s" % (remote_local.get("rssi"), remote_local.get("per"), remote_local.get("snr")))
        callback = _attr(self.manager, "heartbeat_callback")
        if callback:
            try:
                callback(message)
            except Exception as error:
                log.msg("[Heartbeat] callback: %s" % error)


# --- Дрон  ---

class HeartbeatDrone(DatagramProtocol):
    def __init__(self, manager):
        self.manager = manager
        self._last_from_gs = None # пустая коробка
        self._tick_loop = None # пустая коробка

    def startProtocol(self):
        self._tick_loop = task.LoopingCall(self._tick)
        self._tick_loop.start(HEARTBEAT_INTERVAL_SEC, now=False)

    def stopProtocol(self):
        if self._tick_loop and self._tick_loop.running:
            self._tick_loop.stop()

    def _tick(self):
        data = {
            "type": "heartbeat",
            "timestamp": time.time(),
            "status": _status(self.manager),
            "local": _local(self.manager),
            "remote": _remote_from_peer(self._last_from_gs),
            "score": _val(_score(self.manager)),
        }
        try:
            self.transport.write(_encode(data), (GS_IP, HEARTBEAT_GS_PORT))
        except Exception as error:
            log.msg("[Heartbeat] send: %s" % error)

    def datagramReceived(self, data, addr):
        message = _parse(data)
        if message is None:
            return
        self._last_from_gs = message
        remote_local = message.get("local") or {}
        log.msg("[HBeat] Drone <- GS: rssi=%s per=%s snr=%s" % (remote_local.get("rssi"), remote_local.get("per"), remote_local.get("snr")))
