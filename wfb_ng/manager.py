import time
import json
from twisted.internet import reactor, protocol, task, defer

from .frequency_selection import FrequencySelection
from .power_selection import PowerSelection

class LinkStatusReceiver(protocol.Protocol):
    def __init__(self, manager):
        self.manager = manager
        
        self._buffer = b""
        self._connection_data = {}
        self._connection_timeout = 5.0

    def _iter_ant_stats(self, service=None):
        if service:
            sd = self._connection_data.get(service, {})
            stats = sd.get("stats", {})
            if isinstance(stats, dict):
                yield from stats.values()
            else:
                yield from stats
        else:
            for sd in self._connection_data.values():
                stats = sd.get("stats", {})
                if isinstance(stats, dict):
                    yield from stats.values()
                else:
                    yield from stats
        
    def dataReceived(self, data):
        self._buffer += data
        lines = self._buffer.split(b"\n")
        self._buffer = lines[-1]

        for line in lines[:-1]:
            if line.strip():
                try:
                    attrs = json.loads(line.decode("utf-8"))
                    self.process_stats(attrs)
                except Exception as e:
                    print(f"Error processing JSON stats: {e}")
    
    def process_stats(self, attrs):
        current_time = time.time()

        if attrs.get("type") == "rx":
            service_id = attrs["id"]
            packets = attrs.get("packets", {})
            rx_ant_stats = attrs.get("rx_ant_stats", [])

            stats = {}
            if rx_ant_stats:
                for ant in rx_ant_stats:
                    ant_key = ant.get("ant")
                    if ant_key is None:
                        continue
                    
                    stats[ant_key] = {
                        "rssi": {
                            "avg": ant.get("rssi_avg"),
                            "p5": ant.get("rssi_p5")
                        },
                        "snr": {
                            "avg": ant.get("snr_avg"),
                            "p5": ant.get("snr_p5")
                        }
                    }
            
            self._connection_data[service_id] = {
                "timestamp": current_time,
                "has_packets": packets.get("all", [0, 0])[0] > 0,
                "has_data": packets.get("data", [0, 0])[0] > 0,
                "has_signal": len(rx_ant_stats) > 0,
                "stats": stats
            }
    
    def is_connected(self, service_id=None):
        current_time = time.time()
        
        if service_id:
            conn = self._connection_data.get(service_id)
            if not conn:
                return False
            
            if (current_time - conn["timestamp"]) > self._connection_timeout:
                return False
                
            return conn["has_packets"] and conn["has_signal"]
        else:
            return any(self.is_connected(sid) for sid in self._connection_data.keys())
    
    def is_ready(self, service_id=None):
        if service_id:
            conn = self._connection_data.get(service_id)
            return conn and self.is_connected(service_id) and conn["has_data"]
        else:
            return any(self.is_ready(sid) for sid in self._connection_data.keys())
    
    def is_jammed(self):
        for service_id in self._connection_data:
            service = self._connection_data[service_id]
            if not service["has_signal"]:
                continue
            
            for ant_stats in self._iter_ant_stats(service_id):
                print(ant_stats)
                if ant_stats.get("snr", {}).get("p5", 0) < 17:
                    return True
        return False
    
    def get_status(self):
        return {
            "connected": self.is_connected(),
            "ready": self.is_ready(),
            "jammed": self.is_jammed(),
            "services": self._connection_data
        }
    
    def get_rssi(self):
        rssi_values = []
        for service_id in self._connection_data:
            service = self._connection_data[service_id]
            if not service["has_signal"]:
                continue
            rssi_values.append(service["stats"].get("rssi", {}))
        return rssi_values

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
        print("Sending command:", command)
        d = defer.Deferred()
        self._queue.append((command, d))
        if self.transport and not self._waiting:
            self._process_queue()
        return d

class ManagerJSONClientFactory(protocol.ClientFactory):
    protocol = ManagerJSONClient

    def __init__(self, manager):
        self.manager = manager

        self.protocol_instance = None

    def buildProtocol(self, addr):
        p = self.protocol(self.manager)
        self.protocol_instance = p
        return p
    
    def send_command(self, command):
        if self.protocol_instance:
            return self.protocol_instance.send_command(command)

class ManagerJSONServer(protocol.Protocol):
    def __init__(self, manager):
        self.manager = manager

    def send_response(self, obj):
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
            length = int.from_bytes(data[:4], "big")
            payload = data[4:4+length]
            message = json.loads(payload.decode("utf-8"))

            command = message.get("command")

            response = {"status": "success"}
            
            if command == "init":
                if message["freq_sel"]["enabled"] and self.manager.freq_sel.is_enabled():
                    response["freq_sel"] = {"start_time": self.manager.freq_sel.schedule_start()}
            elif command == "freq_sel_hop":
                if self.manager.freq_sel.is_enabled():
                    response["time"] = self.manager.freq_sel.schedule_hop()
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
    _is_link_ready = False
    _is_connection_ready = False

    def __init__(self, config, wlans):
        self.config = config
        self.wlans = wlans

        # Connecting to JSON API for receiving connection status
        self.link_status = LinkStatusReceiver(self)
        reactor.connectTCP("localhost", self.config.api_port, protocol.ClientFactory.forProtocol(lambda: self.link_status))

        # Create manager components
        self.freq_sel = FrequencySelection(self)
        self.power_sel = PowerSelection(self)

        self._lc = task.LoopingCall(self._check_link)
        self._lc.start(2.0, now=True)

    def _check_link(self):
        if self.link_status.is_connected():
            if self._is_link_ready == False:
                self._is_link_ready = True
                self.on_linked()
        else:
            if self.freq_sel.is_enabled():
                if self.freq_sel.is_recovery_channel():
                    self.freq_sel.schedule_hop()
                else:
                    self.freq_sel.schedule_recovery_hop()
            if self._is_link_ready == True:
                self._is_link_ready = False
                self.on_unlinked()

    def _cleanup(self):
        if hasattr(self, "_lc"):
            self._lc.stop()

        if hasattr(self, "freq_sel"):
            self.freq_sel._cleanup()

    def get_type(self):
        return self._type

    def is_linked(self):
        return self.link_status.is_connected()
        
    def is_link_ready(self):
        return self.link_status.is_ready()
        
    def get_link_status(self):
        return self.link_status.get_status()
    
    def on_linked(self):
        print("Link is active")

    def on_unlinked(self):
        print("Link is not active")
    
    def on_connected(self):
        print("Management connection established")

    def on_disconnected(self, reason):
        print("Management connection closed:", reason)

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

        # Create and start management server
        self.server_f = ManagerJSONServerFactory(self)
        reactor.listenTCP(14889, self.server_f)
    
    def on_linked(self):
        super().on_linked()
        reactor.connectTCP("10.5.0.2", 14888, self.client_f)

    def on_connected(self):
        super().on_connected()
        if not self._is_connection_ready:
            d = self.client_f.send_command({
                "command": "init",
                "freq_sel": {"enabled": self.freq_sel.is_enabled()}
            })

            d.addCallback(self.on_connection_ready)
            d.addErrback(lambda err: print("Error initializing connection:", err))

    def on_connection_ready(self, message): 
        if not message["status"] == "success":
            print("Failed to prepare connection:", message)
            return

        self._is_connection_ready = True

        # Start frequency selection if enabled
        if self.freq_sel.is_enabled() and message.get("freq_sel") and "start_time" in message["freq_sel"]:
            self.freq_sel.schedule_start(message["freq_sel"]["start_time"])

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