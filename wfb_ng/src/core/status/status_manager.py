"""
Status Manager - модуль для управления статусами соединения с дроном.
Патерн: State Pattern
Приемущества: Основной модуль состояний связи

Синхронизация при recovery: на GS и на дроне один и тот же код — одинаковые таймауты
(PACKET_TIMEOUT / ARMED_PACKET_TIMEOUT, LOST_TO_RECOVERY_TIMEOUT)
"""

import time
from twisted.python import log
from twisted.internet import task
from abc import ABC


class ConnectionState(ABC):
    """Базовый класс для всех состояний. Имя состояния задаётся в подклассе через _state_name."""

    _state_name: str = ""  # переопределяется в подклассе

    def __init__(self, manager):
        self.manager = manager

    def name(self) -> str:
        return self._state_name

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def on_packet_received(self):
        pass

    def on_arm_command(self):
        pass

    def on_disarm_command(self):
        pass

    def on_metrics_update(self, per, rssi, snr):
        pass

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        pass


# ────────────────────────────────────────────────────────────────
#                         Состояния
# ────────────────────────────────────────────────────────────────

class WaitingState(ConnectionState):
    _state_name = "waiting"

    def on_packet_received(self):
        if self.manager._is_link_stable():
            self.manager._transition_to("connected")
            log.msg("[Waiting] Stable link (3 packets in 1s) -> Connected")


class ConnectedState(ConnectionState):
    _state_name = "connected"

    def on_arm_command(self):
        self.manager._transition_to("armed")

    def on_disarm_command(self):
        self.manager._transition_to("disarmed")

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        if time_since_packet is not None and time_since_packet > self.manager.PACKET_TIMEOUT:
            self.manager._transition_to("lost")
            self.manager._lost_since = now


class ArmedState(ConnectionState):
    _state_name = "armed"

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        # В armed используем увеличенный гистерезис, кратковременный обрыв или hop не ведут сразу в lost
        timeout = getattr(self.manager, 'ARMED_PACKET_TIMEOUT', self.manager.PACKET_TIMEOUT)
        if time_since_packet is not None and time_since_packet > timeout:
            self.manager._transition_to("lost")
            self.manager._lost_since = now

    def on_disarm_command(self):
        self.manager._transition_to("disarmed")


class DisarmedState(ConnectionState):
    _state_name = "disarmed"

    def on_enter(self):
        self.manager._disarmed_since = time.time()
        log.msg("[Disarmed] Grace period started")

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        # Переход в LOST только по таймауту пакетов
        if time_since_packet is not None and time_since_packet > self.manager.PACKET_TIMEOUT:
            self.manager._transition_to("lost")
            self.manager._disarmed_since = None
            self.manager._lost_since = now

        # НЕ уходим автоматически в recovery после 45 сек!

    def on_arm_command(self):
        self.manager._transition_to("armed")


class LostState(ConnectionState):
    _state_name = "lost"

    def on_enter(self):
        self.manager._lost_since = time.time()
        log.msg("[Lost] Entered lost state")

    def on_packet_received(self):
        # Если связь восстановилась (например после hop на другой конец freq_sel) — возврат в connected
        if self.manager._is_link_stable():
            self.manager._lost_since = None
            self.manager._transition_to("connected")
            log.msg("[Lost] Link restored (stable) -> Connected")

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        if self.manager._lost_since is None:
            self.manager._lost_since = now

        if now - self.manager._lost_since > self.manager.LOST_TO_RECOVERY_TIMEOUT:
            self.manager._transition_to("recovery")
            self.manager._lost_since = None


class RecoveryState(ConnectionState):
    _state_name = "recovery"

    def on_packet_received(self):
        if self.manager._is_link_stable():
            self.manager._transition_to("connected")
            log.msg("[Recovery] Stable link recovered -> Connected")


# ────────────────────────────────────────────────────────────────
#                       Основной класс
# ────────────────────────────────────────────────────────────────

