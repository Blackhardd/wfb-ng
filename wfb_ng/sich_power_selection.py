"""
Power Selection — управление мощностью передатчика (только на дроне).

TODO: сделать более прозрачный нейминг и описать логику работы для себя
TODO: Добавить(вернуть) адаптивный режим по RSSI
INFO: пока что не решена проблема с тем, как высчитывать растояние
У нас слишком большой разброс по RSSI и PER, score - спасает но не так как ожидалось
Нужно обыграть резкие скачки, определить как и когда у нас резкий скачек а когда нет
Плавность в дальнейшей работе - приоритет для нормального результата работы
21.02.2026 - закоментировал весь код адаптивного режима смены мощности по RSSI
21.02.2026 - нашел ошибку где сет-ил сразу на все wlan мощность в DB что могло создавать помехи


"""

import os
import re
import subprocess
import time

from twisted.python import log
from twisted.internet import task

from . import call_and_check_rc
from .conf import settings


# ==================== НАСТРОЙКИ ====================

power_selection_switcher    = settings.common.power_sel_enabled
power_selection_level_list  = settings.common.power_sel_levels

# Для будущего адаптивного режима (RSSI с GS):
# RSSI_INCREASE_THRESHOLD   = -48 # увиличиваем мощность при RSSI < -48
# RSSI_DECREASE_THRESHOLD   = -32 # уменьшаем мощность при RSSI > -32
# PER_DECREASE_MAX          = 80  # максимальный PER для уменьшения мощности
# MIN_TIME_ON_LEVEL         = 8.0 # минимальное время на уровне
# GS_POWER_CHECK_INTERVAL   = 2.0 # интервал проверки мощности


# def _throttle_elapsed(last_time, interval=MIN_TIME_ON_LEVEL):
#     return (time.time() - last_time) >= interval


def level_to_dbm(value):
    """Перевод значения уровня мощности в dBm"""
    return value / 100.0


def find_wifi_wlan():
    """
    Нахожу определенный драйвер в определенной директории sys/class/net.
    Раньше просто получал wlan без уточнения какой именно это wlan
    Теперь конкректизирую получение wlan
    """
    supported = ("rtl88xxau_wfb", "rtl88x2eu")
    result = []
    directory = "/sys/class/net"
    if not os.path.isdir(directory):
        return result
    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        path = os.path.join(directory, name)
        if not os.path.islink(path):
            continue
        driver_link = os.path.join(path, "device", "driver")
        if not os.path.islink(driver_link):
            continue
        try:
            driver = os.path.basename(os.readlink(driver_link))
            if driver in supported:
                result.append(name)
        except (OSError, ValueError):
            pass
    return result

_cached_wlan_list = None

def get_txpower_from_device(wlans=None):
    """Получить текущую TX power с устройства в dBm или None."""
    global _cached_wlan_list
    if wlans:
        wlan_list = wlans
    else:
        if _cached_wlan_list is None:
            _cached_wlan_list = find_wifi_wlan()
        wlan_list = _cached_wlan_list
    if not wlan_list:
        return None
    wlan = wlan_list[0] if isinstance(wlan_list, (list, tuple)) else wlan_list
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

#сделал класс для состояний просто базовый класс для всех состояний
# что бы наследовать от него и не писать одно и тоже в каждом состоянии
class PowerSelectionState:
    def __init__(self, ps):
        self.ps = ps

    def name(self): # название состояния
        return "base"

    def on_enter(self): # действие при входе в состояние
        pass

    def on_exit(self): # действие при выходе из состояния
        pass

    def on_arm(self): # действие при взводе
        pass

    def on_disarm(self): # действие при сбросе
        pass

    # Не используется
    # def on_check_signal(self, rssi):
    #     pass


class DisabledState(PowerSelectionState):
    """
    power_sel_enabled=False: всегда max мощность wifi передатчика
    """

    def name(self):
        return "disabled"

    def on_enter(self):
        self.ps.set_maximum_power()
        log.msg("[PS] Power selection disabled → MAX in all modes")

    # def on_check_signal(self, rssi):
    #     # Адаптивный режим по RSSI - для адаптива, пока закоментировал
    #     pass


