"""
sich_heartbeat - двунаправленный heartbeat между ГС и дроном

Суть моей идеи такова
1. ГС отправляет сообщение с: timestamp, rssi, per, score, rx_from_drone
2. Сообщение доходит до дрона и дрону нужно ответить ГС
3. Дрон формирует ответ с: gs_timestamp, timestamp (момент ответа), метрики ГС, rx_from_gs
4. Пульт получает ответ - знает, получает ли дрон сигнал, может вычислить RTT

и ТЕПЕРЬ Круг замыкается когда каждая сторона знает о приёме у противоположной стороны
"""
import time
from twisted.python import log
from twisted.internet import task


# Интервал отправки heartbeat с ГС (сек)
HEARTBEAT_INTERVAL_SEC = 2.0
# Порог: сравниваем наш now с таймстампом пира; если (now - peer_ts) > threshold — «не получаем»
HEARTBEAT_RX_OK_THRESHOLD_SEC = 5.0


def _gs_rx_from_drone(manager) -> bool:
    """
    ГС: получаем ли мы от дрона? Сравниваем наш now с timestamp из последнего ответа дрона.
    Если (now - drone_ts) < threshold — считаем что "мы получили все ок - получаем"
    """
    sender = getattr(manager, 'heartbeat_sender', None)
    if not sender or not hasattr(sender, '_last_drone_ts'):
        return False
    ts = sender._last_drone_ts
    if ts is None:
        return False
    return (time.time() - ts) < HEARTBEAT_RX_OK_THRESHOLD_SEC


def _drone_rx_from_gs(incoming: dict) -> bool:
    """
    Дрон: получаем ли мы от ГС? Сравниваем наш now с gs_timestamp из входящего сообщения
    Если (now - gs_timestamp) < threshold - считаем что "мы получили все ок - получаем"
    """
    gs_ts = incoming.get("timestamp")
    if gs_ts is None:
        return False
    return (time.time() - gs_ts) < HEARTBEAT_RX_OK_THRESHOLD_SEC


def _get_metrics_and_score(manager) -> tuple:
    """
    Возвращаем мы (rssi, per, score) для текущей стороны
    rssi, per - из metrics_manager; score - из текущего канала
    """
    rssi, per, score = None, None, 100.0
    if getattr(manager, 'metrics_manager', None) and manager.metrics_manager:
        m = manager.metrics_manager.get_metrics()
        if m:
            rssi = m.get('rssi')
            per = m.get('per')
    if getattr(manager, 'frequency_selection', None) and manager.frequency_selection:
        ch = manager.frequency_selection.channels.current
        if ch:
            score = ch.score
    return rssi, per, score


def _get_first_connect_ts(manager):
    """Таймстамп первого подключения (для uptime)."""
    return getattr(manager, '_first_connect_ts', None)


def _get_status(manager) -> str | None:
    """Статус из StatusManager (waiting, connected, armed, disarmed, lost, recovery)."""
    sm = getattr(manager, 'status_manager', None)
    return sm.get_status() if sm else None


def build_heartbeat_payload(manager) -> dict:
    """
    Собрать payload heartbeat для отправки (ГС).
    first_connect_ts — когда устройство впервые подключилось (uptime = now - first_connect_ts).
    """
    rssi, per, score = _get_metrics_and_score(manager)
    rx_from_drone = _gs_rx_from_drone(manager)
    first_ts = _get_first_connect_ts(manager)
    uptime = (time.time() - first_ts) if first_ts else None
    status = _get_status(manager)
    return {
        "command": "heartbeat",
        "timestamp": time.time(),
        "first_connect_ts": first_ts,
        "uptime_sec": round(uptime, 2) if uptime is not None else None,
        "status": status,
        "rssi": rssi,
        "per": per,
        "score": round(score, 2) if score is not None else None,
        "rx_from_drone": rx_from_drone,
    }


