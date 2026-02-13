"""
Power Selection — модуль адаптивного управления мощностью передатчика (только на дроне).
Патерн: State Pattern
Приемущества: лучшая структура, расширяемость, легкость поддержки.
Основное: гистерезис и минимальное время пребывания на уровне мощности - уже ест в коде.
"""

import time
import re
import subprocess

from twisted.python import log
from twisted.internet import task, reactor

from ... import call_and_check_rc
from ...conf import settings


# ==================== НАСТРОЙКИ ====================

power_selection_switcher    = settings.common.power_sel_enabled
power_selection_level_list  = settings.common.power_sel_levels

# Гистерезис (в dBm)
RSSI_INCREASE_THRESHOLD   = -48    # RSSI ниже этого то увеличиваем мощность
RSSI_DECREASE_THRESHOLD   = -32    # RSSI выше этого то уменьшаем мощность
# Гистерезис (в dBm)
MIN_TIME_ON_LEVEL         = 8.0    # секунд
DRONE_STATS_LOG_INTERVAL  = 1      # Интервал лога статистики на дроне (секунды)
GS_POWER_CHECK_INTERVAL   = 2.0    # Интервал проверки RSSI на GS перед отправкой команды ДРОНУ (секунды)


def throttle_elapsed(last_time, interval=MIN_TIME_ON_LEVEL):
    """Проверка: прошло ли минимум interval с last_time (одна точка проверки throttle)."""
    return (time.time() - last_time) >= interval


def level_to_dbm(value):
    """Перевод значения уровня мощности в dBm"""
    return value / 100.0


