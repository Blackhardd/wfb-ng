"""
State machine for link status. Same logic on both drone and GS.

Two different situations:
- Cold start: устройство только запустилось, ещё ни разу не имело связи (waiting, never left).
- Link loss: связь была, потом пропала -> lost -> recovery. _status_before_lost хранит состояние до потери.

Различать: is_cold_start() vs is_after_link_loss(). После перезагрузки дрона: GS в recovery
(потеря связи), дрон в waiting (холодный старт) — по ним можно синхронизировать состояние.
"""
import time
from twisted.python import log
from twisted.internet import task
from abc import ABC

from ..data import format_channel_freq

class ConnectionState(ABC):
    _state_name: str = ""

    def __init__(self, manager):
        self.manager = manager

    def name(self) -> str:
        return self._state_name

    def on_enter(self, previous_status=None):
        pass

    def on_exit(self):
        pass

    def on_packet_received(self):
        pass

    def on_arm_command(self):
        pass

    def on_disarm_command(self):
        pass

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        pass

class WaitingState(ConnectionState):
    _state_name = "waiting"
    WAITING_RADIO_FALLBACK_SEC = 5.0
    WAITING_LINK_ALIVE_SEC = 2.0

    def on_enter(self, previous_status=None):
        self._waiting_entered_at = time.time()
        log.msg("[SM] Ожидаем на WiFi_channel (хопы отключены)")

    def on_exit(self):
        self._waiting_entered_at = None

    def on_packet_received(self):
        pass

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        # Одинаково для дрона и ГС: стабильный радио-линк без TCP -> переход в connected
        if time_since_packet is None or time_since_packet > self.WAITING_LINK_ALIVE_SEC:
            return
        entered = getattr(self, "_waiting_entered_at", None)
        if entered is None or (now - entered) < self.WAITING_RADIO_FALLBACK_SEC:
            return
        log.msg("[SM] Waiting: radio stable without TCP handshake, fallback -> connected")
        self.manager._transition_to("connected")

class ConnectedState(ConnectionState):
    _state_name = "connected"

    def on_enter(self, previous_status=None):
        pass

    def on_arm_command(self):
        self.manager._transition_to("armed")

    def on_disarm_command(self):
        self.manager._transition_to("disarmed")

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        # Единый сценарий для дрона и ГС: таймаут пакетов -> lost (как в armed/disarmed)
        if time_since_packet is not None and time_since_packet > self.manager.PACKET_TIMEOUT:
            self.manager._transition_to("lost")
            self.manager._lost_since = now

class ArmedState(ConnectionState):
    _state_name = "armed"

    def on_enter(self, previous_status=None):
        pass

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        if time_since_packet is not None and time_since_packet > self.manager.PACKET_TIMEOUT:
            self.manager._transition_to("lost")
            self.manager._lost_since = now

    def on_disarm_command(self):
        self.manager._transition_to("disarmed")

class DisarmedState(ConnectionState):
    _state_name = "disarmed"

    def on_enter(self, previous_status=None):
        fs = self.manager._get_frequency_selection()
        if fs:
            fs.reset_all_channels_stats()

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        if time_since_packet is not None and time_since_packet > self.manager.PACKET_TIMEOUT:
            self.manager._transition_to("lost")
            self.manager._lost_since = now

    def on_arm_command(self):
        self.manager._transition_to("armed")

class LostState(ConnectionState):
    _state_name = "lost"

    def on_enter(self, previous_status=None):
        # Запоминаем, в каком состоянии были до lost — туда вернёмся при восстановлении
        if previous_status in ("armed", "connected", "disarmed"):
            self.manager._status_before_lost = previous_status
        else:
            self.manager._status_before_lost = getattr(self.manager, "_status_before_lost", "connected")
        self.manager._lost_since = time.time()

        # Один авто-хоп на первый канал из freq_sel только если пришли из "нормального" состояния (не из waiting/recovery).
        fs = self.manager._get_frequency_selection()
        if fs and fs.is_enabled():
            if previous_status in ("connected", "armed", "disarmed"):
                first_ch = fs.channels.first_freq_sel_channel
                if first_ch:
                    log.msg(f"[SM] -> Lost: авто-хоп на первый канал {format_channel_freq(first_ch.freq)} "
                            f"(пред. состояние: {previous_status})")
                    fs.hop_local.to_first(delay=0)
            else:
                log.msg(f"[SM] В lost пришли не из активного состояния ({previous_status}), хоп пропущен")

    def on_packet_received(self):
        if self.manager._last_packet_time is not None:
            self.manager._lost_since = None
            # Восстанавливаемся в то же состояние, что было до lost
            restore = self.manager._status_before_lost if self.manager._status_before_lost in ("armed", "connected", "disarmed") else "connected"
            self.manager._transition_to(restore)

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        if self.manager._lost_since is None:
            self.manager._lost_since = now
        if now - self.manager._lost_since <= self.manager.LOST_TO_RECOVERY_TIMEOUT:
            return
        self.manager._lost_since = None
        self.manager._transition_to("recovery")

