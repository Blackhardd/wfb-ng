"""
Power Selection - управление мощностью передатчика (ГС  Дрон)
Явное разделение: BasePowerSelection + GSPowerSelection (ГС) + DronePowerSelection (Дрон)

Связка со StatusManager (sich_status_manager):
  StatusManager ведает состоянием связи: waiting -> connected -> armed / disarmed, а также lost, recovery.
  При каждой смене статуса вызывается manager.on_status_changed(old_status, new_status).
  На ДРОНЕ (DroneManager) в on_status_changed дергается power_controller:
    - new_status == STATUS_DISARMED -> power_controller.on_disarm()  -> PowerSelection "locked"  -> TX мощность = мин (level 0)
    - new_status == STATUS_ARMED     -> power_controller.on_arm()     -> PowerSelection "active" -> мощность по командам с ГС
    - new_status in (STATUS_CONNECTED, STATUS_LOST, STATUS_RECOVERY) -> power_controller.on_connected() -> "active"
    - new_status == STATUS_WAITING  -> ничего; при старте PowerSelection уже в "active", уровень 0 до первой команды по RSSI
  Итого: "active" = всё кроме disarm (мощность по командам/RSSI), "locked" = только disarm (жёстко мин).

Порядок вызовов при изменении мощности:
  Везде один тип команды: set_level с level_index (индекс в power_selection_levels). Минимум = 0, максимум = len(levels)-1.
  ГС:
  #1 heartbeat -> power_rssi_listener -> по RSSI вызывается power_controller.request_power_change(level_index)
  #2 request_power_change(level_index) -> отправка power_command с action=set_level, level_index
  #4 ответ от дрона -> _power_at_drone_time -> планируем set_txpower_level(level_index) на action_time
  #5 в момент action_time на ГС и Дроне срабатывает _do_power_action(level_index) -> set_txpower_level(level_index) (iw)
  Дрон:
  #3 приход power_command -> handle_power_command -> планируем set_txpower_level(level_index) на action_time, возвращаем time и level_index
"""

from twisted.python import log
from twisted.internet import reactor

from . import sich_wlan_helper
from .conf import settings

# ==================== НАСТРОЙКИ ====================
# Гистерезис: разные пороги для повышения и понижения, чтобы не было ping-pong при колебаниях RSSI
# По таблице: без фризов при drone_rssi ~32-42; фризы при 44-48. Поднимаем мощность до того, как станет слабо.
RSSI_RAISE_AT = -65          # RSSI <= этого - повышаем мощность (level_index = max)
RSSI_LOWER_AT = -35          # RSSI >= этого - понижаем (level_index = 0)
RSSI_COOLDOWN_SEC = 30
MIN_CHANGE_INTERVAL = 10.0   # минимум секунд между ЛЮБЫМИ изменениями мощности

POWER_CMD_SET_LEVEL = "set_level"   
POWER_COMMAND_DELAY_SEC = 1.0   # задержка для синхронизации ГС/Дрон


def global_power_selection_mode():
    v = getattr(settings.common, "power_selection_mode", False)
    if isinstance(v, bool):
        return v
    return str(v).lower().strip() in ("true", "enable", "1", "on")


power_selection_levels = getattr(settings.common, "power_selection_levels", []) 


def level_to_dbm(value):
    return value / 100.0


# ==================== Базовый класс (общее для ГС и Дрона) ====================
# Используется через наследников: GSPowerSelection (ГС) и DronePowerSelection (Дрон)
# Общее: уровни мощности, состояния locked/active, set_txpower_level, _do_power_action (#5)

