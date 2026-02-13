"""
Frequency Selection — каналы, фабрика каналов, score/статистика по каналам.
Переключение каналов (хопы) отключено по умолчанию.
"""
import time
from twisted.python import log
from twisted.internet import reactor, task, defer

from ... import call_and_check_rc
from ...conf import settings
from ..core.data import (
    Utils,
    MeasurementStats,
    ChannelMeasurements,
    calculate_rssi,
    calculate_per,
    calculate_snr,
    format_channel_freq,
)

SCORE_FRAMES = 3
SCORE_PER_WEIGHT = 75
SCORE_SNR_WEIGHT = 25
SCORE_PER_MAX_PENALTY = 10
SCORE_SNR_MIN_THRESHOLD = 20
CHANNEL_KEEP_HISTORY = 5



class Channel:
    """Одна частота: измерения (RSSI, PER, SNR), score, callback при обновлении. Не знает про другие каналы."""
    def __init__(self, freq):
        self._freq = freq
        self._score = [100]
        self._measurements = ChannelMeasurements()
        self._last_packet_time = 0
        self._switched_at = time.time()
        self._on_score_updated = None

    def _update_score(self):
        rssi = calculate_rssi(self._measurements)
        per = calculate_per(self._measurements, SCORE_FRAMES)
        snr = calculate_snr(self._measurements, SCORE_FRAMES)
        pen_per = SCORE_PER_WEIGHT * Utils.clamp(per / SCORE_PER_MAX_PENALTY, 0.0, 1.0)
        pen_snr = SCORE_SNR_WEIGHT * Utils.clamp((SCORE_SNR_MIN_THRESHOLD - snr) / SCORE_SNR_MIN_THRESHOLD, 0.0, 1.0)
        score = 100 - (pen_per + pen_snr)
        self._score.append(score)
        rssi_str = f"{rssi} dBm" if rssi is not None else "N/A"
        ch_str = format_channel_freq(self._freq)
        log.msg(f"Channel {ch_str} - RSSI: {rssi_str}, PER: {per}%, SNR: {snr:.2f} dB, Score: {score:.2f}")
        if self._on_score_updated:
            self._on_score_updated(self)

    @property
    def freq(self):
        return self._freq

    @property
    def score(self):
        return self._score[-1] if self._score else 100

    def add_measurement(self, rx_id, stats):
        if not self._measurements.has(rx_id):
            return
        if stats.p_total > 0:
            self._last_packet_time = time.time()
        self._measurements.append(rx_id, stats)
        lengths = {k: len(v) for k, v in self._measurements.items()}
        if len(set(lengths.values())) == 1:
            self._update_score()

    def set_on_score_updated(self, callback):
        self._on_score_updated = callback

    def clear_measurements(self):
        for stream in [self._measurements.video, self._measurements.mavlink, self._measurements.tunnel]:
            if len(stream) > CHANNEL_KEEP_HISTORY:
                stream[:] = stream[-CHANNEL_KEEP_HISTORY:]
        if len(self._score) > CHANNEL_KEEP_HISTORY:
            self._score = self._score[-CHANNEL_KEEP_HISTORY:]
        self._switched_at = time.time()

class ChannelsFactory:
    """Создание набора Channel по списку частот и «найти или создать» канал по одной частоте (get_single_freq)."""
    def __init__(self, channels):
        self.channels = channels

    @property
    def as_freq(self):
        return {chan.freq: chan for chan in self.channels}

    @classmethod
    def create(cls, freqs) -> "ChannelsFactory":
        """freqs — список частот (freq_sel_frequencies); для каждой создаётся Channel(freq)."""
        return cls(channels=[Channel(freq) for freq in freqs])

    def get_single_freq(self, value):
        """Вернуть Channel для частоты value: если есть — его, иначе создать и добавить. value может быть числом или dict (per-wlan из конфига)."""
        if isinstance(value, dict):
            value = next(iter(value.values()))
        if value in self.as_freq:
            rec = self.as_freq[value]
        else:
            rec = Channel(value)          # создание Channel здесь, если частоты ещё не было в списке
            self.channels.append(rec)
        return rec

