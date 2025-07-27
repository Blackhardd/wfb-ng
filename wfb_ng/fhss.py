from twisted.internet import threads, task

from . import call_and_check_rc

class FHSS:
    def __init__(self, wlan: str, initial_channel, channels, interval = 1.0):
        self.wlan = wlan
        self.initial_channel = initial_channel
        self.channels = [initial_channel, *channels]
        self.channel_index = 0
        # self.start_time = time.perf_counter()
        self.interval = interval

        self.lc = task.LoopingCall(self.hop)
        self.lc.start(self.interval, now=True)

    def _cleanup(self):
        self.lc.stop()

    def hop(self):
        def _hop():
            channel = self.get_next_channel()
            if channel > 2000:
                call_and_check_rc('iw', 'dev', self.wlan, 'set', 'freq', str(channel))
            else:
                call_and_check_rc('iw', 'dev', self.wlan, 'set', 'channel', str(channel))
        return threads.deferToThread(_hop)

    def get_next_channel(self):
        if self.channel_index < len(self.channels) - 1:
            self.channel_index += 1
        else:
            self.channel_index = 0
        return self.channels[self.channel_index]
