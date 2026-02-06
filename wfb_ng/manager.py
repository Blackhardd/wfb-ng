import json
import time
from twisted.python import log
from twisted.internet import reactor, protocol, task, defer
from twisted.internet.protocol import ReconnectingClientFactory

from .src.selections.frequency_selection import FrequencySelection
from .src.selections.power_selection import PowerSelection, GSPowerController
from .src.core.status import StatusManager
from .src.core.data import ConnectionMetricsManager, DataHandler
from .conf import settings


# Отвечает за отправку команд на GS и обратно по JSON через TCP socket (для команд ARM/DISARM)
class ManagerJSONClient(protocol.Protocol): 
    def __init__(self, manager):
        self.manager = manager
        self._queue = []
        self._waiting = False
        self._buffer = b""

    def _process_queue(self): # обработка очереди команд
        while self._queue:
            command, deferred = self._queue.pop(0)
            try:
                self._waiting = True
                self.transport.write(json.dumps(command).encode())
                self._response = deferred
                break
            except Exception as e:
                deferred.errback(e)

    def connectionMade(self): # вызывается при установке соединения
        self.manager.on_connected()
        self._process_queue()

    def connectionLost(self, reason): # вызывается при разрыве соединения
        self.manager.on_disconnected(reason)

    def dataReceived(self, data): # обработка полученных данных - сАмое главнвые действия
        self._buffer += data
        try:
            msg = json.loads(self._buffer.decode())
            self._buffer = b""
            if hasattr(self, "_response") and self._response and not self._response.called:
                self._response.callback(msg)
                self._response = None
                self._waiting = False
                self._process_queue()
        except json.JSONDecodeError:
            pass

    def send_command(self, command): # отправка команды
        log.msg("Sending command:", command)
        d = defer.Deferred()
        self._queue.append((command, d))
        if self.transport and not self._waiting:
            self._process_queue()
        return d

# Фабрика для управления подключением к GS и Drone по JSON через TCP socket
class ManagerJSONClientFactory(ReconnectingClientFactory): 
    protocol = ManagerJSONClient
    noisy = False
    maxDelay = 1.0

    def __init__(self, manager):
        ReconnectingClientFactory.__init__(self)

        self.manager = manager
        self.protocol_instance = None

    def buildProtocol(self, addr): # создание экземпляра протокола
        self.resetDelay()
        p = self.protocol(self.manager)
        self.protocol_instance = p
        return p
    
    def clientConnectionLost(self, connector, reason): # обработка потери соединения
        log.msg("Manager connection lost:", reason)
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason): # обработка неудачного соединения
        log.msg("Manager connection failed:", reason)
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
    
    def send_command(self, command): # отправка команды
        """
        Отправить команду через protocol instance
        
        Returns:
            Deferred если соединение готово, None если соединение не установлено
        """
        if self.protocol_instance:
            return self.protocol_instance.send_command(command)
        # Соединение не готово - возвращаем None
        # Вызывающий код должен проверять на None!
        return None

# Серверная сторона - GS или Drone - принимает команды от GS и отправляет ответы
class ManagerJSONServer(protocol.Protocol): 
    def __init__(self, manager):
        self.manager = manager

    def send_response(self, obj):
        log.msg(f"Sending response: {obj}")
        peer = self.transport.getPeer()
        if peer.host == "127.0.0.1":
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            frame = len(body).to_bytes(4, "big") + body
        else:
            frame = json.dumps(obj).encode("utf-8")
        self.transport.write(frame)

    def connectionMade(self): # вызывается при установке соединения
        peer = self.transport.getPeer()
        if peer.host != "127.0.0.1":
            self.manager.on_connected()

    def connectionLost(self, reason): # вызывается при разрыве соединения
        self.manager.on_disconnected(reason)

    def dataReceived(self, data): # обработка полученных данных - сАмое главнвые действия
        try:
            message = json.loads(data.decode("utf-8"))
            command = message.get("command")

            response = {"status": "success"}
            
            if command == "init":
                if message["freq_sel"]["enabled"] and self.manager.frequency_selection.is_enabled():
                    pass
            elif command == "freq_sel_hop":
                if self.manager.frequency_selection.is_enabled():
                    action_time = message.get("action_time")
                    target_channel = None
                    if message.get("target_freq"):
                        target_freq = message.get("target_freq")
                        target_channel = self.manager.frequency_selection.channels.by_freq(target_freq)
                        if target_channel:
                            kind = "recovery" if message.get("is_recovery") else "hop"
                            log.msg(f"[FS {kind}] Drone received command to {target_freq} MHz")
                        else:
                            log.msg(f"WARNING: Target freq {target_freq} MHz not found in channels")
                    response["time"] = self.manager.frequency_selection.schedule_hop(
                        action_time=action_time,
                        channel=target_channel,
                        is_recovery=message.get("is_recovery", False),
                    )
            elif command == "update_config":
                self.manager.update_config(message.get("settings"))

            elif command == "tx_power":
                action = message.get("action")
                if hasattr(self.manager, 'power_selection') and self.manager.power_selection and action:
                    self.manager.power_selection.on_tx_power_command(action)
                    response["level"] = self.manager.power_selection.level_index
                else:
                    response["status"] = "error"
                    response["error"] = "tx_power not available or invalid action"

            self.send_response(response)
        except json.JSONDecodeError:
            self.send_response({"status": "error"})

