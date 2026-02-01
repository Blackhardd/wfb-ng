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


# ==================== НАСТРОЙКИ FREQUENCY SELECTION ====================

# --- Настройки расчёта Score ---
SCORE_FRAMES = 3              # Количество фреймов для усреднения
SCORE_PER_WEIGHT = 75         # Вес PER в итоговом score (0-100)
SCORE_SNR_WEIGHT = 25         # Вес SNR в итоговом score (0-100)
SCORE_PER_MAX_PENALTY = 10    # При каком PER максимальный штраф (%)
SCORE_SNR_MIN_THRESHOLD = 20  # Ниже какого SNR максимальный штраф (dB)

# --- Настройки триггеров HOP ---
HOP_PER_THRESHOLD = 20        # HOP при PER >= 20%
HOP_SCORE_THRESHOLD = 50      # HOP при score < 50
HOP_TIMEOUT_SECONDS = 30      # Таймаут нахождения на канале (сек)
HOP_TIMEOUT_SCORE = 80        # При таймауте HOP если score < 80

# --- Защиты от ложных срабатываний ---
HOP_MIN_TIME_ON_CHANNEL = 5.0     # Минимум 5 сек на канале перед HOP
HOP_MIN_INTERVAL = 3.0            # Минимум 3 сек между hop запросами
CHANNEL_MIN_MEASUREMENTS = 3      # Минимум фреймов для определения dead channel
CHANNEL_KEEP_HISTORY = 5          # Сколько последних измерений сохранять при hop

# --- Настройки определения offline/dead ---
DRONE_OFFLINE_THRESHOLD = 30.0    # Через сколько секунд без пакетов дрон считается offline
CHANNEL_DEAD_GRACE_PERIOD = 5     # Период ожидания перед объявлением канала мёртвым
# После стольких секунд без пакетов переходим на reserve (wifi_channel),
# чтобы при перезапуске дрона (он стартует на wifi_channel) GS был на том же канале
OFFLINE_HOP_TO_RESERVE_AFTER = 120.0  # сек (2 мин)

# --- Настройки startup ---
STARTUP_GRACE_PERIOD = 20.0       # Период ожидания связи при старте

# =======================================================================


class Utils:
    @staticmethod
    def clamp(n, min_val, max_val):
        return max(min_val, min(n, max_val))
    
    @staticmethod
    def safe_counter_diff(current, previous):
        """ current: текущее значение счётчикаprevious: предыдущее значение счётчика """
        diff = current - previous
        if diff < 0:
            # Переполнение счётчика или сброс - берём абсолютное значение
            return current
        return diff

def avg_db_snr(snr_list):
    """
    Усреднение SNR в dB через линейную область
    1. Переводим каждый SNR из dB в линейную область (10^(snr/10))
    2. Усредняем в линейной области
    3. Перевести обратно в dB (10*log10)
    """
    if not snr_list or not snr_list[0]:
        return 0

    s = 0.0
    n = 0
    for row in snr_list:
        for snr_db in row:
            if snr_db > 0:  # Фильтруем нулевые значения
                s += 10 ** (snr_db / 10)
                n += 1

    if n == 0:
        return 0

    avg_lin = s / n
    return 10.0 * math.log10(avg_lin)

@dataclass # Храним измерения по потокам 
class MeasurementStats:
    p_total: int
    p_bad: int
    rssi: int
    snr: int

