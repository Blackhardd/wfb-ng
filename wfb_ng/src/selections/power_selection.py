"""
Power Selection — адаптивное управление мощностью передатчика (дрон).
Решение о смене мощности принимает ГС по своему RSSI и шлёт команды дрону; дрон только выполняет.
"""

import time
import re
import subprocess

from twisted.internet import task, reactor
from twisted.python import log

from ... import call_and_check_rc
from ...conf import settings


# ==================== НАСТРОЙКИ ====================

power_selection_switcher = settings.common.power_sel_enabled
power_selection_level_list = settings.common.power_sel_levels

# Гистерезис (dBm): увеличиваем мощность только при низком RSSI, уменьшаем только при высоком
RSSI_INCREASE_THRESHOLD = -48   # RSSI ниже -> увеличиваем мощность
RSSI_DECREASE_THRESHOLD = -32   # RSSI выше -> уменьшаем мощность

# Минимальное время на одном уровне перед следующим изменением (сек)
MIN_TIME_ON_LEVEL = 8.0

# Интервал лога статистики на дроне (сек)
DRONE_STATS_LOG_INTERVAL = 1

# Интервал проверки RSSI на ГС перед отправкой команды дрону (сек)
GS_POWER_CHECK_INTERVAL = 2.0


def level_to_dbm(value):
    """Перевод уровня мощности в dBm (значения в конфиге: dBm×100)."""
    return value / 100.0


