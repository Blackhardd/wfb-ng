import math
import time
import msgpack
from dataclasses import dataclass

from twisted.python import log
from twisted.internet import reactor, task, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver

from . import call_and_check_rc
from .conf import settings


class Utils:
    @staticmethod
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

@dataclass # Тип данных для хранения статистики измерений
class MeasurementStats:
    p_total: int
    p_bad: int
    rssi: int
    snr: int

@dataclass # Контейнер для хранения измерений по типам потоков
class ChannelMeasurements:
    video: list      # Список для видео(поток видео,в H265 или H264)
    mavlink: list    # Список для mavlink(телеметрия)
    tunnel: list     # Список для туннеля(хз что в нем)
    
    def __init__(self):
        self.video = []
        self.mavlink = []
        self.tunnel = []
    
    def get(self, rx_id: str):
        """Получить список измерений по rx_id"""
        if rx_id == 'video':
            return self.video
        elif rx_id == 'mavlink':
            return self.mavlink
        elif rx_id == 'tunnel':
            return self.tunnel
        return None
    
    def has(self, rx_id: str) -> bool:
        """Проверить наличие rx_id"""
        return rx_id in ['video', 'mavlink', 'tunnel']
    
    def append(self, rx_id: str, stats: MeasurementStats):
        """Добавить измерение"""
        measurements = self.get(rx_id)
        if measurements is not None:
            measurements.append(stats)
    
    def items(self):
        """Итератор по парам (rx_id, measurements)"""
        return [
            ('video', self.video),
            ('mavlink', self.mavlink),
            ('tunnel', self.tunnel)
        ]
    
    def values(self):
        """Итератор по спискам измерений"""
        return [self.video, self.mavlink, self.tunnel]
    
    def clear(self):
        """Очистить все измерения"""
        self.video = []
        self.mavlink = []
        self.tunnel = []

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

        p_total = 0
        p_bad = 0
        rssi = 0
        snr = 0

        rx_id = str(rx_id).replace(" rx", "")
        
        if session is not None:
            rssi = round(sum(v[2] for v in rx_ant_stats.values()) / len(rx_ant_stats)) if rx_ant_stats else 0
            snr = round(sum(v[5] for v in rx_ant_stats.values()) / len(rx_ant_stats)) if rx_ant_stats else 0

            if rx_id not in self._prev:
                p_total = packets['all'][1]
                p_bad = packets['lost'][1] + packets['dec_err'][1]
            else:
                p_total_diff   = packets['all'][1]     - self._prev[rx_id]['packets']['all'][1]
                p_lost_diff    = packets['lost'][1]    - self._prev[rx_id]['packets']['lost'][1]
                p_dec_err_diff = packets['dec_err'][1] - self._prev[rx_id]['packets']['dec_err'][1]
                
                if p_total_diff < 0:
                    p_total = packets['all'][1]
                    p_bad = packets['lost'][1] + packets['dec_err'][1]
                else:
                    p_total = p_total_diff
                    p_bad = max(0, p_lost_diff) + max(0, p_dec_err_diff)

            self._prev[rx_id] = {
                'packets': packets.copy(),
                'rssi': rssi,
                'snr': snr
            }

        stats = MeasurementStats(p_total=p_total, p_bad=p_bad, rssi=rssi, snr=snr)
        self.channels.current().add_measurement(rx_id, stats)
    
    def reset(self):
        self._prev = {}
    
