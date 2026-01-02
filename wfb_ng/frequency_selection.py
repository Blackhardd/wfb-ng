import time

from twisted.internet import reactor, threads, task

from . import call_and_check_rc
from .conf import settings

class FrequencySelection:
    def __init__(self, manager):
        self.manager = manager

        self.enabled = settings.common.freq_sel_enabled
        self.type = settings.common.freq_sel_type
        self.initial_channel = settings.common.wifi_channel
        self.recovery_channel = settings.common.freq_sel_recovery if not settings.common.freq_sel_recovery is None else self.initial_channel
        self.channels = [self.initial_channel, *settings.common.freq_sel_channels]
        self.channel_index = 0
        self.interval = settings.common.freq_sel_interval

        print(f"Frequency selection initialized with channels: {self.channels}, type: {self.type}, interval: {self.interval} seconds")

        self._is_scheduled_start = False
        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

        self._is_recovery_channel = False

    def _cleanup(self):
        self.stop()

    def _hop(self, channel=None):
        chan = channel
        if not chan:
            if self._is_recovery_channel:
                self.channel_index = 0
                chan = self.channels[self.channel_index]
            else:
                chan = self.get_next_channel()

        if self._is_scheduled_recovery_hop and self.manager.link_status.is_connected():
            self._is_scheduled_recovery_hop = False
            print("Recovery channel hop cancelled because the link is alive")
            return
        
        for wlan in self.manager.wlans:
            call_and_check_rc("iw", "dev", wlan, "set", "freq" if chan > 2000 else "channel", str(chan))

        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

        self._is_recovery_channel = chan == self.recovery_channel

    def is_enabled(self):
        if self.enabled and len(self.channels) > 1:
            return True
        print("Frequency selection is disabled or not configured properly.")
        return False
    
    def is_recovery_channel(self):
        return self._is_recovery_channel

    def schedule_start(self, action_time=None):
        if not self.is_enabled():
            return
        
        if self._is_scheduled_start:
            return

        if action_time is None:
            action_time = self.get_action_time()
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self.start)

        print(f"Frequency selection check scheduled to start in {delay:.2f} seconds")

        return action_time

    def start(self):
        if not self.is_enabled():
            return
        
        self._lc = task.LoopingCall(self.hop)
        self._lc.start(self.interval, now=True)

        self._is_scheduled_start = False

    def stop(self):
        if hasattr(self, "_lc") and self._lc:
            self._lc.stop()
            self._lc = None

    def hop(self):
        if self._is_scheduled_hop:
            return
        
        if not self.manager.link_status.is_connected():
            if not self._is_scheduled_recovery_hop:
                self.schedule_recovery_hop()
            return

        if self.type == "interval":
            threads.deferToThread(self._hop)
        elif self.type == "smart":
            if self.manager.get_type() == "gs" and self.manager.link_status.is_jammed():
                d = self.manager.client_f.send_command({"command": "freq_sel_hop"})
                d.addCallback(lambda res: self.schedule_hop(res.get("time")))
                d.addErrback(lambda err: self.hop)

    def schedule_hop(self, action_time = None):
        if not self.is_enabled():
            return
        
        if self._is_scheduled_hop:
            return
        
        self._is_scheduled_hop = True
        
        if action_time is None:
            action_time = self.get_action_time(1.0)
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self._hop)

        print(f"Frequency hop scheduled to execute in {delay:.2f} seconds")

        return action_time

    def schedule_recovery_hop(self):
        if not self.is_enabled():
            return

        if self._is_scheduled_recovery_hop:
            return

        self._is_scheduled_recovery_hop = True

        action_time = self.get_action_time(1.0)
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self._hop, self.recovery_channel)

        print(f"Recovery channel hop is scheduled to execute in {delay:.2f} seconds")
    
    def get_action_time(self, interval = None):
        if interval is None:
            interval = self.interval if self.interval >= 1 else 1
        return time.time() + interval
    
    def get_next_channel(self):
        if self.channel_index < len(self.channels) - 1:
            self.channel_index += 1
        else:
            self.channel_index = 0
        return self.channels[self.channel_index]