# Фабрика для управления серверным соединением GS или Drone по JSON через TCP socket
class ManagerJSONServerFactory(protocol.ServerFactory):
    protocol = ManagerJSONServer

    def __init__(self, manager):
        self.manager = manager

    def buildProtocol(self, addr):
        return self.protocol(self.manager)


# Глобальный определяющий класс менеджера - GS или Drone
class Manager:
    _is_connected = False # Флаг соединения с GS или Drone

    def __init__(self, config, wlans):
        self.config = config
        self.wlans = wlans

        # 1. Компонент менеджера - получение сырых данных от wfb_rx
        stats_port = getattr(settings, self.get_type()).stats_port
        self.data_handler = DataHandler(stats_port=stats_port)

        # 2. Компонент менеджера - выбор частоты
        self.frequency_selection = FrequencySelection(self)

        # 3. Компонент менеджера - метрики связи
        self.metrics_manager = ConnectionMetricsManager(initial_freq=settings.common.wifi_channel)

        # 4. Компонент менеджера - управление статусами устройств
        self.status_manager = None

        # 5. Компонент менеджера - инициируем "пайплайн"
        self._setup_data_pipeline()

    def _setup_data_pipeline(self):
        """
        Подключаем потоки данных:

        DataHandler -> metrics_manager
        DataHandler -> frequency_selection.channels
        DataHandler -> status_manager.on_packet_received (если есть)

        Источник один — stats от wfb_rx по любому потоку (video/mavlink/tunnel).
        Как только по любому из потоков приходят данные — считаем «пакет получен» для статуса связи.
        """
        self.metrics_manager.connect_to(self.data_handler)

        if hasattr(self, 'frequency_selection') and hasattr(self.frequency_selection, 'channels'):
            self.data_handler.add_callback(self.frequency_selection.channels.on_stats_received)

        # Любая доставка stats по радиоканалу (любой поток) -> событие «пакет получен» для StatusManager.
        # Не зависим от mavlink: работает при любом потоке (video/mavlink/tunnel).
        self.data_handler.add_callback(self._on_radio_stats_for_status)

    def _on_radio_stats_for_status(self, rx_id, stats_dict):
        """Уведомляем StatusManager только когда реально принят хотя бы один пакет (не просто приход stats с PER 100%)."""
        if not getattr(self, 'status_manager', None):
            return
        p_total = stats_dict.get('p_total', 0)
        p_bad = stats_dict.get('p_bad', 0)
        if p_total > 0 and (p_total - p_bad) > 0:
            self.status_manager.on_packet_received()

    def get_type(self):
        """
        Что инициализируется: GS менеджер = _type = "gs" , _type = "drone"
        """
        return self._type
    
    def is_connected(self):
        return self._is_connected
    
    def on_connected(self):
        log.msg("Management connection established")

    def on_disconnected(self, reason):
        log.msg(f"Management connection closed: {reason}")
        self._is_connected = False

    def update_config(self, data): # обновляем cfg по секциям
        from .conf import wfb_ng_cfg, user_settings

        for section_name, section_data in data.items():
            if not user_settings.has_section(section_name):
                user_settings.add_section(section_name)

            section = user_settings.get_section(section_name)

            for name, value in section_data.items():
                section.set(name, value)

        user_settings.save_to_file(wfb_ng_cfg)
    
    def _cleanup(self):
        """
        Очистка ресурсов менеджера при остановке.
        """
        if hasattr(self, 'status_manager') and self.status_manager:
            self.status_manager.stop()