class Channel():
    def __init__(self, freq):
        self._freq = freq
        self._score = [100]
        self._score_update_time = 0
        self._measurements = ChannelMeasurements()  #Sander: новый dataclass вместо словаря
        self._switched_at = 0
        self._became_dead_at = 0  # Время когда канал стал фиговым

        self._on_score_updated = None

    def _update_score(self):
        frames = 3

        per = self.per(frames)
        snr = self.snr(frames)

        pen_per = 75 * Utils.clamp(per / 5, 0.0, 1.0)
        pen_snr = 25 * Utils.clamp((20 - snr) / 20, 0.0, 1.0)

        self._score.append(100 - (pen_per + pen_snr))

        log.msg(f"Channel {self._freq}{' MHz' if self._freq > 2000 else ''} - PER: {per}%, SNR: {snr:.2f} dB, Score: {self._score[-1]:.2f}")

        packet_info_parts = []
        for rx_id in ['video', 'mavlink', 'tunnel']:
            meas = self._measurements.get(rx_id)
            if meas is not None and len(meas) > 0:
                p_total = sum(stats.p_total for stats in meas[-frames:])
                p_bad = sum(stats.p_bad for stats in meas[-frames:])
                
                rx_label = 'vid' if rx_id == 'video' else ('mk' if rx_id == 'mavlink' else 'tl')
                packet_info_parts.append(f"{rx_label}: [tot={p_total},bad={p_bad}]")
        
        if packet_info_parts:
            packet_info = "PER: {}% {}".format(per, " ".join(packet_info_parts))
            log.msg(packet_info)

        self._score_update_time = time.time()

        if self._on_score_updated:
            self._on_score_updated(self)

    def freq(self):
        return self._freq

    def per(self, frames=None):
        per = 0

        if frames is None or frames < 1:
            frames = 1

        # Безопасная проверка на пустые измерения
        meas_lengths = [len(meas) for meas in self._measurements.values() if len(meas) > 0]
        if not meas_lengths:
            return 100 
        
        max_frames = min(meas_lengths)
        if max_frames < frames:
            frames = max_frames

        p_total = 0
        p_bad = 0

        for rx_id, meas in self._measurements.items():
            # Проверка что список не пустой перед доступом к последнему элементу
            if len(meas) == 0 or meas[-1].p_total == 0:
                continue

            p_total += sum(stats.p_total for stats in meas[-frames:])
            p_bad += sum(stats.p_bad for stats in meas[-frames:])

        if p_total > 0:
            p_bad  = min(p_bad, p_total)
            per    = round((p_bad / p_total) * 100)
            per    = Utils.clamp(per, 0, 100)
        else:
            per = 100

        return per

    def snr(self, frames=None):
        snr_vals = []

        if frames is None or frames < 1:
            frames = 1
        
        active_meas = []
        for meas in self._measurements.values():
            if len(meas) != 0:
                active_meas.append(meas)
        if len(active_meas) == 0:
            return 0
        min_lengths = []
        for meas in active_meas:
            min_lengths.append(len(meas))
        
        max_frames = min(min_lengths)
        if frames > max_frames:
            frames = max_frames
        
        for meas in active_meas:
            window = [stats.snr for stats in meas[-frames:]]
            if any(v != 0 for v in window):
                snr_vals.append(window)

        if not snr_vals:
            return 0

        return avg_db_snr(snr_vals)
    
    def score(self):
        return self._score[-1]
    
    def has_received_data(self):
        """Проверка что канал получал хотя бы один пакет (связь была установлена)"""
        for meas in self._measurements.values():
            if len(meas) > 0:
                # Проверяем что хотя бы одно измерение имело пакеты
                for stats in meas:
                    if stats.p_total > 0:
                        return True
        return False
    
    def is_dead(self, grace_seconds=3):
        current_per = self.per()
        
        # Если PER не 100%, канал жив - сбрасываем таймер
        if current_per < 100:
            self._became_dead_at = 0
            return False
        
        # PER = 100%, проверяем отложенный период
        current_time = time.time()
        
        # Отоложенный период после переключения канала
        if self._switched_at > 0:
            time_since_switch = current_time - self._switched_at
            if time_since_switch < grace_seconds:
                return False
        
        # Отмечаем время когда канал впервые стал не о чем
        if self._became_dead_at == 0:
            self._became_dead_at = current_time
            log.msg(f"Channel {self._freq} became dead (PER=100%), grace period {grace_seconds}s started")
            return False  # Не считаем мертвым в первый момент
        
        # Проверяем прошло ли достаточно времени с момента когда канал стал не о чем
        time_since_dead = current_time - self._became_dead_at
        if time_since_dead < grace_seconds:
            return False
        
        # Канал не о чем достаточно долго
        return True
    
    def add_measurement(self, rx_id, stats):
        if not self._measurements.has(rx_id):
            return
        
        self._measurements.append(rx_id, stats)

        lengths = {
            k: len(v)
            for k, v in self._measurements.items()
        }

        if len(set(lengths.values())) == 1:
            self._update_score()

    def set_on_score_updated(self, callback):
        self._on_score_updated = callback

    def clear_measurements(self):
        self._measurements.clear()  # Используем метод dataclass
        self._switched_at = time.time()
        self._became_dead_at = 0  # Сбрасываем таймер фигового канала при переключении

class ChannelsFactory:
    @classmethod
    def create(cls, freqs):
        channels = []
        for freq in freqs:
            channels.append(Channel(freq))
        return channels

