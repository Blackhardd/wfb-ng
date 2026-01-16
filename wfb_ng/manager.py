import json
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

class GSManager(Manager):
    _type = "gs"

    def __init__(self, config, wlans):
        super().__init__(config, wlans)

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