class StatusManager:
    """Менеджер статусов соединения — внешний интерфейс остался прежним"""

    # Константы (можно будет вынести в config)
    STATUS_WAITING   = "waiting"
    STATUS_CONNECTED = "connected"
    STATUS_ARMED     = "armed"
    STATUS_DISARMED  = "disarmed"
    STATUS_LOST      = "lost"
    STATUS_RECOVERY  = "recovery"

    PACKET_TIMEOUT              = 5.0       # connected/disarmed: объявить lost после N сек без пакетов
    ARMED_PACKET_TIMEOUT        = 10.0      # armed -> lost: 10 сек без пакетов (гистерезис)
    LOST_TO_RECOVERY_TIMEOUT    = 10.0      # lost -> recovery: 10 сек в lost, затем FS hop на wifi_recovery
    DISARMED_GRACE_PERIOD       = 45.0      # используется только как ориентир

    STABLE_LINK_PACKETS = 3
    STABLE_LINK_PERIOD  = 1.0

    def __init__(self, config, wlans, manager=None):
        self._last_packet_time = None
        self._lost_since = None
        self._disarmed_since = None
        self._recent_packets = []

        self._link_established_first_time = False
        self._system_start_time = time.time()

        self._last_per = None
        self._last_rssi = None
        self._last_snr = None

        self._event_listeners = {}

        # Состояния
        self._states = {
            "waiting":    WaitingState(self),
            "connected":  ConnectedState(self),
            "armed":      ArmedState(self),
            "disarmed":   DisarmedState(self),
            "lost":       LostState(self),
            "recovery":   RecoveryState(self),
        }

        self._current_state = None

        # Запускаем периодическую проверку
        self._status_check_task = task.LoopingCall(self._periodic_check)
        self._status_check_task.start(1.0)

        # Начинаем с waiting
        self._transition_to("waiting")

        log.msg("[SM] Initialized and started (waiting for connection on wifi_channel)")

    # ─── Переход между состояниями ─────────────────────────────────

    def _transition_to(self, state_name: str):
        if state_name not in self._states:
            log.msg(f"[SM] Error: unknown state {state_name}")
            return

        new_state = self._states[state_name]

        old_status = self.get_status() if self._current_state else "none"

        if self._current_state:
            self._current_state.on_exit()

        self._current_state = new_state
        self._current_state.on_enter()

        log_msg = f"[SM] Status changed: {old_status} -> {state_name}"
        time_since_packet = self.get_time_since_last_packet()
        if time_since_packet is not None:
            log_msg += f" | Last packet: {time_since_packet:.1f}s ago"
        log.msg(log_msg)

        self._trigger_event("status_changed", old_status, state_name)

        if state_name in (self.STATUS_LOST, self.STATUS_RECOVERY):
            self._trigger_event("recovery_needed")

    # ─── Внешний интерфейс (сохранён полностью) ─────────────────────

    def subscribe(self, event_name, callback):
        if event_name not in self._event_listeners:
            self._event_listeners[event_name] = []
        self._event_listeners[event_name].append(callback)
        log.msg(f"[SM] Subscribed to event '{event_name}'")

    def unsubscribe(self, event_name, callback):
        if event_name in self._event_listeners:
            try:
                self._event_listeners[event_name].remove(callback)
            except ValueError:
                pass

    def _trigger_event(self, event_name, *args, **kwargs):
        for cb in self._event_listeners.get(event_name, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                log.msg(f"[SM] Error in event '{event_name}': {e}", isError=True)

    def on_packet_received(self):
        now = time.time()

        if self._last_packet_time is None and not self._link_established_first_time:
            self._link_established_first_time = True
            log.msg("[SM] First packet received, link established!")

        self._last_packet_time = now
        self._recent_packets.append(now)

        if self._current_state:
            self._current_state.on_packet_received()

    def on_arm_command(self):
        if self._current_state:
            self._current_state.on_arm_command()

    def on_disarm_command(self):
        if self._current_state:
            self._current_state.on_disarm_command()

    def update_metrics(self, per, rssi, snr):
        self._last_per = per
        self._last_rssi = rssi
        self._last_snr = snr
        if self._current_state:
            self._current_state.on_metrics_update(per, rssi, snr)

    def _periodic_check(self):
        now = time.time()
        time_since_packet = now - self._last_packet_time if self._last_packet_time else None

        # Логирование каждые ~5 секунд
        if not hasattr(self, '_last_log_time'):
            self._last_log_time = 0
        if now - self._last_log_time >= 5.0:
            self._last_log_time = now
            status = self.get_status()
            msg = f"[SM] Status={status}"
            if time_since_packet is not None:
                msg += f" | Last packet: {time_since_packet:.1f}s ago"
            log.msg(msg)

        if self._current_state:
            self._current_state.on_periodic_check(now, time_since_packet)

    def _is_link_stable(self) -> bool:
        now = time.time()
        self._recent_packets = [
            t for t in self._recent_packets
            if now - t <= self.STABLE_LINK_PERIOD
        ]
        return len(self._recent_packets) >= self.STABLE_LINK_PACKETS

    def get_status(self) -> str:
        return self._current_state.name() if self._current_state else "none"

    def get_time_since_last_packet(self) -> float | None:
        if self._last_packet_time is None:
            return None
        return time.time() - self._last_packet_time

    def is_waiting(self):    return self.get_status() == self.STATUS_WAITING
    def is_connected(self):  return self.get_status() == self.STATUS_CONNECTED
    def is_armed(self):      return self.get_status() == self.STATUS_ARMED
    def is_disarmed(self):   return self.get_status() == self.STATUS_DISARMED
    def is_lost(self):       return self.get_status() == self.STATUS_LOST
    def is_recovery(self):   return self.get_status() == self.STATUS_RECOVERY

    def is_link_active(self) -> bool:
        return self.get_status() in [
            self.STATUS_CONNECTED,
            self.STATUS_ARMED,
            self.STATUS_DISARMED,
            self.STATUS_RECOVERY
        ]

    def can_operate(self) -> bool:
        return self.get_status() in [self.STATUS_CONNECTED, self.STATUS_ARMED]

    def is_link_established(self) -> bool:
        return self._link_established_first_time or self._last_packet_time is not None

    def reset(self):
        self._last_packet_time = None
        self._lost_since = None
        self._disarmed_since = None
        self._recent_packets = []
        self._link_established_first_time = False
        self._transition_to("waiting")
        log.msg("[SM] State reset, waiting for connection on wifi_channel")

    def stop(self):
        if self._status_check_task and self._status_check_task.running:
            self._status_check_task.stop()
        log.msg("[SM] Stopped")

    def get_metrics(self):
        if self._last_per is None:
            return None
        return {
            'per': self._last_per,
            'rssi': self._last_rssi,
            'snr': self._last_snr
        }

    def get_status_info(self):
        now = time.time()
        return {
            "status": self.get_status(),
            "last_packet_time": self._last_packet_time,
            "time_since_last_packet": now - self._last_packet_time if self._last_packet_time else None,
            "last_disarm_time": self._disarmed_since,
            "time_since_disarm": now - self._disarmed_since if self._disarmed_since else None,
        }

    def get_system_uptime(self):
        return time.time() - self._system_start_time