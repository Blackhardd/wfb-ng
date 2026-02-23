import json
import socket
import time
from twisted.python import log
from twisted.internet import reactor, protocol, task, defer
from twisted.internet.protocol import ReconnectingClientFactory

from .sich_frequency_selection import FrequencySelection
from .sich_power_selection import PowerSelection, global_power_selection_mode
from .sich_status_manager import StatusManager, StatusManagerDisabled
from .sich_connection import ConnectionMetricsManager, DataHandler
from .sich_heartbeat import HeartbeatGS, HeartbeatDrone, HEARTBEAT_GS_PORT, HEARTBEAT_DRONE_PORT
from .conf import settings


# Включает TCP_NODELAY и SO_KEEPALIVE для быстрого init после connectionMade, быстрой доставки команд и своевременного обнаружения потери связи.
# Использую его в connectionMade в ооп классе ManagerJSONClient/ManagerJSONServer - для init, freq_sel_hop и других команд.
# Без єтого init/команды задерживались а потеря связи обнаруживалась позже чем надо
def _set_tcp_options(transport):
    try:
        # Пытаемся получить сокет соединения
        get_socket = getattr(transport, "getHandle", None) or getattr(transport, "socket", None)
        if get_socket is None:
            return
        # Получаем сокет и устанавливаем опции
        sock = get_socket() if callable(get_socket) else get_socket
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


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

    def connectionLost(self, reason):  # вызывается при разрыве соединения
        # on_disconnected вызывается только из фабрики (clientConnectionLost), чтобы не дублировать лог
        pass

    def dataReceived(self, data): # обработка полученных данных (ответы от сервера)
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
    initialDelay = 0      # ретрай в следующий тик реактора (моментально)
    maxDelay = 1.0

    def __init__(self, manager):
        ReconnectingClientFactory.__init__(self)
        self.manager = manager
        self.protocol_instance = None

    def buildProtocol(self, addr): # создание экземпляра протокола
        self.resetDelay() # сброс задержки в следующий тик реактора Twisted
        protocol_instance = self.protocol(self.manager)
        self.protocol_instance = protocol_instance
        return protocol_instance
    
    def clientConnectionLost(self, connector, reason): 
        log.msg("TCP connection lost: %s" % (reason.getErrorMessage() if hasattr(reason, 'getErrorMessage') else str(reason)))
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        log.msg("TCP connection failed: %s" % (reason.getErrorMessage() if hasattr(reason, 'getErrorMessage') else str(reason)))
        self.manager.on_disconnected(reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
    
    def send_command(self, command): 
        """
        Отправить команду дрону с GS по TCP
        """
        if self.protocol_instance:
            log.msg("Sending TCP json command to drone: %s" % command)
            return self.protocol_instance.send_command(command)
        log.msg("TCP connection not established")
        return None

# TCP сервер дрона, принимает команды от GS по JSON
class ManagerJSONServer(protocol.Protocol): 
    def __init__(self, manager):
        self.manager = manager

    def send_response(self, obj):
        log.msg("Sending response:", obj)
        peer = self.transport.getPeer()
        if peer.host == "127.0.0.1":
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            frame = len(body).to_bytes(4, "big") + body
        else:
            frame = json.dumps(obj).encode("utf-8")
        self.transport.write(frame)

    # вызывается при установке соединения
    def connectionMade(self): 
        peer = self.transport.getPeer()
        if peer.host != "127.0.0.1":
            _set_tcp_options(self.transport)
        self.manager.on_connected()

    # вызывается при разрыве соединения
    def connectionLost(self, reason): 
        self.manager.on_disconnected(reason)

    # обработка полученных данных (команды от GS)
    def dataReceived(self, data): 
        try:
            message = json.loads(data.decode("utf-8"))
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
        self.metrics_manager = ConnectionMetricsManager()

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

        Источник один - stats от wfb_rx по любому потоку (video/mavlink/tunnel).
        Как только по любому из потоков приходят данные - считаем «пакет получен» для статуса связи.
        """
        self.metrics_manager.connect_to(self.data_handler)

        if hasattr(self, 'frequency_selection') and hasattr(self.frequency_selection, 'channels'):
            ident = f'{self.get_type()}::{"freq_sel" if self.frequency_selection.is_enabled() else "startup"}::on_stats_received'
            self.data_handler.add_callback(self.frequency_selection.channels.on_stats_received, ident)

        # Любая доставка stats по радиоканалу (любой поток) -> событие «пакет получен» для StatusManager.
        # Не зависим от mavlink: работает при любом потоке (video/mavlink/tunnel).
        self.data_handler.add_callback(self._on_radio_stats_for_status)

    def _on_radio_stats_for_status(self, rx_id, stats_dict):
        """
        Уведомляем StatusManager только когда реально принят хотя бы один пакет (не просто приход stats с PER 100%)
        """
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

    def process_command_message(self, message):
        response = {"status": "success"}
        command = message.get("command")
        if command == "init":
            return {"status": "success"}
        if command == "freq_sel_hop":
            # Дрон считает action_time, планирует свой хоп на этот момент, отдаёт время ГС для синхронного хопа
            hop_response = self.frequency_selection.handle_hop_command()
            return {**response, **hop_response}
        return response

    def on_connected(self):
        """При установлении TCP переходим из waiting в connected."""
        log.msg("соеденения по TCP")
        if getattr(self, "status_manager", None) and self.status_manager.get_status() == "waiting":
            self.status_manager._transition_to("connected")

    def on_disconnected(self, reason):
        err = reason.getErrorMessage() if hasattr(reason, 'getErrorMessage') else str(reason)
        log.msg("def on_disconnected: Разрыв соеденения по TCP: %s" % err)
        self._is_connected = False

    def on_status_changed(self, old_status, new_status):
        """Вызывается StatusManager при смене статуса. DroneManager переопределяет для PowerSelection."""
        pass

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

        # StatusManager - управляет статусами соединения (status_manager_mode=false = заглушка)
        if getattr(settings.common, "status_manager_mode", True):
            self.status_manager = StatusManager(config, wlans, manager=self)
        else:
            self.status_manager = StatusManagerDisabled(config, wlans, manager=self)
            log.msg("[SM] StatusManager отключён (status_manager_mode=false)")

        # DataHandler - получение статистики по радиоканалу\а у wfb_rx
        reactor.callWhenRunning(self.data_handler.start)

        # TCP клиент - подключается к дрону, init и команды. Блокируем TCP если оба: status_manager и freq_sel выключены.
        self.client_f = ManagerJSONClientFactory(self)
        self._last_init_attempt = 0.0
        self._init_timeout_sec = 8
        self._init_retry_interval = 3.0
        _sm = getattr(settings.common, "status_manager_mode", True)
        _fs = getattr(settings.common, "freq_sel_enabled", False)
        _need_tcp = _sm or _fs
        log.msg("[GS] status_manager_mode=%s freq_sel_enabled=%s => TCP %s" % (_sm, _fs, "вкл" if _need_tcp else "выкл"))
        if _need_tcp:
            reactor.connectTCP("10.5.0.2", 14888, self.client_f)
            self._init_retry_task = task.LoopingCall(self._periodic_init_retry)
            self._init_retry_task.start(self._init_retry_interval)
        else:
            log.msg("[GS] TCP manager не запущен (status_manager и freq_sel отключены)")
            self._init_retry_task = None

        # Heartbeat по UDP (можно отключить heartbeat_mode в cfg для тестов)
        self._heartbeat_udp = None
        if getattr(settings.common, "heartbeat_mode", True):
            self._heartbeat_udp = reactor.listenUDP(HEARTBEAT_GS_PORT, HeartbeatGS(self))

    def _init_timeout_fire(self, d):
        if d.called:
            return
        d.errback(Exception("Init response timeout (%ds)" % self._init_timeout_sec))

    def _is_client_ready(self):
        """TCP клиент готов к отправке команд."""
        return bool(
            getattr(self.client_f, "protocol_instance", None)
            and getattr(self.client_f.protocol_instance, "transport", None)
        )

    def _send_init(self):
        """Отправить init, настроить таймаут и callbacks. Ничего не делает если клиент не готов."""
        if not self._is_client_ready():
            return
        self._last_init_attempt = time.time()
        d = self.client_f.send_command({"command": "init"})
        if d is None:
            return
        timeout_call = reactor.callLater(self._init_timeout_sec, self._init_timeout_fire, d)

        def _cancel_timeout(x):
            if timeout_call.active():
                timeout_call.cancel()
            return x

        d.addBoth(_cancel_timeout)
        d.addCallback(self.on_connection_ready)
        d.addErrback(
            lambda err: log.msg(
                "Init failed: %s" % (err.getErrorMessage() if hasattr(err, "getErrorMessage") else str(err))
            )
        )

    def _periodic_init_retry(self):
        """Периодическая повторная попытка init, пока в waiting и TCP."""
        if self._is_connected:
            return
        if self.status_manager.get_status() != "waiting":
            return
        if time.time() - self._last_init_attempt < self._init_retry_interval - 0.5:
            return
        log.msg("[GS] Init retry over client connection")
        self._send_init()

    def on_connected(self):
        super().on_connected()
        if self._is_connected:
            return
        self._send_init()

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
        Отправить команду дрону по TCP.
        Returns:
            Deferred с ответом или None если соединения нет.
        """
        if self._is_client_ready():
            return self.client_f.send_command(command)
        return None

    def _cleanup(self):
        if getattr(self, "_init_retry_task", None) and self._init_retry_task.running:
            self._init_retry_task.stop()
        if hasattr(self, '_heartbeat_udp') and self._heartbeat_udp:
            self._heartbeat_udp.stopListening()
        super()._cleanup()

# Менеджер что запускается на дроне
class DroneManager(Manager):
    _type = "drone"

    def __init__(self, config, wlans):
        log.msg("[DroneManager] ========== INITIALIZATION START ==========")
        super().__init__(config, wlans)
        if getattr(settings.common, "status_manager_mode", True):
            self.status_manager = StatusManager(config, wlans, manager=self)
        else:
            self.status_manager = StatusManagerDisabled(config, wlans, manager=self)
            log.msg("[SM] StatusManager отключён (status_manager_mode=false)")

        # PowerSelection - адаптивная мощность передатчика (только на дроне)
        if global_power_selection_mode() and settings.common.power_selection_levels:
            self.power_selection = PowerSelection(self)
            log.msg("[PS] power_selection_mode=True, disarm=min ")
        else:
            self.power_selection = None
            if not global_power_selection_mode():
                log.msg("[PS] power_selection_mode=False, адаптер сам ставит txpower")

        # Запуск единого DataHandler (RSSI/PER/SNR пойдут в metrics_manager и на дрон)
        reactor.callWhenRunning(self.data_handler.start)

        # Management server - принимает подключения от ГС. Блокируем TCP если оба: status_manager и freq_sel выключены.
        self.server_f = ManagerJSONServerFactory(self)
        _sm = getattr(settings.common, "status_manager_mode", True)
        _fs = getattr(settings.common, "freq_sel_enabled", False)
        _need_tcp = _sm or _fs
        log.msg("[Drone] status_manager_mode=%s freq_sel_enabled=%s => TCP %s" % (_sm, _fs, "вкл" if _need_tcp else "выкл"))
        if _need_tcp:
            reactor.listenTCP(14888, self.server_f)
        else:
            log.msg("[Drone] TCP manager не запущен (status_manager и freq_sel отключены)")

        # Heartbeat по UDP (можно отключить heartbeat_mode в cfg для тестов)
        self._heartbeat_udp = None
        if getattr(settings.common, "heartbeat_mode", True):
            self._heartbeat_udp = reactor.listenUDP(HEARTBEAT_DRONE_PORT, HeartbeatDrone(self))

    def on_status_changed(self, old_status, new_status):
        """Мощность: только disarm = min (16 dBm), все остальные статусы = max (26 dBm)."""
        if not self.status_manager:
            return
        if self.power_selection:
            if new_status == self.status_manager.STATUS_ARMED:
                self.power_selection.on_arm()
            elif new_status == self.status_manager.STATUS_DISARMED:
                self.power_selection.on_disarm()
            elif new_status in (self.status_manager.STATUS_CONNECTED,
                               self.status_manager.STATUS_LOST,
                               self.status_manager.STATUS_RECOVERY):
                # Только disarm = min; во всех остальных - max
                self.power_selection.on_connected()
        # STATUS_WAITING: при init уже active → max

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
