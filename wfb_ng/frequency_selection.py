import math
import time
import msgpack

from twisted.python import log
from twisted.internet import reactor, task, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver

from . import call_and_check_rc
from .conf import settings

def clamp(n, min_val, max_val):
    return max(min_val, min(n, max_val))

def avg_db_snr(snr_list):
    if not snr_list or not snr_list[0]:
        return 0

    s = 0.0
    n = 0
    for row in snr_list:
        for snr_db in row:
            s += 10 ** (snr_db / 10)
            n += 1

    avg_lin = s / n

    return 10.0 * math.log10(avg_lin)

class Stats(Int32StringReceiver):
    MAX_LENGTH = 1024 * 1024

    def stringReceived(self, string):
        attrs = msgpack.unpackb(string, strict_map_key=False, use_list=False, raw=False)

        if attrs['type'] == 'rx':
            self.factory.update(attrs)

class StatsFactory(ReconnectingClientFactory):
    noisy = False
    maxDelay = 1.0

    def __init__(self, channels):
        ReconnectingClientFactory.__init__(self)

        self.channels = channels

        self.reset()

    def buildProtocol(self, addr):
        self.resetDelay()
        self.reset()

        p = Stats()
        p.factory = self
        return p
    
    def clientConnectionLost(self, connector, reason):
        self.reset()
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        self.reset()
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def update(self, data):
        rx_id = data.get('id')
        packets = data.get('packets')
        rx_ant_stats = data.get('rx_ant_stats')
        session = data.get('session')

        # Initialize stats container
        stats = {
            'p_total': 0,
            'p_bad': 0,
            'rssi': 0,
            'snr': 0
        }

        rx_id = str(rx_id).replace(" rx", "")
        # log.msg(f"Received stats for {rx_id} RX: {data}")
        
        # Validate fields
        if session is not None:
            # Calculate average RSSI and SNR across antennas
            rssi = round(sum(v[2] for v in rx_ant_stats.values()) / len(rx_ant_stats)) if rx_ant_stats else 0
            stats['rssi'] = rssi

            snr = round(sum(v[5] for v in rx_ant_stats.values()) / len(rx_ant_stats)) if rx_ant_stats else 0
            stats['snr'] = snr

            if rx_id not in self._prev:
                stats['p_total'] = packets['all'][1]
                stats['p_bad'] = packets['lost'][1] + packets['dec_err'][1]
            else:
                stats['p_total'] = packets['all'][1] - self._prev[rx_id]['packets']['all'][1]
                stats['p_bad'] = (packets['lost'][1] - self._prev[rx_id]['packets']['lost'][1]) + (packets['dec_err'][1] - self._prev[rx_id]['packets']['dec_err'][1])

            # Set previous state
            self._prev[rx_id] = {
                'packets': packets.copy(),
                'rssi': rssi,
                'snr': snr
            }

        # Finally store stats to the current channel measurements
        self.channels.current().add_measurement(rx_id, stats)
    
    def reset(self):
        self._prev = {}
    
class Channel():
    def __init__(self, freq):
        self._freq = freq
        self._score = [100]
        self._score_update_time = 0
        self._measurements = {
            'video': [],
            'mavlink': [],
            'tunnel': [],
        }

        self._on_score_updated = None

    def _update_score(self):
        frames = 3 # Number of recent samples to consider

        per = self.per(frames)
        snr = self.snr(frames)

        pen_per = 75 * clamp(per / 20, 0.0, 1.0)  # Penalty for PER over 20%
        pen_snr = 25 * clamp((20 - snr) / 20, 0.0, 1.0)  # Penalty for SNR below 20dB

        self._score.append(100 - (pen_per + pen_snr))

        log.msg(f"Channel {self._freq}{' MHz' if self._freq > 2000 else ''} - PER: {per}%, SNR: {snr:.2f} dB, Score: {self._score[-1]:.2f}")

        # Update last score update time
        self._score_update_time = time.time()

        if self._on_score_updated:
            self._on_score_updated(self)

    def freq(self):
        return self._freq

    def per(self, frames=None):
        per = 0

        # Validate frame parameter
        if frames is None or frames < 1:
            frames = 1

        # Determine the maximum number of frames available
        max_frames = min(len(meas) for meas in self._measurements.values())
        if max_frames < frames:
            frames = max_frames

        # Aggregate total and bad packets across all receivers
        p_total = 0
        p_bad = 0

        for rx_id, meas in self._measurements.items():
            if meas[-1]['p_total'] == 0:
                continue

            p_total += sum(stats['p_total'] for stats in meas[-frames:])
            p_bad += sum(stats['p_bad'] for stats in meas[-frames:])

        per = round((p_bad / p_total) * 100) if p_total > 0 else 100

        return per

    def snr(self, frames=None):
        snr_vals = []

        # Validate frame parameter
        if frames is None or frames < 1:
            frames = 1

        # Determine the maximum number of frames available
        max_frames = min(len(meas) for meas in self._measurements.values())
        if max_frames == 0:
            return 0
        
        if frames < max_frames:
            frames = max_frames

        for rx_id, meas in self._measurements.items():
            snr_vals.append([stats['snr'] for stats in meas[-frames:]])

        for snr_list in snr_vals:
            if snr_list[-1] == 0:
                return 0

        return avg_db_snr(snr_vals)
    
    def score(self):
        return self._score[-1]
    
    def is_dead(self):
        return self.per() == 100
    
    def add_measurement(self, rx_id, stats):
        if rx_id not in self._measurements:
            return
        
        self._measurements[rx_id].append(stats)

        lengths = {
            k: len(v)
            for k, v in self._measurements.items()
        }

        if len(set(lengths.values())) == 1:
            self._update_score()

    def set_on_score_updated(self, callback):
        self._on_score_updated = callback