def get_txpower_from_device(wlans):
    """Получить текущую TX power с устройства в dBm или None"""
    if not wlans:
        return None
    wlan = wlans[0] if isinstance(wlans, (list, tuple)) else wlans
    try:
        out = subprocess.check_output(
            ["iw", "dev", wlan, "info"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True
        )
        match = re.search(r"txpower\s+([-\d.]+)\s*dBm", out, re.IGNORECASE)
        return float(match.group(1)) if match else None
    except Exception:
        return None


# ==================== Состояния ====================

class PowerSelectionState:
    def __init__(self, ps):
        self.ps = ps

    def name(self):
        return "base"

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def on_arm(self):
        pass

    def on_disarm(self):
        pass

    def on_check_signal(self, rssi):
        pass


class DisabledState(PowerSelectionState):
    def name(self):
        return "disabled"

    def on_check_signal(self, rssi):
        pass  # ничего не делаем


class LockedState(PowerSelectionState):
    def name(self):
        return "locked"

    def on_enter(self):
        log.msg("[PS] Power level LOCKED after DISARM")

    def on_check_signal(self, rssi):
        pass  # изменения запрещены


class ActiveAdjustmentState(PowerSelectionState):
    def name(self):
        return "active"

    def on_enter(self):
        log.msg("[PS] Active power adjustment ENABLED (with hysteresis)")

    def on_check_signal(self, rssi):
        if rssi is None:
            return
        if not throttle_elapsed(self.ps._last_change_time):
            return

        current_index = self.ps.level_index
        max_index = len(self.ps.levels) - 1

        if rssi < RSSI_INCREASE_THRESHOLD and current_index < max_index:
            self.ps._last_change_time = time.time()
            self.ps.increase_txpower_level()
        elif rssi > RSSI_DECREASE_THRESHOLD and current_index > 0:
            self.ps._last_change_time = time.time()
            self.ps.decrease_txpower_level()


# ==================== Контроллер мощности на GS ====================
# Решение о смене мощности принимает только GS по своему RSSI (больше пакетов то они более валидные данные пакеты).
# GS отправляет команду дрону и дрон только выполняет

class GSPowerController:
    """
    Работает только на GS. Читает RSSI из metrics_manager, при низком/высоком значении
    отправляет команду дрону увеличить/уменьшить уровень TX power.
    """

    def __init__(self, manager):
        self.manager = manager
        self._last_command_time = 0.0
        self._lc = None

    def start(self):
        if not power_selection_switcher or not power_selection_level_list:
            log.msg("[GS Power] Disabled (power_sel not configured)")
            return
        self._lc = task.LoopingCall(self._check_and_send)
        self._lc.start(GS_POWER_CHECK_INTERVAL, now=False)
        log.msg("[GS Power] Controller started (RSSI -> tx_power commands to drone)")

    def stop(self):
        if self._lc and self._lc.running:
            self._lc.stop()
        log.msg("[GS Power] Controller stopped")

    def _check_and_send(self):
        if not getattr(self.manager, 'client_f', None) or not self.manager.is_connected():
            return
        # Управление по RSSI только в состоянии armed (в connected дрон должен держать минимум)
        if not getattr(self.manager, 'status_manager', None) or not self.manager.status_manager.is_armed():
            return

        metrics = None
        if getattr(self.manager, 'metrics_manager', None) and self.manager.metrics_manager:
            metrics = self.manager.metrics_manager.get_metrics()
        rssi = metrics.get('rssi') if metrics else None
        if rssi is None:
            return

        if not throttle_elapsed(self._last_command_time):
            return

        action = None
        if rssi < RSSI_INCREASE_THRESHOLD:
            action = "increase"
        elif rssi > RSSI_DECREASE_THRESHOLD:
            action = "decrease"
        if not action:
            return

        self._last_command_time = time.time()
        cmd = {"command": "tx_power", "action": action}
        d = self.manager.client_f.send_command(cmd)
        if d:
            log.msg(f"[GS Power] tx_power {action} (RSSI {rssi} dBm)")
            d.addErrback(lambda err: log.msg(f"[GS Power] Command failed: {err}"))


# ==================== Основной класс (дрон) ====================

class PowerSelection:
    """
    Управление адаптивной мощностью передатчика
    Из последнего: добавлен гистерезис и минимальное время пребывания на уровне мощности вафли.
    """

    def __init__(self, manager):
        self.manager = manager

        self.enabled = power_selection_switcher
        self.levels = power_selection_level_list

        # Текущее состояние
        self._current_state = None
        self._states = {
            "disabled": DisabledState(self),
            "locked":   LockedState(self),
            "active":   ActiveAdjustmentState(self),
        }
        
        self.level_index = 0            # Текущий индекс уровня мощности
        self._last_change_time = 0.0    # Время последнего изменения уровня
        self._last_command_time = 0.0   # Время последней применённой команды tx_power с GS (throttle т.е задержка)

        # Периодический лог статистики на дроне
        self._lc_log = task.LoopingCall(self._log_drone_stats)
        self._lc_log.start(DRONE_STATS_LOG_INTERVAL, now=True)
        self._lc_check = None # Периодический опрос RSSI

        log.msg(f"[PS] Инициализация: enabled={self.enabled}, levels={self.levels}")

        if self.enabled and self.levels:
            for i, val in enumerate(self.levels):
                log.msg(f"[PS] Уровень {i} = {val} -> {level_to_dbm(val):.1f} dBm")
            self.set_txpower_level(self.level_index) # Устанавливаем минимальную мощность при старте


        # Начальное состояние
        if not self.enabled:
            self._transition_to("disabled")
        else:
            self._transition_to("active")

    def _transition_to(self, state_name):
        if state_name not in self._states:
            log.msg(f"[PS] Unknown state: {state_name}")
            return

        new_state = self._states[state_name]

        old_name = self._current_state.name() if self._current_state else "none"
        if self._current_state:
            self._current_state.on_exit()

        self._current_state = new_state
        self._current_state.on_enter()

        log.msg(f"[PS] State changed: {old_name} -> {state_name}")

    # ─── Публичный интерфейс (сохранён полностью) ────────────────

    def on_arm(self):
        if self._current_state:
            self._current_state.on_arm()

        if self.enabled:
            self._transition_to("active")
        else:
            log.msg("[PS] ARM received, but power selection is disabled")

    def on_disarm(self):
        if self._current_state:
            self._current_state.on_disarm()

        if self.enabled:
            self._transition_to("locked")

    def on_tx_power_command(self, action):
        """
        Вызывается при получении команды tx_power от GS
        Только в состоянии active b throttle через MIN_TIME_ON_LEVEL
        """
        if not self.enabled or not self.levels:
            return
        if not self._current_state or self._current_state.name() != "active":
            return
        if not throttle_elapsed(self._last_command_time):
            return

        if action == "increase":
            if self.level_index >= len(self.levels) - 1:
                return
            self._last_command_time = time.time()
            self.increase_txpower_level()
        elif action == "decrease":
            if self.level_index <= 0:
                return
            self._last_command_time = time.time()
            self.decrease_txpower_level()

    def stop(self):
        if hasattr(self, "_lc_check") and self._lc_check:
            self._lc_check.stop()
        if hasattr(self, "_lc_log") and self._lc_log:
            self._lc_log.stop()
        log.msg("[PS] Stopped")

    # ─── Внутренние методы ───────────────────────────────────────

    def _check_signal(self):
        if not self._current_state:
            return

        metrics = None
        if hasattr(self.manager, 'metrics_manager') and self.manager.metrics_manager:
            metrics = self.manager.metrics_manager.get_metrics()

        rssi = metrics.get('rssi') if metrics else None
        self._current_state.on_check_signal(rssi)
        

    def _log_drone_stats(self):
        freq = None
        metrics = None

        if hasattr(self.manager, 'metrics_manager') and self.manager.metrics_manager:
            freq = self.manager.metrics_manager.get_current_freq()
            metrics = self.manager.metrics_manager.get_metrics()

        rssi = metrics.get('rssi') if metrics else None
        per = metrics.get('per') if metrics else None
        snr = metrics.get('snr') if metrics else None

        ch_str = f"{freq} MHz" if freq is not None else "N/A"
        rssi_str = f"{rssi} dBm" if rssi is not None else "N/A"
        per_str = f"{per}%" if per is not None else "N/A"
        snr_str = f"{snr:.2f} dB" if snr is not None else "N/A"

        conf_tx = level_to_dbm(self.levels[self.level_index]) if self.levels else None
        real_tx = get_txpower_from_device(self.manager.wlans)

        conf_str = f"{conf_tx:.1f} dBm" if conf_tx is not None else "N/A"
        real_str = f"{real_tx:.1f} dBm" if real_tx is not None else "N/A"

        print(
            f"Drone - Channel: {ch_str}, RSSI: {rssi_str}, PER: {per_str}, "
            f"SNR: {snr_str}, ConfTXp: {conf_str}, RealTXp: {real_str}",
            flush=True
        )
        
    # ─── тут функции для управления мощностью на дроне ───────────────────

    def set_txpower_level(self, level_index):
        if not self.levels:
            return
        if level_index < 0 or level_index >= len(self.levels):
            return

        prev_value = self.levels[self.level_index] if 0 <= self.level_index < len(self.levels) else None
        self.level_index = level_index
        new_value = self.levels[self.level_index]

        for wlan in self.manager.wlans:
            call_and_check_rc(
                "iw", "dev", wlan, "set", "txpower", "fixed", str(new_value)
            )

        if prev_value is not None and prev_value != new_value:
            log.msg(f"[PS] TX power: {level_to_dbm(prev_value):.1f} -> {level_to_dbm(new_value):.1f} dBm")

    def increase_txpower_level(self):
        if self.level_index < len(self.levels) - 1:
            self.set_txpower_level(self.level_index + 1)

    def decrease_txpower_level(self):
        if self.level_index > 0:
            self.set_txpower_level(self.level_index - 1)

    def set_minimum_power(self):
        if not self.enabled or not self.levels:
            return
        self.set_txpower_level(0)