def get_txpower_from_device(wlans):
    """Текущая TX power с устройства в dBm или None."""
    if not wlans:
        return None
    wlan = wlans[0] if isinstance(wlans, (list, tuple)) else wlans
    try:
        out = subprocess.check_output(
            ["iw", "dev", wlan, "info"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        match = re.search(r"txpower\s+([-\d.]+)\s*dBm", out, re.IGNORECASE)
        return float(match.group(1)) if match else None
    except Exception:
        return None


# ==================== Состояния (дрон) ====================

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

    def on_tx_power_command(self, action):
        pass


class DisabledState(PowerSelectionState):
    def name(self):
        return "disabled"

    def on_tx_power_command(self, action):
        pass


class LockedState(PowerSelectionState):
    def name(self):
        return "locked"

    def on_enter(self):
        log.msg("[PS] Power level LOCKED after DISARM")

    def on_tx_power_command(self, action):
        pass


class ActiveAdjustmentState(PowerSelectionState):
    def name(self):
        return "active"

    def on_enter(self):
        log.msg("[PS] Active power adjustment ENABLED (commands from GS)")

    def on_tx_power_command(self, action):
        if action == "increase":
            if self.ps.level_index >= len(self.ps.levels) - 1:
                log.msg("[PS] Command increase ignored — already at max level")
                return
            now = time.time()
            if now - self.ps._last_command_time < MIN_TIME_ON_LEVEL:
                log.msg(f"[PS] Command increase ignored — too soon (throttle {MIN_TIME_ON_LEVEL}s)")
                return
            self.ps._last_command_time = now
            self.ps.increase_txpower_level()
        elif action == "decrease":
            if self.ps.level_index <= 0:
                log.msg("[PS] Command decrease ignored — already at min level")
                return
            now = time.time()
            if now - self.ps._last_command_time < MIN_TIME_ON_LEVEL:
                log.msg(f"[PS] Command decrease ignored — too soon (throttle {MIN_TIME_ON_LEVEL}s)")
                return
            self.ps._last_command_time = now
            self.ps.decrease_txpower_level()


# ==================== Контроллер мощности на ГС ====================
# Решение по RSSI принимает только ГС; отправляет команду дрону increase/decrease.

class GSPowerController:
    """
    Только на ГС. Читает RSSI (freqsel/link_status), при низком/высоком значении
    отправляет команду дрону увеличить/уменьшить TX power.
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
        self._lc = None
        log.msg("[GS Power] Controller stopped")

    def _get_rssi(self):
        """RSSI с ГС: link_status или freqsel."""
        link_status = getattr(self.manager, "link_status", None)
        if link_status is not None and hasattr(link_status, "get_live_rssi"):
            rssi = link_status.get_live_rssi()
            if rssi is not None:
                return rssi
        if getattr(self.manager, "freqsel", None):
            return self.manager.freqsel.get_current_rssi()
        return None

    def _check_and_send(self):
        if not getattr(self.manager, "client_f", None) or not self.manager.is_connected():
            return
        if not getattr(self.manager, "status_manager", None) or not self.manager.status_manager.is_armed():
            return

        rssi = self._get_rssi()
        if rssi is None:
            return

        now = reactor.seconds()
        if now - self._last_command_time < MIN_TIME_ON_LEVEL:
            return

        action = None
        if rssi < RSSI_INCREASE_THRESHOLD:
            action = "increase"
        elif rssi > RSSI_DECREASE_THRESHOLD:
            action = "decrease"

        if not action:
            return

        self._last_command_time = now
        cmd = {"command": "tx_power", "action": action}
        d = self.manager.client_f.send_command(cmd)
        if d:
            log.msg(f"[GS Power] RSSI {rssi} dBm -> sending tx_power {action} to drone")
            d.addErrback(lambda err: log.msg(f"[GS Power] Command failed: {err}"))


# ==================== Основной класс (дрон) ====================

class PowerSelection:
    """
    Управление мощностью передатчика на дроне.
    Регулировка только по командам с ГС (on_tx_power_command). Локального цикла по RSSI нет.
    """

    def __init__(self, manager):
        self.manager = manager

        self.enabled = power_selection_switcher
        self.levels = power_selection_level_list

        self._current_state = None
        self._states = {
            "disabled": DisabledState(self),
            "locked": LockedState(self),
            "active": ActiveAdjustmentState(self),
        }

        self.level_index = 0
        self._last_change_time = 0.0
        self._last_command_time = 0.0

        self._lc_log = task.LoopingCall(self._log_drone_stats)
        self._lc_log.start(DRONE_STATS_LOG_INTERVAL, now=True)

        log.msg(f"[PS] Init: enabled={self.enabled}, levels={self.levels}")

        if self.enabled and self.levels:
            for i, val in enumerate(self.levels):
                log.msg(f"[PS] Level {i} = {val} -> {level_to_dbm(val):.1f} dBm")
            self.set_txpower_level(self.level_index)

        if not self.enabled:
            self._transition_to("disabled")
        else:
            self._transition_to("active")

    @property
    def armed(self):
        """Обратная совместимость: True если активная регулировка (active)."""
        return self._current_state and self._current_state.name() == "active"

    @property
    def power_locked(self):
        """Обратная совместимость: True если locked после DISARM."""
        return self._current_state and self._current_state.name() == "locked"

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
        log.msg(f"[PS] State: {old_name} -> {state_name}")

    # ----- Публичный интерфейс (on_arm / on_disarm как раньше) -----

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
            self.set_minimum_power()
            self._transition_to("locked")

    def on_tx_power_command(self, action):
        """
        Вызывается при получении команды tx_power от ГС (increase/decrease).
        Выполняется только в состоянии active. Throttle: не чаще MIN_TIME_ON_LEVEL.
        """
        if not self.enabled or not self.levels:
            log.msg("[PS] Command ignored — power selection disabled")
            return
        if not self._current_state or self._current_state.name() != "active":
            log.msg(
                f"[PS] Command {action} ignored — state is "
                f"{self._current_state.name() if self._current_state else 'none'}"
            )
            return
        self._current_state.on_tx_power_command(action)

    def stop(self):
        if hasattr(self, "_lc_log") and self._lc_log and self._lc_log.running:
            self._lc_log.stop()
        self._lc_log = None
        log.msg("[PS] Stopped")

    def _cleanup(self):
        self.stop()

    def _log_drone_stats(self):
        freqsel = getattr(self.manager, "freqsel", None)
        if freqsel is None:
            return
        freq = freqsel.get_current_freq()
        link_status = getattr(self.manager, "link_status", None)
        rssi = None
        if link_status is not None and hasattr(link_status, "get_live_rssi"):
            rssi = link_status.get_live_rssi()
        if rssi is None:
            rssi = freqsel.get_current_rssi()
        per = freqsel.get_current_per()
        snr = freqsel.get_current_snr()

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
            flush=True,
        )

    def set_txpower_level(self, level_index):
        if not self.levels:
            log.msg("[PS] ERROR: power_sel_levels empty")
            return
        if level_index < 0 or level_index >= len(self.levels):
            log.msg(f"[PS] ERROR: invalid level index {level_index}")
            return
        prev_value = self.levels[self.level_index] if 0 <= self.level_index < len(self.levels) else None
        self.level_index = level_index
        new_value = self.levels[self.level_index]

        for wlan in self.manager.wlans:
            call_and_check_rc("iw", "dev", wlan, "set", "txpower", "fixed", str(new_value))

        if prev_value is not None and prev_value != new_value:
            log.msg(
                f"[PS] TX power: {level_to_dbm(prev_value):.1f} -> {level_to_dbm(new_value):.1f} dBm"
            )
        else:
            log.msg(f"[PS] TX power set to {level_to_dbm(new_value):.1f} dBm")

    def increase_txpower_level(self):
        if self.level_index < len(self.levels) - 1:
            old = self.level_index
            self.set_txpower_level(self.level_index + 1)
            log.msg(
                f"[PS] TX power INCREASED: level {old} -> {self.level_index} "
                f"({level_to_dbm(self.levels[old]):.1f} -> {level_to_dbm(self.levels[self.level_index]):.1f} dBm)"
            )

    def decrease_txpower_level(self):
        if self.level_index > 0:
            old = self.level_index
            self.set_txpower_level(self.level_index - 1)
            log.msg(
                f"[PS] TX power DECREASED: level {old} -> {self.level_index} "
                f"({level_to_dbm(self.levels[old]):.1f} -> {level_to_dbm(self.levels[self.level_index]):.1f} dBm)"
            )

    def set_minimum_power(self):
        """Минимальный уровень (индекс 0). Вызывается при DISARM."""
        if not self.enabled or not self.levels:
            return
        if self.level_index != 0:
            log.msg("[PS] Power set to MINIMUM (disarm)")
        self.set_txpower_level(0)
