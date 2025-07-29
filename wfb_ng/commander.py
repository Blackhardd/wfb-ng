import json
from twisted.internet import reactor, protocol, defer

from .frequency_selection import FrequencySelection

class CommanderJSONProtocol(protocol.Protocol):
    def __init__(self, deferred):
        self.deferred = deferred
        self.buffer = b""

    def connectionMade(self):
        print("Message sent")

    def dataReceived(self, data):
        print(f"Received: {data}")
        self.buffer += data
        try:
            # Попробуем распарсить JSON из буфера
            response = json.loads(self.buffer.decode())
            self.deferred.callback(response)
            self.transport.loseConnection()
        except json.JSONDecodeError:
            # Ждём, пока придёт полный JSON
            pass

    def connectionLost(self, reason):
        print(f"Connection lost: {reason}")
        if not self.deferred.called:
            self.deferred.errback(reason)

    def send_command(self, obj):
        message = json.dumps(obj).encode()
        self.transport.write(message)
        print(f"Command sent: {message}")

class CommanderServer(protocol.Protocol):
    def connectionMade(self):
        print("Controller server connected")

    def dataReceived(self, data):
        print(f"Controller server received: {data}")

class CommanderClientFactory(protocol.Factory):
    def __init__(self):
        self.deferred = defer.Deferred()

    def buildProtocol(self, addr):
        return CommanderJSONProtocol(self.deferred)
    
    def clientConnectionFailed(self, connector, reason):
        self.deferred.errback(reason)

    def send_command(self, command):
        if self.protocol_instance:
            self.protocol_instance.send_command(command)
        else:
            print("No protocol instance available to send command")

class CommanderServerFactory(protocol.Factory):
    protocol = CommanderServer

class GSCommander:
    def __init__(self, wlans):
        self.wlans = wlans

        self.client_f = CommanderClientFactory()
        reactor.connectTCP('10.5.0.2', 14888, self.client)

        self.init_frequency_selection()

    def init_frequency_selection(self):
        self.freq_sel = FrequencySelection(self.wlans)

        start_time = self.freq_sel.get_start_time()
        self.client_f.send_command({'command': 'start_frequency_selection', 'start_time': start_time})

class DroneCommander:
    def __init__(self, wlans):
        self.server_factory = CommanderServerFactory()
        reactor.listenTCP(14888, self.server_factory)

        self.freq_sel = FrequencySelection(wlans)

    def _cleanup(self):
        self.freq_sel._cleanup()