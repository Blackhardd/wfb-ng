import json
import socket
import time
from twisted.python import log
from twisted.internet import reactor, protocol, task, defer
from twisted.internet.protocol import ReconnectingClientFactory

from .sich_frequency_selection import FrequencySelection
from .sich_power_selection import PowerSelection, GSPowerController
from .sich_status_manager import StatusManager
from .sich_connection import ConnectionMetricsManager, DataHandler
from .sich_heartbeat import HeartbeatGS, HeartbeatDrone, HEARTBEAT_GS_PORT, HEARTBEAT_DRONE_PORT
from .conf import settings


def _set_tcp_options(transport):
    """Keepalive + TCP_NODELAY: быстрая отправка init без буферизации Nagle."""
    try:
        h = getattr(transport, "getHandle", None) or getattr(transport, "socket", None)
        if h is None:
            return
        s = h() if callable(h) else h
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


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
        _set_tcp_options(self.transport)
        self.manager.on_connected()
        self._process_queue()

    def connectionLost(self, reason): # вызывается при разрыве соединения
        self.manager.on_disconnected(reason)

    def dataReceived(self, data): # обработка полученных данных - сАмое главнвые действия
        self._buffer += data
        try:
            msg = json.loads(self._buffer.decode())
            self._buffer = b""
            # На дроне: команды от GS могут приходить по этому же соединению. heartbeat — по UDP.
            if self.manager.get_type() == "drone" and msg.get("command") in ("init", "freq_sel_hop", "tx_power", "update_config", "set_status"):
                response = self.manager.process_command_message(msg)
                self.transport.write(json.dumps(response).encode())
                return
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
    initialDelay = 0      # ретрай в следующий тик реактора (моментально)
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
    
    def _reason_str(self, reason):
        return reason.getErrorMessage() if hasattr(reason, 'getErrorMessage') else str(reason)

    def clientConnectionLost(self, connector, reason): # обработка потери соединения
        log.msg("Manager connection lost: %s" % self._reason_str(reason))
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason): # обработка неудачного соединения
        log.msg("Manager connection failed: %s" % self._reason_str(reason))
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
        self._pending_init_deferred = None
        self._pending_response_deferred = None

    def send_response(self, obj):
        log.msg("Sending response:", obj)
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
            _set_tcp_options(self.transport)
            if hasattr(self.manager, "on_incoming_server_connection"):
                self.manager.on_incoming_server_connection(self)
            self.manager.on_connected()

    def connectionLost(self, reason): # вызывается при разрыве соединения
        if self._pending_init_deferred and not self._pending_init_deferred.called:
            self._pending_init_deferred.errback(reason)
        self._pending_init_deferred = None
        if self._pending_response_deferred and not self._pending_response_deferred.called:
            self._pending_response_deferred.errback(reason)
        self._pending_response_deferred = None
        if hasattr(self.manager, "_incoming_server_protocol") and self.manager._incoming_server_protocol is self:
            self.manager._incoming_server_protocol = None
        self.manager.on_disconnected(reason)

    def send_init_and_wait(self, init_command):
        """Отправить init и ждать ответ (используется GS при входящем соединении от дрона)."""
        d = defer.Deferred()
        self._pending_init_deferred = d
        try:
            peer = self.transport.getPeer()
            frame = json.dumps(init_command).encode("utf-8") if peer.host != "127.0.0.1" else json.dumps(init_command, ensure_ascii=False).encode("utf-8")
            self.transport.write(frame)
        except Exception as e:
            d.errback(e)
            self._pending_init_deferred = None
        return d

    def send_command_and_wait(self, command):
        """Отправить команду пиру и ждать ответ (GS шлёт команду дрону по входящему соединению)."""
        d = defer.Deferred()
        self._pending_response_deferred = d
        try:
            peer = self.transport.getPeer()
            frame = json.dumps(command).encode("utf-8") if peer.host != "127.0.0.1" else json.dumps(command, ensure_ascii=False).encode("utf-8")
            self.transport.write(frame)
        except Exception as e:
            self._pending_response_deferred = None
            d.errback(e)
        return d

    def dataReceived(self, data): # обработка полученных данных - сАмое главнвые действия
        try:
            message = json.loads(data.decode("utf-8"))
            if self._pending_init_deferred and not self._pending_init_deferred.called and "status" in message:
                d, self._pending_init_deferred = self._pending_init_deferred, None
                d.callback(message)
                return
            if self._pending_response_deferred and not self._pending_response_deferred.called:
                d, self._pending_response_deferred = self._pending_response_deferred, None
                d.callback(message)
                return
            response = self.manager.process_command_message(message)
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

        # Таймстамп первого подключения (для вычисления uptime)
        self._first_connect_ts = None

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
            ident = f'{self.get_type()}::{"freq_sel" if self.frequency_selection.is_enabled() else "startup"}::on_stats_received'
            self.data_handler.add_callback(self.frequency_selection.channels.on_stats_received, ident)

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

    def process_init_command(self, message):
        """Обработка команды init (вызывается из ManagerJSONServer и ManagerJSONClient на дроне)."""
        try:
            if message.get("freq_sel", {}).get("enabled") and self.frequency_selection.is_enabled():
                pass
            return {"status": "success"}
        except Exception:
            return {"status": "error"}

    def process_command_message(self, message):
        """
        Обработка входящей команды от пира. Возвращает dict-ответ для отправки.
        Используется на сервере (оба стороны) и на клиенте дрона при приёме команд от GS по входящему соединению.
        """
        response = {"status": "success"}
        command = message.get("command")
        if command == "init":
            result = self.process_init_command(message)
            if result.get("status") == "success" and getattr(self, "status_manager", None):
                sync_status = message.get("status")
                if sync_status in ("connected", "armed", "disarmed"):
                    self.status_manager._transition_to(sync_status)
            return result
        if command == "freq_sel_hop":
            # Дрон считает action_time, планирует свой хоп на этот момент, отдаёт время ГС для синхронного хопа
            hop_response = self.frequency_selection.handle_hop_command()
            return {**response, **hop_response}
        if command == "set_status":
            # Синхронизация статуса с ГС только для connected/armed/disarmed. Работает только при
            # нормальной связи; при потере связи команда не дойдёт — это нормально. lost/recovery
            # на дроне всегда по локальному таймауту пакетов (без команд от ГС).
            status = message.get("status")
            if status and getattr(self, "status_manager", None) and status in ("connected", "armed", "disarmed"):
                self.status_manager._transition_to(status)
                log.msg("[Drone] Статус синхронизирован с ГС: %s" % status)
            return response
        if command == "update_config":
            self.update_config(message.get("settings"))
        elif command == "tx_power":
            action = message.get("action")
            if hasattr(self, "power_selection") and self.power_selection and action:
                self.power_selection.on_tx_power_command(action)
                response["level"] = self.power_selection.level_index
            else:
                response["status"] = "error"
                response["error"] = "tx_power not available or invalid action"
        return response

    def _mark_first_connect(self):
        """Записать таймстамп первого подключения (для uptime)."""
        if self._first_connect_ts is None:
            self._first_connect_ts = time.time()
            log.msg("[Manager] First connect ts=%.2f (uptime starts)" % self._first_connect_ts)

    def get_connection_uptime_sec(self) -> float | None:
        """Сколько секунд устройство в подключённом состоянии. None если ещё не подключались."""
        if self._first_connect_ts is None:
            return None
        return time.time() - self._first_connect_ts

    def on_connected(self):
        """При установлении TCP с GS — выходим из waiting в connected (синхрон с GS после init)."""
        self._mark_first_connect()
        log.msg("Management connection established")
        if getattr(self, "status_manager", None) and self.status_manager.get_status() == "waiting":
            self.status_manager._transition_to("connected")

    def on_disconnected(self, reason):
        err = reason.getErrorMessage() if hasattr(reason, 'getErrorMessage') else str(reason)
        log.msg("Management connection closed: %s" % err)
        self._is_connected = False

    def on_status_changed(self, old_status, new_status):
        """Вызывается StatusManager при смене статуса. DroneManager переопределяет для PowerSelection."""
        pass

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

        # Хопы вызываются напрямую из Status Manager (в состояниях)

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

        # Heartbeat по UDP
        self._heartbeat_udp = reactor.listenUDP(HEARTBEAT_GS_PORT, HeartbeatGS(self))

        self._incoming_server_protocol = None
        self._last_init_attempt = 0.0
        self._init_timeout_sec = 8
        self._init_retry_interval = 3.0
        self._init_retry_task = task.LoopingCall(self._periodic_init_retry)
        self._init_retry_task.start(self._init_retry_interval)

    def on_incoming_server_connection(self, server_protocol):
        """Вызывается при входящем соединении от дрона. Используем для init, если клиент ещё не готов."""
        self._incoming_server_protocol = server_protocol
        self._try_init_over_incoming()

    def _try_init_over_incoming(self):
        """Попытаться отправить init по входящему соединению (fallback при перезагрузке дрона)."""
        if self._is_connected:
            return
        if not self._incoming_server_protocol or not self._incoming_server_protocol.transport:
            return
        init_cmd = {
            "command": "init",
            "freq_sel": {"enabled": self.frequency_selection.is_enabled()},
            "status": self.status_manager.get_status(),
        }
        log.msg("[GS] Sending init over incoming connection (client not ready)")
        self._last_init_attempt = time.time()
        d = self._incoming_server_protocol.send_init_and_wait(init_cmd)
        timeout_call = reactor.callLater(self._init_timeout_sec, self._init_timeout_fire, d)
        def _cancel_timeout(x):
            if timeout_call.active():
                timeout_call.cancel()
            return x
        d.addBoth(_cancel_timeout)
        d.addCallback(self.on_connection_ready)
        d.addErrback(lambda err: log.msg("Init over incoming connection failed: %s" % (err.getErrorMessage() if hasattr(err, 'getErrorMessage') else str(err))))

    def _init_timeout_fire(self, d):
        if d.called:
            return
        d.errback(Exception("Init response timeout (%ds)" % self._init_timeout_sec))

    def _periodic_init_retry(self):
        """Периодическая повторная попытка init, пока в waiting и TCP без handshake (асимметрия/потери)."""
        if self._is_connected:
            return
        if self.status_manager.get_status() != "waiting":
            return
        if time.time() - self._last_init_attempt < self._init_retry_interval - 0.5:
            return
        client_ready = getattr(self.client_f, "protocol_instance", None) and getattr(
            self.client_f.protocol_instance, "transport", None
        )
        if client_ready:
            self._last_init_attempt = time.time()
            init_cmd = {
                "command": "init",
                "freq_sel": {"enabled": self.frequency_selection.is_enabled()},
                "status": self.status_manager.get_status(),
            }
            log.msg("[GS] Init retry over client connection")
            d = self.client_f.send_command(init_cmd)
            if d is None:
                return
            timeout_call = reactor.callLater(self._init_timeout_sec, self._init_timeout_fire, d)
            def _cancel_timeout_retry(x):
                if timeout_call.active():
                    timeout_call.cancel()
                return x
            d.addBoth(_cancel_timeout_retry)
            d.addCallback(self.on_connection_ready)
            d.addErrback(lambda err: log.msg("Init retry failed: %s" % (err.getErrorMessage() if hasattr(err, 'getErrorMessage') else str(err))))
        elif self._incoming_server_protocol and self._incoming_server_protocol.transport:
            self._try_init_over_incoming()

    def on_connected(self):
        super().on_connected()
        # on_connected() вызывается и когда наш клиент подключился к дрону, и когда дрон
        # подключился к нам (сервер). Init шлём по исходящей связи; если первым пришло
        # входящее — пробуем init по входящему соединению (fallback при перезагрузке дрона).
        if self._is_connected:
            return
        client_ready = getattr(self.client_f, "protocol_instance", None) and getattr(
            self.client_f.protocol_instance, "transport", None
        )
        if not client_ready:
            self._try_init_over_incoming()
            return
        self._last_init_attempt = time.time()
        d = self.client_f.send_command({
            "command": "init",
            "freq_sel": {"enabled": self.frequency_selection.is_enabled()},
            "status": self.status_manager.get_status(),
        })
        if d is None:
            return
        timeout_call = reactor.callLater(self._init_timeout_sec, self._init_timeout_fire, d)
        def _cancel_timeout_client(x):
            if timeout_call.active():
                timeout_call.cancel()
            return x
        d.addBoth(_cancel_timeout_client)
        d.addCallback(self.on_connection_ready)
        d.addErrback(lambda err: log.msg("Error initializing connection: %s" % (err.getErrorMessage() if hasattr(err, 'getErrorMessage') else str(err))))

    def on_connection_ready(self, message):
        if self._is_connected:
            return
        if not message.get("status") == "success":
            log.msg("Failed to prepare connection:", message)
            return

        self._is_connected = True
        sm = self.status_manager
        # Переход в connected при установлении management link: из waiting (старт) или disarmed (дрон перезагрузился).
        # Хоп на первый freq_sel только после ARM (ArmedState.on_enter).
        if sm.get_status() in ("waiting", "disarmed"):
            log.msg("[GS] Connection ready: transition to connected (stay on reserve until ARM)")
            sm._transition_to("connected")

    def send_command_to_drone(self, command):
        """
        Отправить команду дрону. В момент ARM часто есть только входящее соединение (дрон подключился к нам).
        Сначала пробуем исходящий клиент; если нет — шлём по входящему соединению, дрон ответит, плавный хоп по action_time.
        Returns:
            Deferred с ответом или None если нет соединения.
        """
        client_ready = getattr(self.client_f, "protocol_instance", None) and getattr(
            self.client_f.protocol_instance, "transport", None
        )
        if client_ready:
            return self.client_f.send_command(command)
        if self._incoming_server_protocol and self._incoming_server_protocol.transport:
            return self._incoming_server_protocol.send_command_and_wait(command)
        return None

    def update_config(self, data):
        super().update_config(data)

    def _cleanup(self): # очищаю при остановке сервера и дрона
        if getattr(self, "_init_retry_task", None) and self._init_retry_task.running:
            self._init_retry_task.stop()
        if hasattr(self, 'power_controller') and self.power_controller:
            self.power_controller.stop()
        if hasattr(self, '_heartbeat_udp') and self._heartbeat_udp:
            self._heartbeat_udp.stopListening()
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

        # Запуск единого DataHandler (RSSI/PER/SNR пойдут в metrics_manager и на дрон)
        reactor.callWhenRunning(self.data_handler.start)

        # Create management client to send commands to GS
        self.client_f = ManagerJSONClientFactory(self)
        reactor.connectTCP("10.5.0.1", 14889, self.client_f)

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14888, self.server_f)

        # Heartbeat по UDP
        self._heartbeat_udp = reactor.listenUDP(HEARTBEAT_DRONE_PORT, HeartbeatDrone(self))

    def on_status_changed(self, old_status, new_status):
        """При смене статуса: connected/disarmed -> минимум мощности; armed -> управление по RSSI с GS."""
        if not self.status_manager:
            return
        if new_status == self.status_manager.STATUS_ARMED:
            self.power_selection.on_arm()
        elif new_status == self.status_manager.STATUS_DISARMED:
            self.power_selection.on_disarm()
            self.power_selection.set_minimum_power()
        elif new_status == self.status_manager.STATUS_CONNECTED:
            self.power_selection.set_minimum_power()
        # Отправка статуса дрону только на ГС (у дрона нет send_command_to_drone).

    def _cleanup(self):
        if hasattr(self, 'power_selection') and self.power_selection:
            self.power_selection.stop()
        if hasattr(self, '_heartbeat_udp') and self._heartbeat_udp:
            self._heartbeat_udp.stopListening()
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