@dataclass # Храним измерения по типам потоков
class ChannelMeasurements:
    video: list   
    mavlink: list
    tunnel: list
    
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
            # Округляем до целых чисел для RSSI и SNR
            rssi = int(round(sum(v[2] for v in rx_ant_stats.values()) / len(rx_ant_stats))) if rx_ant_stats else 0
            snr = int(round(sum(v[5] for v in rx_ant_stats.values()) / len(rx_ant_stats))) if rx_ant_stats else 0

            if rx_id not in self._prev:
                # Первое измерение - берём абсолютные значения
                p_total = packets['all'][1]
                p_bad = packets['lost'][1] + packets['dec_err'][1]
            else:
                # Обрабатываем "переполнение" 
                p_total = Utils.safe_counter_diff(
                    packets['all'][1],
                    self._prev[rx_id]['packets']['all'][1]
                )
                p_lost = Utils.safe_counter_diff(
                    packets['lost'][1],
                    self._prev[rx_id]['packets']['lost'][1]
                )
                p_dec_err = Utils.safe_counter_diff(
                    packets['dec_err'][1],
                    self._prev[rx_id]['packets']['dec_err'][1]
                )
                p_bad = p_lost + p_dec_err

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
        self._measurements = ChannelMeasurements() 
        self._switched_at = 0
        self._became_dead_at = 0  # Время когда канал стал фиговым
        self._last_packet_time = 0  # Время последнего полученного пакета

        self._on_score_updated = None

    def _update_score(self):
        """Обновление score канала на основе PER и SNR"""
        per = self.per(SCORE_FRAMES)
        snr = self.snr(SCORE_FRAMES)
        
        # Рассчитываем штрафы
        pen_per = SCORE_PER_WEIGHT * Utils.clamp(per / SCORE_PER_MAX_PENALTY, 0.0, 1.0)
        pen_snr = SCORE_SNR_WEIGHT * Utils.clamp((SCORE_SNR_MIN_THRESHOLD - snr) / SCORE_SNR_MIN_THRESHOLD, 0.0, 1.0)

        self._score.append(100 - (pen_per + pen_snr))

        log.msg(f"Channel {self._freq}{' MHz' if self._freq > 2000 else ''} - PER: {per}%, SNR: {snr:.2f} dB, Score: {self._score[-1]:.2f}")

        packet_info_parts = []
        for rx_id in ['video', 'mavlink', 'tunnel']:
            meas = self._measurements.get(rx_id)
            if meas is not None and len(meas) > 0:
                p_total = sum(stats.p_total for stats in meas[-SCORE_FRAMES:])
                p_bad = sum(stats.p_bad for stats in meas[-SCORE_FRAMES:])
                
                rx_label = 'vid' if rx_id == 'video' else ('mk' if rx_id == 'mavlink' else 'tl')
                packet_info_parts.append(f"{rx_label}: [tot={p_total},bad={p_bad}]")
        
        packet_info = ""
        if packet_info_parts:
            packet_info = " ".join(packet_info_parts)
            log.msg("PER: {}% {}".format(per, packet_info))

        
        self._score_update_time = time.time()

        if self._on_score_updated:
            self._on_score_updated(self)

    def freq(self):
        return self._freq

    def per(self, frames=None):
        """Расчёт PER (Packet Error Rate) по всем rx за последние N фреймов"""
        if frames is None or frames < 1:
            frames = 1
        meas_lengths = [len(meas) for meas in self._measurements.values() if len(meas) > 0]
        if not meas_lengths:
            return 100 # нет измерений - нет канала - нет конекта - нет проблем 
        
        max_frames = min(meas_lengths)
        if max_frames < frames:
            frames = max_frames

        p_total = 0
        p_bad = 0

        for rx_id, meas in self._measurements.items():
            # Пропускаем пустые измерения или измерения без пакетов
            if len(meas) == 0:
                continue
            
            # Суммируем пакеты за последние N фреймов
            for stats in meas[-frames:]:
                if stats.p_total > 0:  # Учитываем только фреймы с пакетами
                    p_total += stats.p_total
                    p_bad += stats.p_bad

        if p_total > 0:
            # Защита: p_bad не может быть больше p_total
            p_bad = min(p_bad, p_total)
            per = round((p_bad / p_total) * 100)
            per = Utils.clamp(per, 0, 100)
        else:
            per = 100  # Нет пакетов → 100% потерь

        return per

    def snr(self, frames=None):
        """Расчёт среднего SNR по всем rx за последние N фреймов"""
        if frames is None or frames < 1:
            frames = 1
        
        # Собираем только непустые измерения
        active_meas = [meas for meas in self._measurements.values() if len(meas) > 0]
        if not active_meas:
            return 0
        
        # Определяем доступное количество фреймов
        max_frames = min(len(meas) for meas in active_meas)
        if frames > max_frames:
            frames = max_frames
        
        # Собираем SNR значения, фильтруя нулевые
        snr_vals = []
        for meas in active_meas:
            window = [stats.snr for stats in meas[-frames:] if stats.snr != 0]
            if window:  # Добавляем только если есть ненулевые значения
                snr_vals.append(window)

        if not snr_vals:
            return 0

        return avg_db_snr(snr_vals)
    
    def score(self):
        # возвращаю оценку канала в диапазоне от нуля до 100-та 
        if not self._score:
            return 100
        return self._score[-1]
    
    def has_received_data(self):
        for meas in self._measurements.values():
            if len(meas) > 0:
                # проверка на получение пакетов хоть с одного rx_id
                for stats in meas:
                    if stats.p_total > 0:
                        return True
        return False
    
    def has_any_measurements(self):
        for meas in self._measurements.values():
            if len(meas) > 0:
                return True
        return False
    
    def is_drone_offline(self, threshold_seconds=DRONE_OFFLINE_THRESHOLD):
        """
        Returns:
            True - дрон никогда не подключался ИЛИ последний пакет >threshold_seconds назад
            False - дрон активен (последний пакет был недавно)
        """
        if not self.has_received_data():
            return True
        
        if self._last_packet_time == 0:
            return True
        
        time_since_last_packet = time.time() - self._last_packet_time
        return time_since_last_packet > threshold_seconds
    
    def is_dead(self, grace_seconds=CHANNEL_DEAD_GRACE_PERIOD):
        """
        Args:
            grace_seconds: grace period перед объявлением канала мёртвым
        """
        current_per = self.per()  
        # Защита: Если PER не 100%, канал жив - сбрасываем таймер
        if current_per < 100:
            self._became_dead_at = 0
            return False
        # Защита: проверка на то что дрон давно offline
        if self.is_drone_offline(threshold_seconds=DRONE_OFFLINE_THRESHOLD):
            self._became_dead_at = 0
            return False 
        # Защита: PER = 100% И дрон БЫЛ активен недавно 
        current_time = time.time()
        # Защита: требуем минимум измерений (избегаем ложных срабатываний)
        total_measurements = sum(len(m) for m in self._measurements.values())
        if total_measurements < CHANNEL_MIN_MEASUREMENTS:
            return False   
        # Защита: после переключения канала даём время на стабилизацию 
        if self._switched_at > 0:
            time_since_switch = current_time - self._switched_at
            if time_since_switch < grace_seconds:
                return False
        if self._became_dead_at == 0:
            self._became_dead_at = current_time
            log.msg(f"Channel {self._freq} became dead (PER=100%), grace period {grace_seconds}s started")
            return False
        
        # Защита: Проверяем истёк ли grace period
        time_since_dead = current_time - self._became_dead_at
        if time_since_dead < grace_seconds:
            return False
        
        # Защита: Канал мертвый достаточно долго - нужен recovery hop
        log.msg(f"Channel {self._freq} confirmed dead after {time_since_dead:.1f}s")
        return True
    
    def add_measurement(self, rx_id, stats):
        """
        Добавить измерение для rx_id
        """
        if not self._measurements.has(rx_id):
            return
        
        # Защита: Обновляем время последнего пакета если получили данные
        if stats.p_total > 0:
            self._last_packet_time = time.time()
        
        self._measurements.append(rx_id, stats)

        # Защита: Проверяем синхронизацию, все rx должны иметь одинаковое количество фреймов и измерений
        lengths = {k: len(v) for k, v in self._measurements.items()}

        if len(set(lengths.values())) == 1:
            self._update_score()

    def set_on_score_updated(self, callback):
        self._on_score_updated = callback

    def clear_measurements(self):
        """Очистка измерений при переключении канала (с сохранением истории)"""
        # Обрезаем каждый поток до последних CHANNEL_KEEP_HISTORY измерений
        for stream in [self._measurements.video, self._measurements.mavlink, self._measurements.tunnel]:
            if len(stream) > CHANNEL_KEEP_HISTORY:
                stream[:] = stream[-CHANNEL_KEEP_HISTORY:]
        
        # Обрезаем историю score тоже
        if len(self._score) > CHANNEL_KEEP_HISTORY:
            self._score = self._score[-CHANNEL_KEEP_HISTORY:]
        
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
        """
        Callback при обновлении score канала
        """
        # Защита: Не переключаем каналы пока связь не была установлена хоть 1 раз
        if not self.freqsel.is_ready_for_hop():
            # Логируем только раз в 5 секунд
            current_time = time.time()
            should_log = (current_time - self.freqsel._last_not_ready_log_time) >= 5.0
            
            if should_log and self.freqsel.is_in_startup_grace_period():
                elapsed = time.time() - self.freqsel._startup_time
                
                # Логируем только значимые события
                if channel.is_dead():
                    log.msg(f"System not ready for hop: no link established yet (startup {elapsed:.1f}s, channel is dead)")
                    self.freqsel._last_not_ready_log_time = current_time
                elif channel.per() >= 10 or channel.score() < 50:
                    log.msg(f"System not ready for hop: no link established yet (startup {elapsed:.1f}s, PER={channel.per()}%, score={channel.score():.2f})")
                    self.freqsel._last_not_ready_log_time = current_time
            
            return 
        
        # Защита: минимальное время на канале перед HOP
        time_on_channel = time.time() - channel._switched_at
        if time_on_channel < HOP_MIN_TIME_ON_CHANNEL:
            return
        
        # Защита: Есть готовность, продолжаем логику в норм состоянии
        if channel.is_dead():
            self.freqsel.schedule_recovery_hop()
            return
        else:
            # Защита: Канал НЕ мертвый - сбрасываем флаг recovery hop
            if self.freqsel._is_scheduled_recovery_hop:
                log.msg("Channel recovered, clearing recovery hop flag")
                self.freqsel._is_scheduled_recovery_hop = False
        
        # Долго нет пакетов → переходим на reserve (wifi_channel), дрон при перезапуске стартует там
        if channel.is_drone_offline(threshold_seconds=OFFLINE_HOP_TO_RESERVE_AFTER):
            if not self.freqsel._is_scheduled_recovery_hop and not self.freqsel._is_scheduled_hop:
                log.msg(f"Drone offline >{OFFLINE_HOP_TO_RESERVE_AFTER}s, hopping to reserve (wifi_channel) for reconnect")
            self.freqsel.schedule_recovery_hop()
            return
        
        # Защита: не пытаемся обычный HOP если дрон недавно offline (< 30 сек)
        if channel.is_drone_offline(threshold_seconds=DRONE_OFFLINE_THRESHOLD):
            return
        
        # Проверка условий для HOP (триггеры)
        if channel.per() >= HOP_PER_THRESHOLD:
            self.freqsel.hop()
        elif channel.score() < HOP_SCORE_THRESHOLD:
            self.freqsel.hop()
        elif self.freqsel.is_hop_timed_out(HOP_TIMEOUT_SECONDS) and channel.score() < HOP_TIMEOUT_SCORE:
            # HOP механизм: переключения если канал плохой
            time_on_channel = time.time() - self.freqsel._last_hop_time
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
        self._last_hop_request_time = 0  # Время последнего hop запроса 
        self._startup_grace_period = STARTUP_GRACE_PERIOD  # Период ожидания связи при старте
        self._last_not_ready_log_time = 0  # Время последнего лога "not ready" 
        self._link_established_first_time = False  # Флаг первого установления связи
        
        if self.is_enabled():
            startup_channel = self.channels.current().freq()
            log.msg(f"Frequency selection initialized. Startup channel: {startup_channel}{' MHz' if startup_channel > 2000 else ''}, Grace period: {STARTUP_GRACE_PERIOD}s")

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
        
        # если связь была установлена хоть раз, разрешаем hop
        if self._link_established_first_time:
            return True
        
        # Если связи никогда не было - ждем окончания grace period
        if not self.is_in_startup_grace_period():
            current_channel = self.channels.current()
            
            # Если дрон давно offline - НЕ разрешаем HOP 
            if current_channel.is_drone_offline(threshold_seconds=DRONE_OFFLINE_THRESHOLD):
                return False
            
            # Есть измерения И дрон был активен недавно - можем пробовать hop
            if current_channel.has_any_measurements():
                log.msg("Grace period expired, no link but have measurements, allowing hop")
                return True
        
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
        
        # Защита: минимальный интервал между hop запросами (предотвращает спам)
        current_time = time.time()
        time_since_last_request = current_time - self._last_hop_request_time
        
        if self._last_hop_request_time > 0 and time_since_last_request < HOP_MIN_INTERVAL:
            log.msg(f"Hop request too soon ({time_since_last_request:.1f}s < {HOP_MIN_INTERVAL}s), skipping")
            return
        
        self._last_hop_request_time = current_time
        
        log.msg(f"[HOP REQUEST] Initiating hop from {self.manager.get_type()}")
        
        if not hasattr(self.manager, 'client_f'):
            log.msg("ERROR: Manager has no client_f, cannot send hop command")
            return

        self._is_scheduled_hop = True
        
        d = self.manager.client_f.send_command({"command": "freq_sel_hop"})
        
        if d is None:
            log.msg("ERROR: send_command returned None, connection not ready")
            self._is_scheduled_hop = False  # Сбрасываем флаг если команда не отправилась
            return
        
        d.addCallback(self._on_hop_response)
        d.addErrback(self._on_hop_error)

    def _on_hop_response(self, res):
        log.msg(f"[HOP RESPONSE] Received: {res}")
        action_time = res.get("time")
        if action_time:
            # Планируем HOP с полученным временем 
            delay = max(0, action_time - time.time())
            task.deferLater(reactor, delay, self.do_hop)
            
            log.msg(f"Frequency hop scheduled to execute in {delay:.2f} seconds")
        else:
            log.msg("ERROR: No 'time' in hop response, cannot schedule")
            # Защита: сбрасываем флаг если не получили время для планирования
            self._is_scheduled_hop = False
    
    def _on_hop_error(self, err):
        log.msg(f"[HOP ERROR] Failed to send hop command: {err}")
        # Защита: сбрасываем флаг при ошибке, чтобы разрешить следующую попытку
        self._is_scheduled_hop = False

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
        """Выполнение переключения канала с проверками безопасности"""
        target_channel = channel if channel else self.channels.next_channel()

        # Защита 1: проверка валидности целевого канала
        if not target_channel or not isinstance(target_channel, Channel):
            log.msg("ERROR: Invalid target_channel in do_hop, aborting")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        # Защита 2: пропуск если уже на целевом канале (избегаем бесполезных переключений)
        if target_channel.freq() == self.channels.current().freq():
            log.msg("Already on the selected channel, skipping hop.")
            self._is_scheduled_hop = False
            self._is_scheduled_recovery_hop = False
            return

        # Защита 3: отмена recovery hop если канал восстановился
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
        self._is_scheduled_recovery_hop = True
        channel = self.channels.reserve()
        if self.channels.current().freq() == channel.freq():
            log.msg(f"Already on reserve channel {channel.freq()}, recovery hop not needed")
            return

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
            channel._last_packet_time = 0
            channel._score = [100]
        
        self._is_scheduled_hop = False
        self._is_scheduled_recovery_hop = False

        self._last_hop_time = time.time()