class ChannelsFactory:
    @classmethod
    def create(cls, freqs):
        channels = []
        for freq in freqs:
            channels.append(Channel(freq))
        return channels

# Channels class to manage frequency channels
class Channels:
    def __init__(self, freqsel, channel_frequencies):
        self.freqsel = freqsel
        self._index = 0
        self._list = ChannelsFactory.create(channel_frequencies)

        for chan in self._list:
            chan.set_on_score_updated(self.on_channel_score_updated)

        # Create channel stats factory and connect
        reactor.callWhenRunning(lambda: defer.maybeDeferred(self._init_stats()))

    def _init_stats(self):
        self._stats = StatsFactory(self)
        stats_port = getattr(settings, self.freqsel.manager.get_type()).stats_port
        reactor.connectTCP("127.0.0.1", stats_port, self._stats)

    # Return the number of channels
    def count(self):
        return len(self._list)

    # Get all channels
    def all(self):
        return self._list
    
    def first(self):
        self._index = 0
        return self.current()
    
    def current(self):
        return self._list[self._index]
    
    def prev(self):
        if self._index > 0:
            self._index -= 1
        else:
            self._index = len(self._list) - 1
        return self.current()
    
    def next(self):
        if self._index < len(self._list) - 1:
            self._index += 1
        else:
            self._index = 0
        return self.current()

    def best(self):
        best_chan = max(self._list, key=lambda chan: chan.score())
        return best_chan
    
    def reserve(self):
        return self._list[0]
    
    def by_freq(self, freq):
        for chan in self._list:
            if chan.freq() == freq:
                return chan
        return None
    
    def avg_score(self):
        total_score = sum(chan.score() for chan in self._list)
        return total_score / len(self._list) if self._list else 0
    
    def on_channel_score_updated(self, channel):
        # Schedule recovery hop if link is dead
        if channel.is_dead():
            self.freqsel.schedule_recovery_hop()
            return
        
        # Drone makes decision only about reserve channel hopping
        if not self.freqsel.manager.get_type() == "drone":
            return

        # Check channels average score and decide on hopping
        if self.avg_score() > 60:
            # Only hop if the current channel score is low
            if channel.per() >= 15 or channel.score() < 50:
                self.freqsel.hop()
        elif self.freqsel.is_hop_timed_out(30):
            self.freqsel.hop()

class FrequencySelection:
    def __init__(self, manager):
        self.manager = manager

        # Load settings
        self.enabled = settings.common.freq_sel_enabled
        self.channels = Channels(self, [settings.common.wifi_channel, *settings.common.freq_sel_channels])

        # Scheduling flags
        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

        self._last_hop_time = 0

    def is_enabled(self):
        if self.enabled and self.channels.count() > 1:
            return True
        log.msg("Frequency selection is disabled or not configured properly.")
        return False
    
    def last_hop_timed_out(self, timeout):
        if time.time() - self._last_hop_time >= timeout:
            return True
        return False

    def hop(self):
        if not self.is_enabled() or self._is_scheduled_hop or self._is_scheduled_recovery_hop:
            return
        
        d = self.manager.client_f.send_command({"command": "freq_sel_hop"})
        d.addCallback(lambda res: self.schedule_hop(res.get("time")))
        d.addErrback(lambda err: self.hop)

    # Schedule a frequency hop at a specific time
    def schedule_hop(self, action_time=None):
        if not self.is_enabled():
            return
        
        if self._is_scheduled_hop or self._is_scheduled_recovery_hop:
            return

        self._is_scheduled_hop = True
        
        if action_time is None:
            action_time = self.get_action_time()
        
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self.do_hop)

        log.msg(f"Frequency hop scheduled to execute in {delay:.2f} seconds")

        return action_time

    def do_hop(self, channel=None):
        chan = channel
        if not chan:
            chan = self.channels.next()

        if chan.freq() == self.channels.current().freq():
            log.msg("Already on the selected channel, skipping hop.")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        if self._is_scheduled_recovery_hop and not self.channels.current().is_dead():
            self._is_scheduled_recovery_hop = False
            log.msg("Recovery channel hop cancelled because the link is alive")
            return
        
        if isinstance(chan, Channel):
            freq = chan.freq()
            score = chan.score()
        else:
            freq = chan
            score = 100
        
        log.msg(f"Hopping to channel {freq}{' MHz' if freq > 2000 else ''} with previous score {score:.2f}")
        
        for wlan in self.manager.wlans:
            call_and_check_rc("iw", "dev", wlan, "set", "freq" if freq > 2000 else "channel", str(freq))

        # Update last hop time
        self._last_hop_time = time.time()

        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

    # Schedule a recovery hop
    def schedule_recovery_hop(self):
        if not self.is_enabled():
            return

        if self._is_scheduled_recovery_hop or self._is_scheduled_hop:
            return
        
        channel = self.channels.reserve()
        
        # If already on the reserve channel, pick the next one
        if self.channels.current().freq() == channel.freq():
            channel = self.channels.next()

        self._is_scheduled_recovery_hop = True

        action_time = self.get_action_time()
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self.do_hop, channel)

        log.msg(f"Recovery channel hop is scheduled to execute in {delay:.2f} seconds")
    
    def get_action_time(self, interval=None):
        if interval is None:
            interval = 1.0
        return time.time() + interval