def build_heartbeat_response(manager, incoming: dict) -> dict:
    """
    Дрон: сформировать ответ на heartbeat от ГС
    Передаём метрики ГС (из incoming), добавляем rx_from_gs
    """
    log.msg(
        f"[Hbeat RX] Дрон <-<-<- ГС: ts={incoming.get('timestamp')}, status={incoming.get('status')}, "
        f"rssi={incoming.get('rssi')}, per={incoming.get('per')}%, rx_from_drone={incoming.get('rx_from_drone')}"
    )
    rx_from_gs = _drone_rx_from_gs(incoming)
    ts = time.time()
    first_ts = _get_first_connect_ts(manager)
    uptime = (ts - first_ts) if first_ts else None
    drone_status = _get_status(manager)
    log.msg(f"[Heartbeat TX] Дрон -> ГС: status={drone_status}, rx_from_gs={rx_from_gs}, ts={ts:.2f}, uptime={uptime:.1f}s" if uptime else f"[Heartbeat TX] Дрон -> ГС: status={drone_status}, rx_from_gs={rx_from_gs}, ts={ts:.2f}")
    return {
        "status": "success",
        "command": "heartbeat",
        "gs_timestamp": incoming.get("timestamp"),
        "gs_first_connect_ts": incoming.get("first_connect_ts"),
        "gs_uptime_sec": incoming.get("uptime_sec"),
        "gs_status": incoming.get("status"),
        "timestamp": ts,
        "drone_first_connect_ts": first_ts,
        "drone_uptime_sec": round(uptime, 2) if uptime is not None else None,
        "drone_status": drone_status,
        "gs_rssi": incoming.get("rssi"),
        "gs_per": incoming.get("per"),
        "gs_score": incoming.get("score"),
        "gs_rx_from_drone": incoming.get("rx_from_drone"),
        "drone_rx_from_gs": rx_from_gs,
    }


def on_heartbeat_response(manager, response: dict) -> None:
    """
    ГС: обработать ответ на heartbeat получение ответа от др
    """
    drone_rx = response.get("drone_rx_from_gs")
    gs_rssi = response.get("gs_rssi")
    gs_per = response.get("gs_per")
    gs_score = response.get("gs_score")
    ts_sent = response.get("gs_timestamp")
    ts_reply = response.get("timestamp")
    rtt = (time.time() - ts_sent) if ts_sent else None
    gs_uptime = response.get("gs_uptime_sec")
    drone_uptime = response.get("drone_uptime_sec")
    gs_status = response.get("gs_status")
    drone_status = response.get("drone_status")
    base = (f"[HBeat RX] ГС <-<-<- Дрон: GS status={gs_status}, rx_from_drone={response.get('gs_rx_from_drone')}, "
            f"Drone status={drone_status}, rx_from_gs={drone_rx} | rssi={gs_rssi}, per={gs_per}, score={gs_score}")
    if gs_uptime is not None:
        base += f" | GS uptime={gs_uptime}s"
    if drone_uptime is not None:
        base += f" | Drone uptime={drone_uptime}s"
    if ts_sent is not None and ts_reply is not None:
        base += f" | ts_sent={ts_sent:.2f}, ts_reply={ts_reply:.2f}, rtt={rtt:.3f}s"
    log.msg(base)
    if hasattr(manager, 'heartbeat_callback') and manager.heartbeat_callback:
        try:
            manager.heartbeat_callback(response)
        except Exception as e:
            log.msg(f"[Heartbeat] Callback error: {e}")


class HeartbeatSender:
    """
    Отправка heartbeat с ГС Запускается только на GSManager
    Хранит _last_drone_ts - timestamp из последнего ответа дрона 
    """
    def __init__(self, manager):
        self.manager = manager
        self._lc = None
        self._last_drone_ts = None  # timestamp последнего ответа дрона

    def start(self):
        if not hasattr(self.manager, 'send_command_to_drone'):
            log.msg("[Heartbeat] Sender disabled: no send_command_to_drone (not GS)")
            return
        self._lc = task.LoopingCall(self._send_once)
        self._lc.start(HEARTBEAT_INTERVAL_SEC, now=False)
        log.msg("[Heartbeat] Sender started on GS")

    def stop(self):
        if self._lc and self._lc.running:
            self._lc.stop()
        log.msg("[Heartbeat] Sender stopped")

    def _send_once(self):
        if not getattr(self.manager, 'send_command_to_drone', None):
            return
        if not self.manager.is_connected():
            return
        payload = build_heartbeat_payload(self.manager)
        uptime = payload.get('uptime_sec')
        uptime_str = f", uptime={uptime}s" if uptime is not None else ""
        status = payload.get('status')
        status_str = f"status={status}, " if status else ""
        log.msg(
            f"[Heartbeat TX] ГС -> Дрон: {status_str}rssi={payload.get('rssi')}, per={payload.get('per')}%, "
            f"score={payload.get('score')}, rx_from_drone={payload.get('rx_from_drone')}{uptime_str}, ts={payload.get('timestamp'):.2f}"
        )
        d = self.manager.send_command_to_drone(payload)
        if d is None:
            return
        d.addCallback(self._on_response)
        d.addErrback(lambda err: log.msg(f"[Heartbeat] Send failed: {err}"))

    def _on_response(self, response):
        if response.get("command") == "heartbeat" and response.get("status") == "success":
            self._last_drone_ts = response.get("timestamp")  # таймстамп отправки ответа дроном
            on_heartbeat_response(self.manager, response)