class RecoveryState(ConnectionState):
    _state_name = "recovery"

    def on_enter(self, previous_status=None):
        fs = self.manager._get_frequency_selection()
        if fs:
            fs.reset_all_channels_stats()
        # Хоп на wifi_channel: в recovery ждём восстановления связи на стартовом канале
        if fs and fs.is_enabled():
            wifi_ch = fs.channels.reserve
            if wifi_ch and fs.channels.current.freq != wifi_ch.freq:
                log.msg(f"[Recovery] Хоп на wifi_channel {format_channel_freq(wifi_ch.freq)}")
                fs.hop_local.to_wifi_channel(delay=0)
        log.msg("[Recovery] Вошли в режим длительного ожидания восстановления связи. "
                "Повторных хопов не будет. Ждём пакетов.")

    def on_periodic_check(self, now: float, time_since_packet: float | None):
        # Не уходим обратно в lost. Остаёмся в recovery до восстановления связи (on_packet_received).
        pass

    def on_packet_received(self):
        if self.manager._last_packet_time is not None:
            # Recovery = долгое ожидание; дрон мог перезагрузиться. Всегда в connected.
            self.manager._transition_to("connected")
            log.msg("[Recovery] Link recovered -> connected (дрон мог перезапуститься)")

class StatusManager:
    STATUS_WAITING   = "waiting"
    STATUS_CONNECTED = "connected"
    STATUS_ARMED     = "armed"
    STATUS_DISARMED  = "disarmed"
    STATUS_LOST      = "lost"
    STATUS_RECOVERY  = "recovery"

    PACKET_TIMEOUT           = 5.0
    LOST_TO_RECOVERY_TIMEOUT = 10.0

    def __init__(self, config, wlans, manager=None):
        self.manager = manager
        self._last_packet_time = None
        self._lost_since = None
        self._recovered_from_lost_at = None
        self._link_established_first_time = False
        # False пока ни разу не выходили из waiting (холодный старт); после первого connected/armed/disarmed — True
        self._has_ever_established_link = False

        self._states = {
            "waiting":    WaitingState(self),
            "connected":  ConnectedState(self),
            "armed":      ArmedState(self),
            "disarmed":   DisarmedState(self),
            "lost":       LostState(self),
            "recovery":   RecoveryState(self),
        }
        self._current_state = None
        self._previous_status = None
        # Состояние до потери связи (connected/armed/disarmed) — в него возвращаемся при восстановлении
        self._status_before_lost = "connected"

        self._status_check_task = task.LoopingCall(self._periodic_check)
        self._status_check_task.start(1.0)

        self._transition_to("waiting")
        log.msg("[SM] Старт инициализации Системы Статусов")

    def _get_frequency_selection(self):
        if self.manager and hasattr(self.manager, 'frequency_selection'):
            return self.manager.frequency_selection
        return None

    def _transition_to(self, state_name: str):
        if state_name not in self._states:
            log.msg(f"[SM] Ошибка: Не известный статус: {state_name}")
            return
        if self._current_state and self._current_state.name() == state_name:
            return

        new_state = self._states[state_name]
        old_status = self.get_status() if self._current_state else "none"
        self._previous_status = old_status

        if self._current_state:
            self._current_state.on_exit()

        self._current_state = new_state
        self._current_state.on_enter(previous_status=old_status)

        if old_status == "lost" and state_name in ("armed", "connected", "disarmed"):
            self._recovered_from_lost_at = time.time()
        if state_name == "lost":
            self._recovered_from_lost_at = None
        if old_status == "waiting" and state_name in ("connected", "armed", "disarmed"):
            self._has_ever_established_link = True

        log_msg = f"[SM] Изменен статус с: {old_status} -> {state_name}"
        time_since_packet = self.get_time_since_last_packet()
        if time_since_packet is not None:
            log_msg += f" | Посл.пак: {time_since_packet:.1f}s назад"
        log.msg(log_msg)

        if self.manager:
            self.manager.on_status_changed(old_status, state_name)

    def on_packet_received(self):
        now = time.time()
        if self._last_packet_time is None and not self._link_established_first_time:
            self._link_established_first_time = True
            log.msg("[SM] Первый пакет получен, связь установлена!")

        self._last_packet_time = now

        if self._current_state:
            self._current_state.on_packet_received()

    def on_arm_command(self):
        if self._current_state:
            self._current_state.on_arm_command()

    def on_disarm_command(self):
        if self._current_state:
            self._current_state.on_disarm_command()

    def _periodic_check(self):
        now = time.time()
        time_since_packet = now - self._last_packet_time if self._last_packet_time else None

        if not hasattr(self, '_last_log_time'):
            self._last_log_time = 0
        if now - self._last_log_time >= 5.0:
            self._last_log_time = now
            status = self.get_status()
            msg = f"[SM] Статус ={status}"
            if time_since_packet is not None:
                msg += f" | Посл.пак: {time_since_packet:.1f}s назад"
            log.msg(msg)

        if self._current_state:
            self._current_state.on_periodic_check(now, time_since_packet)

    def get_status(self) -> str:
        return self._current_state.name() if self._current_state else "none"

    def get_time_since_last_packet(self) -> float | None:
        if self._last_packet_time is None:
            return None
        return time.time() - self._last_packet_time

    def is_armed(self) -> bool:
        return self.get_status() == self.STATUS_ARMED

    def is_cold_start(self) -> bool:
        """True: устройство только запустилось, ни разу не имело связи (дрон после перезагрузки и т.п.)."""
        return self.get_status() == self.STATUS_WAITING and not self._has_ever_established_link

    def is_after_link_loss(self) -> bool:
        """True: связь была, потом пропала — мы в lost или recovery (не холодный старт)."""
        return self.get_status() in (self.STATUS_LOST, self.STATUS_RECOVERY)

    def stop(self):
        if self._status_check_task and self._status_check_task.running:
            self._status_check_task.stop()
        log.msg("[SM] Остановлен мониторинг статусов")
