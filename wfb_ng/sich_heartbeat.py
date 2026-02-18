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


def _local(manager):
    metrics_manager = getattr(manager, "metrics_manager", None)
    rssi, per, snr = None, None, None
    if metrics_manager:
        metrics = metrics_manager.get_metrics()
        if metrics:
            rssi = metrics.get("rssi")
            per = metrics.get("per")
            snr = metrics.get("snr")
    channels = getattr(getattr(manager, "frequency_selection", None), "channels", None)
    current_channel = channels.current if channels else None
    channel_freq = current_channel.freq if current_channel else None
    snr_rounded = round(snr, 1) if snr is not None else "n/a"
    return {"rssi": _val(rssi), "per": _val(per), "snr": snr_rounded, "channel": _val(channel_freq)}


def _score(manager):
    channels = getattr(getattr(manager, "frequency_selection", None), "channels", None)
    current_channel = channels.current if channels else None
    if current_channel is None:
        return "n/a"
    return round(current_channel.score, 2)


def _remote_from_peer(peer_message):
    if not peer_message or not isinstance(peer_message, dict):
        return None
    if peer_message.get("type") != "heartbeat":
        return None
    peer_local = peer_message.get("local") or {}
    return {
        "timestamp": _val(peer_message.get("timestamp")),
        "status": _val(peer_message.get("status")),
        "rssi": _val(peer_local.get("rssi")),
        "per": _val(peer_local.get("per")),
        "snr": _val(peer_local.get("snr")),
        "channel": _val(peer_local.get("channel")),
        "score": _val(peer_message.get("score")),
    }


# --- ГС (downlink) ---

class HeartbeatGS(DatagramProtocol):
    def __init__(self, manager):
        self.manager = manager
        self._last_from_drone = None
        self._tick_loop = None

    def startProtocol(self):
        self._tick_loop = task.LoopingCall(self._tick)
        self._tick_loop.start(HEARTBEAT_INTERVAL_SEC, now=False)
        log.msg("[Heartbeat] GS UDP %d -> %s:%d" % (HEARTBEAT_GS_PORT, DRONE_IP, HEARTBEAT_DRONE_PORT))

    def stopProtocol(self):
        if self._tick_loop and self._tick_loop.running:
            self._tick_loop.stop()

    def _tick(self):
        if not self.manager.is_connected():
            return
        status_manager = getattr(self.manager, "status_manager", None)
        status = _val(status_manager.get_status() if status_manager else None)
        payload = {
            "type": "heartbeat",
            "timestamp": time.time(),
            "status": status,
            "local": _local(self.manager),
            "remote": _remote_from_peer(self._last_from_drone),
            "score": _val(_score(self.manager)),
        }
        try:
            self.transport.write(json.dumps(payload).encode(), (DRONE_IP, HEARTBEAT_DRONE_PORT))
        except Exception as error:
            log.msg("[Heartbeat] send: %s" % error)

    def datagramReceived(self, data, addr):
        try:
            message = json.loads(data.decode())
        except Exception:
            return
        self._last_from_drone = message
        remote_local = message.get("local") or {}
        log.msg("[HBeat] GS <- Drone: rssi=%s per=%s snr=%s" % (remote_local.get("rssi"), remote_local.get("per"), remote_local.get("snr")))
        callback = getattr(self.manager, "heartbeat_callback", None)
        if callback:
            try:
                callback(message)
            except Exception as error:
                log.msg("[Heartbeat] callback: %s" % error)


# --- Дрон (uplink) ---

class HeartbeatDrone(DatagramProtocol):
    def __init__(self, manager):
        self.manager = manager
        self._last_from_gs = None
        self._tick_loop = None

    def startProtocol(self):
        self._tick_loop = task.LoopingCall(self._tick)
        self._tick_loop.start(HEARTBEAT_INTERVAL_SEC, now=False)

    def stopProtocol(self):
        if self._tick_loop and self._tick_loop.running:
            self._tick_loop.stop()

    def _tick(self):
        status_manager = getattr(self.manager, "status_manager", None)
        status = _val(status_manager.get_status() if status_manager else None)
        payload = {
            "type": "heartbeat",
            "timestamp": time.time(),
            "status": status,
            "local": _local(self.manager),
            "remote": _remote_from_peer(self._last_from_gs),
            "score": _val(_score(self.manager)),
        }
        try:
            self.transport.write(json.dumps(payload).encode(), (GS_IP, HEARTBEAT_GS_PORT))
        except Exception as error:
            log.msg("[Heartbeat] send: %s" % error)

    def datagramReceived(self, data, addr):
        try:
            message = json.loads(data.decode())
        except Exception:
            return
        self._last_from_gs = message
        remote_local = message.get("local") or {}
        log.msg("[HBeat] Drone <- GS: rssi=%s per=%s snr=%s" % (remote_local.get("rssi"), remote_local.get("per"), remote_local.get("snr")))
