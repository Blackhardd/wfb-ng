"""
Power Selection - управление мощностью передатчика ТОЛЬКО на ДРОНЕ.

TODO: сделать более прозрачный нейминг и описать логику работы для себя
TODO: Добавить(вернуть) адаптивный режим по RSSI
INFO: пока что не решена проблема с тем, как высчитывать растояние
У нас слишком большой разброс по RSSI и PER, score - спасает но не так как ожидалось
Нужно обыграть резкие скачки, определить как и когда у нас резкий скачек а когда нет
Плавность в дальнейшей работе - приоритет для нормального результата работы
21.02.2026 - закоментировал весь код адаптивного режима смены мощности по RSSI
21.02.2026 - нашел ошибку где сет-ил сразу на все wlan мощность в DB что могло создавать помехи
"""

from twisted.python import log

from . import sich_wlan_helper
from .conf import settings


# ==================== НАСТРОЙКИ ====================

# Позволяю или не позволяю работать коду управления мощностью передатчика
def global_power_selection_mode():
    """True = включено, False = выключено. В конфиге: power_selection_mode = True/False"""
    v = getattr(settings.common, "power_selection_mode", False)
    if isinstance(v, bool):
        return v
    # Обратная совместимость со строками "enable"/"disable"
    return str(v).lower().strip() == "enable"
power_selection_level_list = settings.common.power_selection_levels

def level_to_dbm(value):
    """Перевод значения уровня мощности в dBm"""
    return value / 100.0


# ==================== Состояния ====================

#сделал класс для состояний просто базовый класс для всех состояний
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

    def on_disarm(self):
        pass


class LockedState(PowerSelectionState):
    """
    Disarm- первый уровень из power_selection_levels
    """

    def name(self):
        return "locked"

    def on_enter(self):
        # проверяю на enable or disable из wifibraodcast.cfg
        if not global_power_selection_mode():
            return
        self.ps.set_minimum_power()


class ActiveState(PowerSelectionState):
    """
    Armed/Connected/Lost/Recovery- последний уровень из power_selection_levels
    """
    def name(self):
        return "active"

    def on_enter(self):
        # проверяю на enable or disable из wifibraodcast.cfg
        if not global_power_selection_mode():
            return
        self.ps.set_maximum_power()


# ==================== Основной класс (дрон) ====================

class PowerSelection:
    """
    Управление мощностью: disarm - min, armed/connected/lost/recovery оставляю в максимальном значении
    """

    def __init__(self, manager):
        self.manager = manager # ссылаюсь на менеджеер дрона
        self.enabled = global_power_selection_mode()
        self.levels = power_selection_level_list
        self.level_index = 0 # индекс текущего уровня мощности из списка power_selection_level_list

        self._current_state = None # текущее состояние
        self._states = {
            "locked": LockedState(self),
            "active": ActiveState(self),
        }
        if global_power_selection_mode():
            self._transition_to("active")


    # через это место по моей задумке идет вся смена состояний
    def _transition_to(self, state_name):
        if state_name not in self._states:
            log.msg(f"[Power Selection] Class PowerSelection - неизвестное состояние: {state_name}")
            return
        new_state = self._states[state_name]
        old_name = self._current_state.name() if self._current_state else "none"
        if old_name == state_name:
            return
        if self._current_state:
            self._current_state.on_exit()
        self._current_state = new_state
        self._current_state.on_enter()
        log.msg(f"[Power Selection] Class PowerSelection - состояние изменено: {old_name} -> {state_name}")

    def on_arm(self):
        if self._current_state:
            self._current_state.on_arm()
        if self.enabled:
            self._transition_to("active")

    def on_connected(self):
        if self.enabled:
            self._transition_to("active")

    def on_disarm(self):
        if self._current_state:
            self._current_state.on_disarm()
        if self.enabled:
            self._transition_to("locked")

    def stop(self):
        log.msg("[PS] Stopped")
        
    def set_txpower_level(self, level_index):
        """
        Единая точка изменения мощности. При power_selection_mode=False - iw не вызывается.
        """
        if not global_power_selection_mode():
            return
        if not self.levels:
            return
        if level_index < 0 or level_index >= len(self.levels):
            return
        prev_value = self.levels[self.level_index] if 0 <= self.level_index < len(self.levels) else None
        self.level_index = level_index
        new_value = self.levels[self.level_index]
        for wlan in self.manager.wlans:
            sich_wlan_helper.set_txpower(wlan, new_value)
        if prev_value is not None and prev_value != new_value:
            log.msg(f"[PS] TX power: {level_to_dbm(prev_value):.1f} -> {level_to_dbm(new_value):.1f} dBm")

    def set_minimum_power(self):
        """Первый уровень из power_selection_levels."""
        if not global_power_selection_mode() or not self.levels:
            return
        self.set_txpower_level(0)

    def set_maximum_power(self):
        """Последний уровень из power_selection_levels."""
        if not global_power_selection_mode() or not self.levels:
            return
        self.set_txpower_level(len(self.levels) - 1)
