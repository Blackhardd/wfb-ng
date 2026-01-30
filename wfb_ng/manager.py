import json
import time
from twisted.python import log
from twisted.internet import reactor, protocol, task, defer
from twisted.internet.protocol import ReconnectingClientFactory

from .frequency_selection import FrequencySelection
from .power_selection import PowerSelection

class ManagerJSONClient(protocol.Protocol):
    def __init__(self, manager):
        self.manager = manager

        self._queue = []
        self._waiting = False
        self._buffer = b""

    def _process_queue(self):
        while self._queue:
            command, deferred = self._queue.pop(0)
            try:
                self._waiting = True
                self.transport.write(json.dumps(command).encode())
                self._response = deferred
                break
            except Exception as e:
                deferred.errback(e)

    def connectionMade(self):
        self.manager.on_connected()
        self._process_queue()

    def connectionLost(self, reason):
        self.manager.on_disconnected(reason)

    def dataReceived(self, data):
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

    def send_command(self, command):
        log.msg("Sending command:", command)
        d = defer.Deferred()
        self._queue.append((command, d))
        if self.transport and not self._waiting:
            self._process_queue()
        return d

class ManagerJSONClientFactory(ReconnectingClientFactory):
    protocol = ManagerJSONClient
    noisy = False
    maxDelay = 1.0

    def __init__(self, manager):
        ReconnectingClientFactory.__init__(self)

        self.manager = manager
        self.protocol_instance = None

    def buildProtocol(self, addr):
        self.resetDelay()
        p = self.protocol(self.manager)
        self.protocol_instance = p
        return p
    
    def clientConnectionLost(self, connector, reason):
        log.msg("Manager connection lost:", reason)
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        log.msg("Manager connection failed:", reason)
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
    
    def send_command(self, command):
        if self.protocol_instance:
            return self.protocol_instance.send_command(command)

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

    def connectionMade(self):
        peer = self.transport.getPeer()
        if peer.host != "127.0.0.1":
            self.manager.on_connected()

    def connectionLost(self, reason):
        self.manager.on_disconnected(reason)

    def dataReceived(self, data):
        try:
            message = json.loads(data.decode("utf-8"))
            command = message.get("command")

            response = {"status": "success"}
            
            if command == "init":
                if message["freq_sel"]["enabled"] and self.manager.freqsel.is_enabled():
                    pass
            elif command == "freq_sel_hop":
                if self.manager.freqsel.is_enabled():
                    response["time"] = self.manager.freqsel.schedule_hop()
            elif command == "update_config":
                self.manager.update_config(message.get("settings"))

            self.send_response(response)
        except json.JSONDecodeError:
            self.send_response({"status": "error"})

class ManagerJSONServerFactory(protocol.ServerFactory):
    protocol = ManagerJSONServer

    def __init__(self, manager):
        self.manager = manager

    def buildProtocol(self, addr):
        return self.protocol(self.manager)

class Manager:
    _is_connected = False

    def __init__(self, config, wlans):
        self.config = config
        self.wlans = wlans

        # Create manager components
        self.freqsel = FrequencySelection(self)
        # self.power_sel = PowerSelection(self)
        
        # StatusManager работает только на ground station (gs)
        # Не создаем для drone
        self.status_manager = None

    def get_type(self):
        return self._type
    
    def is_connected(self):
        return self._is_connected
    
    def on_connected(self):
        log.msg("Management connection established")

    def on_disconnected(self, reason):
        log.msg(f"Management connection closed: {reason}")
        self._is_connected = False

    def update_config(self, data):
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
        if hasattr(self, 'status_manager'):
            self.status_manager.stop()

class GSManager(Manager):
    _type = "gs"

    def __init__(self, config, wlans):
        super().__init__(config, wlans)
        
        ##### Sander 23.01.2026 Для понимания работы устройства. StatusManager только для GS
        self.status_manager = StatusManager(config, wlans, manager=self)

        # Create management client
        self.client_f = ManagerJSONClientFactory(self)
        reactor.connectTCP("10.5.0.2", 14888, self.client_f)

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14889, self.server_f)

    def on_connected(self):
        super().on_connected()
        if not self._is_connected:
            d = self.client_f.send_command({
                "command": "init",
                "freq_sel": {"enabled": self.freqsel.is_enabled()}
            })

            d.addCallback(self.on_connection_ready)
            d.addErrback(lambda err: log.msg("Error initializing connection:", err))

    def on_connection_ready(self, message): 
        if not message["status"] == "success":
            log.msg("Failed to prepare connection:", message)
            return

        self._is_connected = True

    def update_config(self, data):
        # self.client_f.send_command({
        #     "command": "update_config",
        #     "settings": data
        # })

        super().update_config(data)