class Channels:
    def __init__(self, freqsel, channel_frequencies):
        self.freqsel = freqsel
        self._index = 0
        self._list = ChannelsFactory.create(channel_frequencies)

        for chan in self._list:
            chan.set_on_score_updated(self.on_channel_score_updated)

        reactor.callWhenRunning(lambda: defer.maybeDeferred(self._init_stats()))

    def _init_stats(self):
        self._stats = StatsFactory(self)
        stats_port = getattr(settings, self.freqsel.manager.get_type()).stats_port
        reactor.connectTCP("127.0.0.1", stats_port, self._stats)

    def count(self):
        return len(self._list)

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

    def next_channel(self):
        next_index = (self._index + 1) % len(self._list)
        return self._list[next_index]

    def set_current(self, channel):
        for i, c in enumerate(self._list):
            if c is channel:
                self._index = i
                return
        freq = channel.freq() if hasattr(channel, 'freq') else channel
        for i, c in enumerate(self._list):
            if c.freq() == freq:
                self._index = i
                return

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

        # Не переключаем каналы пока связь не была установлена хоть 1 раз
        if not self.freqsel.is_ready_for_hop():
            # Логируем только раз в 5 секунд
            current_time = time.time()
            should_log = (current_time - self.freqsel._last_not_ready_log_time) >= 5.0
            
            if should_log and self.freqsel.is_in_startup_grace_period():
                elapsed = time.time() - self.freqsel._startup_time
                remaining = self.freqsel._startup_grace_period - elapsed
                
                # Логируем только значимые события
                if channel.is_dead():
                    log.msg(f"System not ready for hop: no link established yet (startup {elapsed:.1f}s, channel is dead)")
                    self.freqsel._last_not_ready_log_time = current_time
                elif channel.per() >= 10 or channel.score() < 50:
                    log.msg(f"System not ready for hop: no link established yet (startup {elapsed:.1f}s, PER={channel.per()}%, score={channel.score():.2f})")
                    self.freqsel._last_not_ready_log_time = current_time
            
            return  # Блокируем ВСЕ hop пока связь не установлена
        
        # Система готова к hop - нормальная логика переключения
        if channel.is_dead():
            self.freqsel.schedule_recovery_hop()
            return
        
        if channel.per() >= 10 or channel.score() < 50:
            self.freqsel.hop()
        elif self.freqsel.is_hop_timed_out(30) and channel.score() < 100:
            # Периодически проверяем другие каналы, если текущий не идеальный
            self.freqsel.hop()