class BasePowerSelection:
    def __init__(self, manager):
        self.manager = manager
        self.enabled = global_power_selection_mode()  # кэш - конфиг статичен
        self.levels = power_selection_levels
        self.level_index = 0 # индекс текущего уровня мощности, с него мы стартуем

        self._current_state = None
        self._states = {
            "locked": LockedState(self),
            "active": ActiveState(self),
        }

        # Список "ручек" таймеров (объекты DelayedCall из reactor.callLater)
        # Параметры вызова (func, args) хранит реактор
        #   reactor планирует "через 0.5 с вызвать _do_power_action(2)" и возвращает одну "ручку";
        #   мы кладём эту ручку в _pending_calls. При stop() по списку вызываем ручка.cancel() - запланированный вызов не произойдёт.
        # стоп мы делаем для того что бы не было лишних вызовов set_txpower_level
        self._pending_calls = []  


        if self.enabled:
            self._transition_to("active")
            # Сразу выставляем железо в уровень level_index (при старте = 0 = мин), чтобы не зависеть от дефолта драйвера
            if self.levels:
                self.set_txpower_level(self.level_index)
            log.msg("[PowerSelection] включён")
        else:
            log.msg("[PowerSelection] ВЫКЛЮЧЕН глобально")

    def _transition_to(self, state_name):
        if state_name not in self._states:
            log.msg(f"[PowerSelection] неизвестное состояние: {state_name}")
            return
        new_state = self._states[state_name]
        old_name = self._current_state.name() if self._current_state else "none"
        if old_name == state_name:
            return
        if self._current_state:
            self._current_state.on_exit()
        self._current_state = new_state
        self._current_state.on_enter()
        log.msg(f"[PowerSelection] состояние: {old_name} -> {state_name}")

    def on_arm(self):
        if self.enabled:
            self._transition_to("active")

    def on_connected(self):
        if self.enabled:
            self._transition_to("active")

    def on_disarm(self):
        if self.enabled:
            self._transition_to("locked")

    def set_txpower_level(self, level_index):
        if not self.enabled or not self.levels:
            return
        if level_index < 0 or level_index >= len(self.levels):
            return

        prev = self.levels[self.level_index] if 0 <= self.level_index < len(self.levels) else None
        self.level_index = level_index
        new_value = self.levels[self.level_index]

        for wlan in getattr(self.manager, "wlans", ()):
            sich_wlan_helper.set_txpower(wlan, new_value)

        if prev is not None and prev != new_value:
            log.msg(f"[PS] TX power: {level_to_dbm(prev):.1f} -> {level_to_dbm(new_value):.1f} dBm")

    def set_minimum_power(self):
        self.set_txpower_level(0)

    def set_maximum_power(self):
        if self.levels:
            self.set_txpower_level(len(self.levels) - 1)

    def _do_power_action(self, level_index):
        # #5 фактическое переключение TX power на этой машине (ГС или Дрон) - один уровень из power_selection_levels
        self.set_txpower_level(level_index)

    def _schedule(self, delay, func, *args):
        """Запланировать вызов func(*args) через delay секунд. Ручку таймера сохраняем в _pending_calls, чтобы при stop() отменить."""
        call = reactor.callLater(delay, func, *args)
        self._pending_calls.append(call)

    def stop(self):
        """Отменить все запланированные таймеры (чтобы после остановки не сработал set_txpower)."""
        for call in self._pending_calls[:]:
            if call.active():
                call.cancel()
        self._pending_calls.clear()
        log.msg("[PowerSelection] stopped")


# ==================== Состояния (общие) ====================

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


# States - это паттерн "Состояние" (State pattern), используется для управления поведением PowerSelection в зависимости от контекста.
class LockedState(PowerSelectionState):
    def name(self):
        return "locked"

    def on_enter(self):
        # При входе в состояние 'locked' - TX мощность переводится в минимальный уровень (если включено и есть уровни)
        if self.ps.enabled and self.ps.levels:
            self.ps.set_txpower_level(0)

class ActiveState(PowerSelectionState):
    def name(self):
        return "active"

    def on_enter(self):
        # При входе в active ничего не делаем - мощность изменяется только явными командами
        pass


# ==================== GSPowerSelection - только на ГС ====================
# request_power_change(level_index) (#2), _power_at_drone_time (#4), по таймеру _do_power_action(level_index) (#5).
# handle_power_command на ГС не вызывается (заглушка ниже).

class GSPowerSelection(BasePowerSelection):
    """Версия для Ground Station (ГС). Может инициировать изменение мощности."""

    def request_power_change(self, level_index):
        # #2 ГС: отправить power_command дрону (set_level и плюс level_index); по ответу _power_at_drone_time (#4) планируем переключение на тот же level_index
        if not self.enabled:
            return
        # отправляю команду дрону , внутри (send_command_to_drone) будет вызван handle_power_command (Дрон)
        send = getattr(self.manager, "send_command_to_drone", None)
        
        # проверяю что send_command_to_drone callable, а callable это функция которая может быть вызвана
        # если не callable == не вызываем же ж 
        if not callable(send):
            log.msg("[GS PowerSelection] send_command_to_drone отсутствует")
            return
        if level_index is None or level_index < 0 or (self.levels and level_index >= len(self.levels)):
            log.msg(f"[GS PowerSelection] неверный level_index={level_index}")
            return
        payload = {"command": "power_command", "action": POWER_CMD_SET_LEVEL, "level_index": level_index}
        d = send(payload)
        if d is None:
            log.msg("[GS PowerSelection] TCP-канал не готов, power_command не отправлен")
            return
        d.addCallback(self._power_at_drone_time, level_index)
        d.addErrback(lambda f: log.msg(f"[GS PowerSelection] power_command failed: {f.getErrorMessage()}"))

    def _power_at_drone_time(self, response, level_index):
        # #4 ГС: получили от дрона time и level_index - планируем set_txpower_level(level_index) на action_time (#5)
        if not isinstance(response, dict):
            log.msg("[GS PowerSelection] Ответ от дрона не dict")
            return
        action_time = response.get("time")
        if action_time is None:
            log.msg("[GS PowerSelection] Нет 'time' в ответе от дрона")
            return
        if "level_index" not in response:
            log.msg("[GS PowerSelection] Нет 'level_index' в ответе - рассинхрон ГС/Дрон, отменяем переключение")
            return
        level_index = response["level_index"]
        now = reactor.seconds()
        delay = max(0.0, action_time - now)
        log.msg(f"[GS PowerSelection] Синхронное переключение через {delay:.2f}с (level_index={level_index})")
        self._schedule(delay, self._do_power_action, level_index)

    def handle_power_command(self, message):
        # На ГС этот метод не должен вызываться
        log.msg("[GS PowerSelection] handle_power_command вызван на ГС - игнорируется")
        return {"status": "not_applicable"}


