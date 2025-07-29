import time

from twisted.internet import reactor, threads, task

from . import call_and_check_rc
from .conf import settings

class FrequencySelection:
    def __init__(self, wlans):
        self.wlans = tuple(wlans)
        self.initial_channel = settings.common.wifi_channel
        self.type = settings.common.freq_sel_type
        self.channels = [self.initial_channel, *settings.common.freq_sel_channels]
        self.channel_index = 0
        self.interval = settings.common.freq_sel_interval

    def _cleanup(self):
        self.lc.stop()

    def schedule(self, start_time):
        self.start_time = start_time
        delay = max(0, self.start_time - time.time())
        task.deferLater(reactor, delay, self._start)

    def start(self):
        self.lc = task.LoopingCall(self.hop)
        self.lc.start(self.interval, now=True)

    def hop(self):
        def _hop():
            channel = self.get_next_channel()
            for wlan in enumerate(self.wlans):
                call_and_check_rc('iw', 'dev', wlan, 'set', 'freq' if channel > 2000 else 'channel', str(channel))
        
        if self.type == 'smart':
            return
        
        return threads.deferToThread(_hop)

    def get_next_channel(self):
        if self.channel_index < len(self.channels) - 1:
            self.channel_index += 1
        else:
            self.channel_index = 0
        return self.channels[self.channel_index]
    
    def get_start_time(self):
        self.start_time = time.time() + (self.interval if self.interval >= 1 else 1)
        return self.start_time