class FrequencySelection:
    def __init__(self, manager):
        self.manager = manager

        self.enabled = settings.common.freq_sel_enabled
        self.channels = Channels(self, [settings.common.wifi_channel, *settings.common.freq_sel_channels])

        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

        self._startup_time = time.time()  # Время запуска гс или дрона
        self._last_hop_time = self._startup_time  # Инициализируем текущим временем, чтобы избежать немедленного hop
        self._startup_grace_period = 20.0  # Период ожидания20 секунд
        self._last_not_ready_log_time = 0  # Время последнего лога "not ready" (защита от спама)
        self._link_established_first_time = False  # Флаг первого установления связи
        
        if self.is_enabled():
            startup_channel = self.channels.current().freq()
            log.msg(f"Frequency selection initialized. Startup channel: {startup_channel}{' MHz' if startup_channel > 2000 else ''}, Grace period: {self._startup_grace_period}s")

    def is_enabled(self):
        if self.enabled and self.channels.count() > 1:
            return True
        log.msg("Frequency selection is disabled or not configured properly.")
        return False
    
    def is_in_startup_grace_period(self):
        """Проверка, находимся ли мы в периоде инициализации"""
        elapsed = time.time() - self._startup_time
        return elapsed < self._startup_grace_period
    
    def is_link_established(self):
        """Проверка что связь с дроном была """
        # Проверяем текущий канал - получал ли он пакеты
        return self.channels.current().has_received_data()
    
    def is_ready_for_hop(self):
        """Проверка что система готова к hop"""
        # Период ожидания при старте завершен ИЛИ связь уже установлена
        if self.is_link_established():
            # Если связь установилась впервые - сбрасываем таймер hop
            # чтобы не учитывать время ожидания дрона
            if not self._link_established_first_time:
                self._last_hop_time = time.time()
                self._link_established_first_time = True
                log.msg("Link established for the first time, hop timer reset")
            
            # Если связь установлена, можем hop даже во время ожидания
            return True   
        # Если связи нет - ждем окончания периода ожидания
        return False
    
    def is_hop_timed_out(self, timeout):
        if time.time() - self._last_hop_time >= timeout:
            return True
        return False

    def hop(self):
        if not self.is_enabled():
            return
        
        if self._is_scheduled_hop or self._is_scheduled_recovery_hop:
            log.msg("Hop already scheduled, skipping new hop request")
            return
        
        log.msg(f"[HOP REQUEST] Initiating hop from {self.manager.get_type()}")
        
        if not hasattr(self.manager, 'client_f'):
            log.msg("ERROR: Manager has no client_f, cannot send hop command")
            return
        
        d = self.manager.client_f.send_command({"command": "freq_sel_hop"})
        
        if d is None:
            log.msg("ERROR: send_command returned None, connection not ready")
            return
        
        d.addCallback(self._on_hop_response)
        d.addErrback(self._on_hop_error)

    def _on_hop_response(self, res):
        log.msg(f"[HOP RESPONSE] Received: {res}")
        action_time = res.get("time")
        if action_time:
            self.schedule_hop(action_time)
        else:
            log.msg("ERROR: No 'time' in hop response, cannot schedule")
    
    def _on_hop_error(self, err):
        log.msg(f"[HOP ERROR] Failed to send hop command: {err}")

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

    @defer.inlineCallbacks
    def do_hop(self, channel=None):
        target_channel = channel if channel else self.channels.next_channel()

        if not target_channel or not isinstance(target_channel, Channel):
            log.msg("ERROR: Invalid target_channel in do_hop, aborting")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        if target_channel.freq() == self.channels.current().freq():
            log.msg("Already on the selected channel, skipping hop.")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        if self._is_scheduled_recovery_hop and not self.channels.current().is_dead():
            self._is_scheduled_recovery_hop = False
            log.msg("Recovery channel hop cancelled because the link is alive")
            return
        
        freq = target_channel.freq()
        score = target_channel.score()
        
        current = self.channels.current()
        log.msg(f"[HOP START] From: ch_idx={self.channels._index} freq={current.freq()}{' MHz' if current.freq() > 2000 else ''} -> To: freq={freq}{' MHz' if freq > 2000 else ''} (prev_score={score:.2f})")
        
        try:
            for wlan in self.manager.wlans:
                yield call_and_check_rc("iw", "dev", wlan, "set", "freq" if freq > 2000 else "channel", str(freq))
        except Exception as e:
            log.msg(f"ERROR: Failed to switch frequency on hardware: {e}")
            log.msg("State NOT updated - still on the old channel to avoid desync")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        self.channels.set_current(target_channel)

        target_channel.clear_measurements()

        self._last_hop_time = time.time()

        log.msg(f"[HOP DONE] Now: ch_idx={self.channels._index} freq={target_channel.freq()}{' MHz' if freq > 2000 else ''}")

        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

    def schedule_recovery_hop(self):
        if not self.is_enabled():
            return

        if self._is_scheduled_recovery_hop or self._is_scheduled_hop:
            return
        
        # Fix Comment on line R530 by Sander at 31/01/2025
        # Возврат к wifi_channel'у из common цфг при потере связи
        # Это гарантирует синхронизацию между дроном и Ground Station при lost или полной потере PER 100%
        channel = self.channels.reserve()
        
        # Если уже на резервном канале, не переключаемся
        # Дрон рано или поздно вылетит из зоны помех и связь восстановится
        if self.channels.current().freq() == channel.freq():
            log.msg(f"Already on reserve channel {channel.freq()}, recovery hop cancelled")
            return

        self._is_scheduled_recovery_hop = True

        action_time = self.get_action_time()
        delay = max(0, action_time - time.time())
        task.deferLater(reactor, delay, self.do_hop, channel)

        log.msg(f"Recovery hop to reserve channel {channel.freq()}{' MHz' if channel.freq() > 2000 else ''} scheduled in {delay:.2f}s")
    
    def get_action_time(self, interval=None):
        if interval is None:
            interval = 1.0
        return time.time() + interval
    
    def reset_all_channels_stats(self):
        log.msg("Сброс статистики.Проверка: офлайн? дизарм?")
        for channel in self.channels.all():
            channel.clear_measurements()
            channel._became_dead_at = 0
            channel._score = [100]
        
        # Сбрасываем флаги и таймеры
        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False
        self._link_established_first_time = False
        self._last_hop_time = time.time()