# Менеджер что запускается на пульте
class GSManager(Manager):
    _type = "gs"

    def __init__(self, config, wlans):
        super().__init__(config, wlans)

        # StatusManager — управляет статусами соединения
        self.status_manager = StatusManager(config, wlans, manager=self)

        # Подключаем metrics_manager -> status_manager (данные уже идут из DataHandler в metrics_manager)
        self.metrics_manager.set_metrics_callback(self.status_manager.update_metrics)

        # Интегрируем frequency_selection в status_manager через subscribe_to_status_events
        self.frequency_selection.subscribe_to_status_events(self.status_manager)

        # Запуск единого DataHandler
        reactor.callWhenRunning(self.data_handler.start)

        # Create management client
        self.client_f = ManagerJSONClientFactory(self)
        reactor.connectTCP("10.5.0.2", 14888, self.client_f)

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14889, self.server_f)

        # Контроллер мощности: GS по своему RSSI отправляет команды дрону (increase/decrease)
        self.power_controller = GSPowerController(self)
        reactor.callWhenRunning(self.power_controller.start)

    def on_connected(self):
        super().on_connected()
        if not self._is_connected:
            d = self.client_f.send_command({
                "command": "init",
                "freq_sel": {"enabled": self.frequency_selection.is_enabled()}
            })
            
            # Защита: send_command может вернуть None если соединение не готово
            if d is None:
                log.msg("ERROR: send_command returned None, connection not ready")
                return

            d.addCallback(self.on_connection_ready)
            d.addErrback(lambda err: log.msg("Error initializing connection:", err))

    def on_connection_ready(self, message): 
        if not message["status"] == "success":
            log.msg("Failed to prepare connection:", message)
            return

        self._is_connected = True

    def update_config(self, data):
        super().update_config(data)

    def _cleanup(self):
        if hasattr(self, 'power_controller') and self.power_controller:
            self.power_controller.stop()
        super()._cleanup()

# Менеджер что запускается на дроне
class DroneManager(Manager):
    _type = "drone"

    def __init__(self, config, wlans):
        log.msg("[DroneManager] ========== INITIALIZATION START ==========")
        super().__init__(config, wlans)
        self.status_manager = StatusManager(config, wlans, manager=self)

        # PowerSelection — адаптивная мощность передатчика (только на дроне)
        self.power_selection = PowerSelection(self)
        self.status_manager.subscribe("status_changed", self._on_status_changed_for_power)

        # metrics_manager уже инициализирован в Manager, подключаем к status_manager через set_metrics_callback
        self.metrics_manager.set_metrics_callback(self.status_manager.update_metrics)

        self.frequency_selection.subscribe_to_status_events(self.status_manager)

        # Запуск единого DataHandler (RSSI/PER/SNR пойдут в metrics_manager и на дрон)
        reactor.callWhenRunning(self.data_handler.start)

        # Create management client to send commands to GS
        self.client_f = ManagerJSONClientFactory(self)
        reactor.connectTCP("10.5.0.1", 14889, self.client_f)

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14888, self.server_f)

    def _on_status_changed_for_power(self, old_status, new_status):
        """При смене статуса: connected/disarmed -> минимум мощности; armed -> управление по RSSI с GS."""
        if new_status == self.status_manager.STATUS_ARMED:
            self.power_selection.on_arm()
        elif new_status == self.status_manager.STATUS_DISARMED:
            self.power_selection.on_disarm()
            self.power_selection.set_minimum_power()
        elif new_status == self.status_manager.STATUS_CONNECTED:
            self.power_selection.set_minimum_power()

    def _cleanup(self):
        if hasattr(self, 'power_selection') and self.power_selection:
            self.power_selection.stop()
        super()._cleanup()


# Фабрика, что запускает тот или иной менеджер в файле services.py
class ManagerFactory:
    _registry = {
        "gs": GSManager,
        "drone": DroneManager
    }

    @classmethod
    def create(self, profile, config, wlans):
        manager_class = self._registry.get(profile)
        if manager_class:
            return manager_class(config, wlans)
        raise ValueError(f"Unknown profile: {profile}")