class LockedState(PowerSelectionState):
    """
    Disarm- первый уровень из power_sel_levels
    """

    def name(self):
        return "locked"

    def on_enter(self):
        self.ps.set_minimum_power()
        log.msg("[PS] Установлено 16dBm в Disarm")


class ActiveAdjustmentState(PowerSelectionState):
    """
    Armed/Connected/Lost/Recovery- последний уровень из power_sel_levels
    """

    def name(self):
        return "active"

    def on_enter(self):
        self.ps.set_maximum_power()
        log.msg("[PS] Power MAX (armed/connected)")

    # def on_check_signal(self, rssi):
    #     # Адаптивный режим по RSSI - для будущего расширения
    #     pass


# ==================== Контроллер мощности на GS ====================

class GSPowerController:
    """
    TODO в будущем вернуть адаптивный режим.
    Сейчас заглушка. Простой режим.
    Для адаптивного - читает RSSI и шлёт tx_power команды дрону.
    """

    def __init__(self, manager):
        self.manager = manager
        self._last_command_time = 0.0 #время от последней отправки команды
        self._lc = None #повторитель, пустая коробка

    def start(self):
        switcher = power_selection_switcher # True or False 
        levels   = power_selection_level_list # [1600, 2600]
        if not switcher or not levels:
            log.msg("[GS Power] Отключен через конфин")
            return
        log.msg("[GS Power] В Disarm 16dBm, в Armed/Connected 26dBm")

    def stop(self):
        # проверяю что есть loopcall и он работает
        if self._lc and self._lc.running:
            self._lc.stop()

    # закоментил, для адаптива пока что
    # def _check_and_send(self):
    #     """
    #     Для будущего адаптивного режима
    #     """
    #     # проверяю что есть клиент и он подключен
    #     if not getattr(self.manager, 'client_f', None) or not self.manager.is_connected():
    #         return
    #     # проверяю что статус дрона armed
    #     if not getattr(self.manager, 'status_manager', None) or not self.manager.status_manager.is_armed():
    #         return
    #     metrics = None
    #     # проверяю что есть metrics_manager и он не пустой
    #     if getattr(self.manager, 'metrics_manager', None) and self.manager.metrics_manager:
    #         metrics = self.manager.metrics_manager.get_metrics()
    #     rssi = metrics.get('rssi') if metrics else None
    #     per = metrics.get('per') if metrics else None
    #     # проверяю что rssi не пустой и не равен 0
    #     if rssi is None or rssi == 0:
    #         return
    #     # проверяю что прошло достаточно времени с последней команды
    #     if not _throttle_elapsed(self._last_command_time):
    #         return
    #     action = None
    #     if rssi < RSSI_INCREASE_THRESHOLD:
    #         action = "increase"
    #     elif rssi > RSSI_DECREASE_THRESHOLD:
    #         if per is not None and per > PER_DECREASE_MAX:
    #             return
    #         action = "decrease"
    #     if not action:
    #         return
    #     self._last_command_time = time.time()
    #     cmd = {"command": "tx_power", "action": action}
    #     d = self.manager.client_f.send_command(cmd)
    #     if d:
    #         log.msg(f"[GS Power] tx_power {action} (RSSI {rssi} dBm)")
    #         d.addErrback(lambda err: log.msg(f"[GS Power] Command failed: {err}"))


# ==================== Основной класс (дрон) ====================