class DroneManager(Manager):
    _type = "drone"

    def __init__(self, config, wlans):
        super().__init__(config, wlans)

        # Create management client to send commands to GS
        self.client_f = ManagerJSONClientFactory(self)
        reactor.connectTCP("10.5.0.1", 14889, self.client_f)

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14888, self.server_f)

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


class StatusManager:
    """
    Менеджер статусов.
    - Online > если получили пакет. 
    - Working > если получили arm команду
    - Reconnecting > если был или working или online и пакеты пропали
    - Lost > если пакетов не было больше 10-секунд и не было команды дизарм
    - Offline > если получили команду дизарм и не было пакетов 10 секунд
    """
    
    # Статусы устройства
    STATUS_OFFLINE = "offline"
    STATUS_ONLINE = "online"
    STATUS_WORKING = "working"
    STATUS_LOST = "lost"
    STATUS_RECONNECTING = "reconnecting"
    
    # Таймауты в секундах
    PACKET_TIMEOUT = 10.0  # Таймаут для определения потери связи
    DISARM_TIMEOUT = 10.0  # Таймаут после disarm для перехода в offline
    
    def __init__(self, config, wlans, manager=None):
        self._last_packet_time = None # Время = когда был последний пакет
        self._last_disarm_time = None # Время = когда был последний дизарм
        self._current_status = self.STATUS_OFFLINE # Текущий статус борта 
        self._status_change_callback = None # Калбе для уведомлений изменения статуса
        self._status_check_task = task.LoopingCall(self._check_status) # Задаем задачу для пееродического обновления инфы статуса
        self._status_check_task.start(1.0)  # Проверка каждую секунду
        self._manager = manager  # Ссылка на manager для доступа к freqsel
        
        log.msg("Good: StatusManager - init & start")
    
    def on_packet_received(self):
        """
        Вызываем время последнего пакета, и сразу обновляем время.
        """
        self._last_packet_time = time.time()
        
        # Если получили пакет - статус Online (если не Working)
        if self._current_status == self.STATUS_WORKING:
            # В статусе Working остаемся в Working при получении пакетов
            pass
        elif self._current_status != self.STATUS_ONLINE:
            # Из других статусов переходим в Online
            self._set_status(self.STATUS_ONLINE)
    
    def on_arm_command(self):
        """
        Получили arm? Переводим в статус Working.
        """
        self._last_disarm_time = None # Сбрасываем время дизарма при новом арме
        self._set_status(self.STATUS_WORKING)
        log.msg("Atention: StatusManager - arm command received, status -> Working")
    
    def on_disarm_command(self):
        """
        Получили disarm? Запоминает время ( для Ofline).
        """
        self._last_disarm_time = time.time() # Запоминаем время команды дизарм
        log.msg("Atention: StatusManager - disarm command received")
        if self._manager and hasattr(self._manager, 'freqsel'):
            self._manager.freqsel.reset_all_channels_stats()
    
    def _check_status(self):
        """
        Проверка статуса устройства ( главная логика по сути)
        """
        current_time = time.time()
        time_since_packet = current_time - self._last_packet_time if self._last_packet_time else None
        time_since_disarm = current_time - self._last_disarm_time if self._last_disarm_time else None
        
        # Sander 24.01.2026: Ограничиваем время отсчета дизарма. Если он был > 60 сек назад, 
        # то он уже не важен для логики перехода в offline и только засоряет логи.
        if time_since_disarm is not None and time_since_disarm > 60.0:
            self._last_disarm_time = None
            time_since_disarm = None
        
        # Периодическое логирование текущего статуса (каждые 5 секунд для отладки)
        if not hasattr(self, '_last_status_log_time'):
            self._last_status_log_time = current_time
        if current_time - self._last_status_log_time >= 5.0:
            self._last_status_log_time = current_time
            log_msg = f"StatusManager: Борт в: {self._current_status}"
            if time_since_packet is not None:
                log_msg += f" | Пакет был: {time_since_packet:.1f}s"
            if time_since_disarm is not None:
                log_msg += f" | Дизарм был: {time_since_disarm:.1f}s"
            log.msg(log_msg)
        
        # Проверка статуса офлайн: был дизарм и пакеты не получались больше 10 сек
        if self._last_disarm_time is not None:
            if time_since_packet is not None and time_since_packet > self.PACKET_TIMEOUT:
                # После дизарма пакеты перестали идти
                if self._current_status != self.STATUS_OFFLINE:
                    self._set_status(self.STATUS_OFFLINE)
                return
            elif time_since_packet is not None and time_since_packet <= self.PACKET_TIMEOUT:
                # Пакеты все еще идут после дизарма - если был Working, переходим в Online
                if self._current_status == self.STATUS_WORKING:
                    self._set_status(self.STATUS_ONLINE)
                # Но флаг _last_disarm_time НЕ СБРАСЫВАЕМ, ждем когда пакеты закончатся
        
        # Проверка онлайн - пакеты получались в последние 10 сек
        if time_since_packet is not None and time_since_packet <= self.PACKET_TIMEOUT:
            if self._current_status == self.STATUS_WORKING:
                # В статусе Working остаемся в Working при получении пакетов
                pass
            elif self._current_status != self.STATUS_ONLINE:
                self._set_status(self.STATUS_ONLINE)
            return
        
        # Пакеты не получаются в течение 10 сек
        if time_since_packet is None:
            # Никогда не получали пакеты - остаемся в текущем статусе
            pass
        elif time_since_packet > self.PACKET_TIMEOUT:
            # Обработка потери пакетов для разных статусов
            if self._current_status == self.STATUS_ONLINE and self._last_disarm_time is None:
                # Был Online, прошло > 10 сек без пакетов -> Reconnecting
                self._set_status(self.STATUS_RECONNECTING)
            elif self._current_status == self.STATUS_WORKING:
                # Был Working, прошло > 10 сек без пакетов
                if self._last_disarm_time is None:
                    # Не было disarm -> Reconnecting
                    self._set_status(self.STATUS_RECONNECTING)
                # Если был disarm, проверка Offline уже обработана выше
            elif self._current_status == self.STATUS_RECONNECTING:
                # Был Reconnecting, прошло еще > 10 сек (всего > 20 сек) -> Lost
                if self._last_disarm_time is None and time_since_packet > self.PACKET_TIMEOUT * 2:
                    # Не было disarm и прошло > 20 сек без пакетов -> Lost
                    self._set_status(self.STATUS_LOST)
                # Если был disarm, проверка Offline уже обработана выше
    
    def _set_status(self, new_status):
        """
        Запрашиваю новый статус
        """
        if new_status != self._current_status:
            old_status = self._current_status
            self._current_status = new_status
            
            # Логируем изменение статуса с дополнительной информацией
            time_since_packet = time.time() - self._last_packet_time if self._last_packet_time else None
            time_since_disarm = time.time() - self._last_disarm_time if self._last_disarm_time else None
            
            log_msg = f"StatusManager: Сменился статус с [{old_status} -> {new_status}]"
            if time_since_packet is not None:
                log_msg += f" | Время пакета: ={time_since_packet:.1f}s"
            if time_since_disarm is not None:
                log_msg += f" | Время дизарма: ={time_since_disarm:.1f}s"
            log.msg(log_msg)
            
            # Сбрасываем статистику frequency selection при переходе в offline
            if new_status == self.STATUS_OFFLINE:
                if self._manager and hasattr(self._manager, 'freqsel'):
                    self._manager.freqsel.reset_all_channels_stats()
            
            if self._status_change_callback:
                try:
                    self._status_change_callback(old_status, new_status)
                except Exception as e:
                    log.msg(f"Error: StatusManager - error in status change callback: {e}", isError=True)
    
    def get_status(self):
        """
        Свежий статус борта
        """
        return self._current_status
    
    def get_status_info(self):
        """
       Возвращает статус инфо по борту
        """
        current_time = time.time()
        info = {
            "status": self._current_status,
            "last_packet_time": self._last_packet_time,
            "time_since_last_packet": current_time - self._last_packet_time if self._last_packet_time else None,
            "last_disarm_time": self._last_disarm_time,
            "time_since_disarm": current_time - self._last_disarm_time if self._last_disarm_time else None,
        }
        return info
    
    def set_status_change_callback(self, callback):
        """
        Устанавливаю колбек для уведомления об изменении статуса
        """
        self._status_change_callback = callback
    
    def reset(self):
        """
        Сбрасывает стейт менеджера
        """
        self._last_packet_time = None
        self._last_disarm_time = None
        self._current_status = self.STATUS_OFFLINE
        log.msg("Atention: StatusManager - state reset")
    
    def stop(self):
        """
        Останавливает проверку статуса
        """
        if self._status_check_task and self._status_check_task.running:
            self._status_check_task.stop()
        log.msg("Atention: StatusManager - stopped")