class Channels:
    """
    1) Один канал wifi_channel = старт и резерв (_startup и _reserve — один и тот же Channel).
    2) Список _list = только freq_sel, порядок из конфига; по нему прыгают HopLocalOnly / HopScheduledGS2Drone.
    3) Центральная логика каналов: next_channel(), prev_channel(), by_freq(), first_freq_sel_channel, last_freq_sel_channel.
       Хопы и приложение вызывают только эти методы — без дублирования логики.
    """
    def __init__(self, frequency_selection, wifi_channel_freq, reserve_freq, freq_sel_frequencies):
        self.frequency_selection = frequency_selection
        chan_factory = ChannelsFactory.create(freq_sel_frequencies)
        # Один канал для старта и резерва (wifi_channel из конфига)
        self._startup = chan_factory.get_single_freq(wifi_channel_freq)
        self._reserve = chan_factory.get_single_freq(reserve_freq)
        # Список только для прыжков: строго freq_sel, в порядке конфига (без лишних добавлений)
        self._list = [chan_factory.get_single_freq(f) for f in freq_sel_frequencies]
        self._current_channel = self._startup
        self._index = 0
        self._startup.set_on_score_updated(self._on_channel_score_updated)
        self._reserve.set_on_score_updated(self._on_channel_score_updated)
        for chan in self._list:
            chan.set_on_score_updated(self._on_channel_score_updated)

    def _on_channel_score_updated(self, channel):
        self.frequency_selection._on_channel_score_updated(channel)

    def on_stats_received(self, rx_id, stats_dict):
        stats = MeasurementStats(
            p_total=stats_dict['p_total'],
            p_bad=stats_dict['p_bad'],
            rssi=stats_dict['rssi'],
            snr=stats_dict['snr']
        )
        self.current.add_measurement(rx_id, stats)

    @property
    def count(self):
        return len(self._list)

    @property
    def all(self):
        """Все уникальные каналы: старт (он же резерв) + список freq_sel для прыжков."""
        return [self._startup] + self._list

    @property
    def current(self):
        return self._current_channel

    def _index_of(self, channel):
        for i, c in enumerate(self._list):
            if c is channel:
                return i
        return None

    def next_channel(self):
        """Следующий канал в freq_sel (циклично). Центральная точка — используйте отсюда."""
        if not self._list:
            return None
        idx = self._index_of(self._current_channel)
        if idx is None:
            return self._list[0]
        return self._list[(idx + 1) % len(self._list)]

    def prev_channel(self):
        """Предыдущий канал в freq_sel (циклично). Центральная точка — используйте отсюда."""
        if not self._list:
            return None
        idx = self._index_of(self._current_channel)
        if idx is None:
            return self._list[0]
        return self._list[(idx - 1) % len(self._list)]

    @property
    def first_freq_sel_channel(self):
        return self._list[0] if self._list else None

    @property
    def last_freq_sel_channel(self):
        return self._list[-1] if self._list else None

    @property
    def reserve(self):
        return self._reserve

    def set_current(self, channel):
        if isinstance(channel, Channel):
            self._current_channel = channel
            idx = self._index_of(channel)
            self._index = idx if idx is not None else 0
            return
        freq = channel
        ch = self.by_freq(freq)
        if ch is not None:
            self._current_channel = ch
            idx = self._index_of(ch)
            self._index = idx if idx is not None else 0

    def by_freq(self, freq):
        if self._startup.freq == freq:
            return self._startup
        if self._reserve.freq == freq:
            return self._reserve
        for chan in self._list:
            if chan.freq == freq:
                return chan
        return None

    @property
    def is_on_freq_sel(self):
        """True, если текущий канал в списке freq_sel."""
        return self._index_of(self._current_channel) is not None


# ==================== Контуры переключения частоты (hop) ====================
# Низкий уровень: switch_wifiradio_to_channel (используется обоими контурами)
@defer.inlineCallbacks
def switch_wifiradio_to_channel(manager, channels, target_channel):
    if target_channel is None:
        return

    current = channels.current
    target_freq = target_channel.freq
    current_freq = current.freq

    if target_freq == current_freq:
        return
    try:
        for wlan in manager.wlans:
            yield call_and_check_rc(
                "iw", "dev", wlan, "set",
                "freq" if target_freq > 2000 else "channel",
                str(target_freq),
            )
    except Exception as e:
        log.msg(f"[HOP FAILED] {e}")
        raise

    channels.set_current(target_channel)
    if hasattr(manager, "metrics_manager") and manager.metrics_manager:
        manager.metrics_manager.set_current_freq(target_freq)
    if hasattr(target_channel, "clear_measurements"):
        target_channel.clear_measurements()
    if hasattr(target_channel, "_switched_at"):
        target_channel._switched_at = time.time()

    log.msg(f"[HOP SUCCESS] Now on {format_channel_freq(target_freq)}")

