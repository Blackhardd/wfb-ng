from twisted.internet import reactor, threads, task

from . import call_and_check_rc
from .conf import settings

class PowerSelection:
    def __init__(self, manager):
        self.manager = manager

        self.enabled = settings.common.power_sel_enabled
        self.levels = settings.common.power_sel_levels
        self.level_index = 0

        print(f"PowerSelection initialized with enabled={self.enabled}, levels={self.levels}")

        if self.enabled:
            # Start the signal checking loop
            self._lc = task.LoopingCall(self.check_signal)
            self._lc.start(1.0, now=True)

    def _cleanup(self):
        self.stop()

    def stop(self):
        if hasattr(self, "_lc") and self._lc:
            self._lc.stop()
            self._lc = None

    def check_signal(self):
        rssi = self.manager.link_status.get_rssi()

        if not rssi:
            print("No RSSI data available")
            return
        
        for antenna in rssi:
            p5 = antenna.get("p5")
            if p5 < -70 and self.level_index < len(self.levels) - 1:
                print(f"Low RSSI detected: {p5} dBm")
                self.increase_txpower_level()
                return
            elif p5 > -20 and self.level_index > 0:
                print(f"High RSSI detected: {p5} dBm")
                self.decrease_txpower_level()
                return
                    
    def set_txpower_level(self, level):
        if level < len(self.levels) and level >= 0:
            self.level_index = level
            txpower = self.levels[self.level_index]
            for wlan in self.manager.wlans:
                call_and_check_rc("iw", "dev", wlan, "set", "txpower", "fixed", str(txpower))
            print(f"Setting TX power to {txpower}")
        else:
            print(f"TX power level {txpower} not found in levels")

    def increase_txpower_level(self):
        if self.level_index < len(self.levels) - 1:
            self.level_index += 1
            self.set_txpower(self.levels[self.level_index])

    def decrease_txpower_level(self):
        if self.level_index > 0:
            self.level_index -= 1
            self.set_txpower(self.levels[self.level_index])