class PowerSelection:
    """
    Управление мощностью: disarm - min, armed/connected/lost/recovery оставляю в максимальном значении
    """

    def __init__(self, manager):
        self.manager = manager # ccылаюсь на менеджеер дрона,
        self.enabled = power_selection_switcher # обьявляю перем-у в инит для удобства
        self.levels = power_selection_level_list # обьявляю перем-у с списком в инит для удобства

        self._current_state = None # пустая коробка, пока не поменял - там ничего не будет. Заглушечка.
        self._states = {
            "disabled": DisabledState(self), # состояние disabled, когда power_sel_enabled=False
            "locked":   LockedState(self),   # состояние locked, когда power_sel_enabled=True и взводе
            "active":   ActiveAdjustmentState(self), # состояние active, когда power_sel_enabled=True и connected
        }

        self.level_index = 0
        # Для адаптива:
        # self._last_change_time = 0.0
        # self._last_command_time = 0.0
        # self._lc_log = None
        # self._lc_check = None

        if self.enabled and self.levels:
            for level_index, level_value in enumerate(self.levels):
                log.msg(f"[PS] Уровень {level_index} = {level_value} -> {level_to_dbm(level_value):.1f} dBm")

        if not self.enabled:
            self._transition_to("disabled")
        else:
            self._transition_to("active")


    # через это место по моей задумке идет вся смена состояний
    # условно _transition_to и дальше указываем в какое состояние переходим
    # ниже так же проверки
    # TODO: сделать более прозрачный нейминг 
    def _transition_to(self, state_name):
        if state_name not in self._states:
            log.msg(f"[PS] Unknown state: {state_name}")
            return
        new_state = self._states[state_name]
        old_name = self._current_state.name() if self._current_state else "none"
        if old_name == state_name:
            return
        if self._current_state:
            self._current_state.on_exit()
        self._current_state = new_state
        self._current_state.on_enter()
        log.msg(f"[PS] State changed: {old_name} -> {state_name}")

    def on_arm(self):
        if self._current_state:
            self._current_state.on_arm()
        if self.enabled:
            self._transition_to("active")
        else:
            log.msg("[PS] ARM, но управление мощностью отключено")

    def on_connected(self):
        if self.enabled:
            self._transition_to("active")

    def on_disarm(self):
        if self._current_state:
            self._current_state.on_disarm()
        if self.enabled:
            self._transition_to("locked")

    def on_tx_power_command(self, action):
        """
        В простом режиме не используется (только min/max). 
        Для адаптива — обработать increase/decrease.
        """
        pass

    def stop(self):
        # Для адаптива:
        # if hasattr(self, "_lc_check") and self._lc_check and self._lc_check.running:
        #     self._lc_check.stop()
        # if hasattr(self, "_lc_log") and self._lc_log and self._lc_log.running:
        #     self._lc_log.stop()
        log.msg("[PS] Stopped")

    # Для адаптива (периодическая проверка RSSI):
    # def _check_signal(self):
    #     if not self._current_state:
    #         return
    #     metrics = None
    #     if hasattr(self.manager, 'metrics_manager') and self.manager.metrics_manager:
    #         metrics = self.manager.metrics_manager.get_metrics()
    #     rssi = metrics.get('rssi') if metrics else None
    #     self._current_state.on_check_signal(rssi)

    def set_txpower_level(self, level_index):
        if not self.levels:
            return
        if level_index < 0 or level_index >= len(self.levels):
            return
        prev_value = self.levels[self.level_index] if 0 <= self.level_index < len(self.levels) else None
        self.level_index = level_index
        new_value = self.levels[self.level_index]
        for wlan in self.manager.wlans:
            call_and_check_rc("iw", "dev", wlan, "set", "txpower", "fixed", str(new_value))
        if prev_value is not None and prev_value != new_value:
            log.msg(f"[PS] TX power: {level_to_dbm(prev_value):.1f} -> {level_to_dbm(new_value):.1f} dBm")

    # Для адаптива (шаги по уровням):
    # def increase_txpower_level(self):
    #     if self.level_index < len(self.levels) - 1:
    #         self.set_txpower_level(self.level_index + 1)
    #
    # def decrease_txpower_level(self):
    #     if self.level_index > 0:
    #         self.set_txpower_level(self.level_index - 1)

    def set_minimum_power(self):
        """Первый уровень из power_sel_levels (disarm)."""
        if not self.enabled or not self.levels:
            return
        self.set_txpower_level(0)

    def set_maximum_power(self):
        """Последний уровень из power_sel_levels. Работает и при enabled=False."""
        if not self.levels:
            return
        self.set_txpower_level(len(self.levels) - 1)