# -------------------
# 1) Только локально — команда на другую сторону не отправляется
# -------------------
class HopLocalOnly:
    """
    Переключение радио только на этой машине. Команда на дрон/GS не отправляется.
    Использование: на GS или на дроне, когда нужно переключить своё радио без согласования со второй стороной.
    Методы: to_first(), to_last(), to_next(), to_prev().
    """

    def __init__(self, manager, channels):
        self.manager = manager
        self.channels = channels

    def _switch_to(self, target_channel, delay=0):
        if target_channel is None:
            return None
        if self.channels.current.freq == target_channel.freq:
            return None

        def run_hop():
            return switch_wifiradio_to_channel(self.manager, self.channels, target_channel)

        if delay <= 0:
            return run_hop()
        d = task.deferLater(reactor, delay, None)
        d.addCallback(lambda _: run_hop())
        return d

    def to_first(self, delay=0):
        """Локальный хоп на первый канал из freq_sel."""
        return self._switch_to(self.channels.first_freq_sel_channel, delay=delay)

    def to_last(self, delay=0):
        """Локальный хоп на последний канал из freq_sel."""
        return self._switch_to(self.channels.last_freq_sel_channel, delay=delay)

    def to_next(self, delay=0):
        """Локальный хоп на следующий канал в списке freq_sel."""
        return self._switch_to(self.channels.next_channel(), delay=delay)

    def to_prev(self, delay=0):
        """Локальный хоп на предыдущий канал в списке freq_sel."""
        return self._switch_to(self.channels.prev_channel(), delay=delay)

    def to_wifi_channel(self, delay=0):
        """Локальный хоп на wifi_channel (старт/резерв из конфига)."""
        return self._switch_to(self.channels.reserve, delay=delay)

# -------------------
# 2) Запланированный хоп GS → дрон (выполнение на дроне в action_time)
# -------------------
class HopScheduledGS2Drone:
    """
    Запланированный хоп от Ground Station к дрону. Используется на дроне при приёме команды freq_sel_hop:
    GS отправляет action_time (и опционально target_freq), дрон выполняет переключение радио в этот момент.
    Метод: .schedule(action_time, target_freq=None).
    """

    def __init__(self, manager, channels):
        self.manager = manager
        self.channels = channels

    def schedule(self, action_time, target_freq=None):
        """
        Запланировать переключение радио на момент action_time.
        target_freq: частота в MHz или None (тогда channels.next_channel()).
        """
        if target_freq is not None:
            target = self.channels.by_freq(target_freq)
            if not target:
                log.msg(f"[FS] HopScheduledGS2Drone: unknown target_freq {target_freq}")
                return action_time
        else:
            target = self.channels.next_channel()
        if not target:
            log.msg("[FS] HopScheduledGS2Drone: no target channel")
            return action_time

        delay = max(0.0, action_time - time.time())
        now = time.time()
        if action_time < now - 0.5:
            log.msg(f"[FS] WARNING: action_time in the past (skew {now - action_time:.1f}s), hop immediately.")
        elif delay > 4.0:
            log.msg(f"[FS] WARNING: hop delay {delay:.1f}s (clock skew?). Use NTP.")

        log.msg(f"[FS] Scheduled hop (GS→drone) to {format_channel_freq(target.freq)} in {delay:.2f}s")
        d = task.deferLater(reactor, delay, None)
        d.addCallback(lambda _: switch_wifiradio_to_channel(self.manager, self.channels, target))
        return action_time


# ==================== FrequencySelection ====================


class FrequencySelection:
    """
    Каналы, score, статистика по каналам. Держит список каналов и состояние (текущий канал, резерв).
    Переключение радио не вызывает — для хопов используйте снаружи:
      HopLocalOnly(manager, self.channels) — только локально;
      HopScheduledGS2Drone(manager, self.channels) — запланированный хоп GS→дрон.
    """

    def __init__(self, manager):
        self.manager = manager
        self.enabled = settings.common.freq_sel_enabled
        wifi_channel = settings.common.wifi_channel
        freq_sel_channels = list(settings.common.freq_sel_channels)
        self.channels = Channels(self, wifi_channel, wifi_channel, freq_sel_channels)
        self.hop_local = HopLocalOnly(manager, self.channels)
        log.msg(f"[FS] Initialized (hops disabled). Channel: {format_channel_freq(self.channels.current.freq)}")

    def is_enabled(self):
        return self.enabled and self.channels.count > 1

    # заглушка на будущее
    # def is_ready_for_hop(self):
    #     """Статус разрешает хоп (connected/armed/disarmed). Для использования с HopLocalOnly / HopScheduledGS2Drone."""
    #     if not hasattr(self.manager, 'status_manager') or not self.manager.status_manager:
    #         return False
    #     status = self.manager.status_manager.get_status()
    #     return status in ["connected", "armed", "disarmed"]
    # заглушка на будущее

    def reset_all_channels_stats(self):
        log.msg("[FS] Resetting all channel statistics")
        for channel in self.channels.all:
            channel._measurements.clear()
            channel._last_packet_time = 0
            channel._score = [100]

    def _on_channel_score_updated(self, channel):
        pass