# ==================== DronePowerSelection - только на Дроне ====================
# На дроне: handle_power_command (#3) принимает только set_level + level_index; по таймеру _do_power_action(level_index) (#5).
# request_power_change на дроне не вызывается, влепил заглушку - ии порекомендовал что бы обойти ошибки, как самому сделать иначе не смог придумтаь достойный вариант.

class DronePowerSelection(BasePowerSelection):
    """Версия для Drone. Принимает команды и планирует локальное переключение."""

    def handle_power_command(self, message):
        # #3 Дрон: приняли power_command (set_level + level_index) - планируем set_txpower_level(level_index) на action_time, возвращаем time и level_index (#4)
        if not self.enabled:
            return {"status": "disabled"}
        if not self.levels:
            return {"status": "error", "error": "no levels configured"}

        # message.get - метод словаря, возвращающий значение по ключу, зачем? если я передам не set_level то будет ошибка.По єтому проверяю.
        action = message.get("action")
        if action != POWER_CMD_SET_LEVEL:
            return {"status": "error", "error": f"unknown action {action!r}, use set_level"}

        level_index = message.get("level_index")
        if level_index is None:
            return {"status": "error", "error": "set_level requires level_index"}
        try:
            level_index = int(level_index)
        except (TypeError, ValueError):
            return {"status": "error", "error": "level_index must be int"}
        if level_index < 0 or level_index >= len(self.levels):
            return {"status": "error", "error": f"invalid level_index {level_index}"}

        action_time = reactor.seconds() + POWER_COMMAND_DELAY_SEC
        delay = max(0.0, action_time - reactor.seconds())
        log.msg(f"[Drone PowerSelection] set_level level_index={level_index}, переключение через {delay:.2f}с")
        self._schedule(delay, self._do_power_action, level_index)
        return {"status": "success", "time": action_time, "level_index": level_index}

    def request_power_change(self, level_index):
        # На дроне не вызывается
        log.msg("[Drone PowerSelection] request_power_change вызван на дроне - игнорируется")


# GSPowerListener - только на ГС
# Вызывается из heartbeat при приходе метрик; решает по RSSI и дергает power_controller.request_power_change (#1 -> #2).
# stop вызывается из GSManager._cleanup при "teardown" - отменяет таймеры ( т.е делает их None или 0 ) (для единообразия с power_controller).

class GSPowerListener:
    def __init__(self, manager):
        self.manager = manager # что делает менеджер тут в импорте? - Задает вопрос "он менеджер GS или Drone?Вот что мне интересно.
        self.enabled = global_power_selection_mode() # включен ли power selection
        self._last_change_time = 0.0 # время последнего изменения мощности
        self._cooldown_increase_until = 0.0 # когда можно будет увеличить мощность
        self._cooldown_decrease_until = 0.0 # когда можно будет уменьшить мощность
        self._pending_calls = []  # ручки таймеров reactor.callLater; при stop() отменяем (как в BasePowerSelection)

    def on_heartbeat(self, gs_metrics, drone_metrics):
        ##1 ГС: по RSSI решаем "увеличить/уменьшить" - вызываею power_controller.request_power_change(level_index)
        if not self.enabled or not drone_metrics:
            return

        rssi = drone_metrics.get("rssi")
        if rssi is None or rssi == "n/a":
            return

        try:
            rssi_val = int(rssi)
        except (TypeError, ValueError):
            return

        now = reactor.seconds()
        # reactor.seconds() - время в секундах (как time.time()), для таймеров в реакторе
        if now - self._last_change_time < MIN_CHANGE_INTERVAL:
            log.msg("[GSPowerListener] Пропуск: MIN_CHANGE_INTERVAL не прошёл")
            return

        ps = getattr(self.manager, "power_controller", None)
        if not ps or not getattr(ps, "levels", None):
            return
        max_index = len(ps.levels) - 1

        if rssi_val <= RSSI_RAISE_AT and now >= self._cooldown_increase_until:
            log.msg(f"[GSPowerListener] RSSI {rssi_val} ≤ {RSSI_RAISE_AT} -> level_index={max_index} (max)")
            self._cooldown_increase_until = now + RSSI_COOLDOWN_SEC
            self._last_change_time = now
            ps.request_power_change(max_index)

        elif rssi_val >= RSSI_LOWER_AT and now >= self._cooldown_decrease_until:
            log.msg(f"[GSPowerListener] RSSI {rssi_val} ≥ {RSSI_LOWER_AT} -> level_index=0 (min)")
            self._cooldown_decrease_until = now + RSSI_COOLDOWN_SEC
            self._last_change_time = now
            ps.request_power_change(0)

    def _schedule(self, delay, func, *args):
        call = reactor.callLater(delay, func, *args)
        self._pending_calls.append(call)

    def stop(self):
        for call in self._pending_calls[:]:
            if call.active():
                call.cancel()
        self._pending_calls.clear()
        log.msg("[GSPowerListener] stopped")