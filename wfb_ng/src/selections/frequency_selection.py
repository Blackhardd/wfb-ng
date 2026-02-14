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

# Авто-хоп по PER: только при PER в этом диапазоне и только в connected/armed/disarmed
PER_HOP_MIN = 40
PER_HOP_MAX = 80
PER_HOP_COOLDOWN_SEC = 15

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
        if self._on_score_updated:
            self._on_score_updated(self, per=per)

    def get_stats_for_log(self):
        """Текущие rssi, per, snr, score для лога (без изменения состояния)."""
        rssi = calculate_rssi(self._measurements)
        per = calculate_per(self._measurements, SCORE_FRAMES)
        snr = calculate_snr(self._measurements, SCORE_FRAMES)
        pen_per = SCORE_PER_WEIGHT * Utils.clamp(per / SCORE_PER_MAX_PENALTY, 0.0, 1.0)
        pen_snr = SCORE_SNR_WEIGHT * Utils.clamp((SCORE_SNR_MIN_THRESHOLD - snr) / SCORE_SNR_MIN_THRESHOLD, 0.0, 1.0)
        score = 100 - (pen_per + pen_snr)
        return rssi, per, snr, score

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
        # Обновлять score когда есть достаточно данных для расчёта PER (SCORE_FRAMES).

        lengths = [len(v) for v in self._measurements.values() if len(v) > 0]
        if lengths and min(lengths) >= SCORE_FRAMES:
            self._update_score()

    def set_on_score_updated(self, callback):
        self._on_score_updated = callback


    #
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

    def _on_channel_score_updated(self, channel, per=None):
        self.frequency_selection._on_channel_score_updated(channel, per=per)

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
# 2) Запланированный хоп GS -> дрон (выполнение на дроне в action_time)
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
        target_freq: частота в MHz или None — тогда: если на wifi_channel -> первый из freq_sel,
        иначе следующий канал в списке freq_sel.
        """
        if target_freq is not None:
            target = self.channels.by_freq(target_freq)
            if not target:
                log.msg(f"[FS] HopScheduledGS2Drone: unknown target_freq {target_freq}")
                return action_time
        else:
            # На wifi_channel -> первый из freq_sel, иначе следующий в списке
            if self.channels.current.freq == self.channels.reserve.freq:
                target = self.channels.first_freq_sel_channel
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

        log.msg(f"[FS] Scheduled hop (GS->drone) to {format_channel_freq(target.freq)} in {delay:.2f}s")
        d = task.deferLater(reactor, delay, None)
        d.addCallback(lambda _: switch_wifiradio_to_channel(self.manager, self.channels, target))
        return action_time


# ==================== FrequencySelection ====================


class FrequencySelection:
    """
    Каналы, score, статистика по каналам. Держит список каналов и состояние (текущий канал, резерв).
    Переключение радио не вызывает — для хопов используйте снаружи:
      HopLocalOnly(manager, self.channels) — только локально;
      HopScheduledGS2Drone(manager, self.channels) — запланированный хоп GS->дрон.
    """

    def __init__(self, manager):
        self.manager = manager
        self.enabled = settings.common.freq_sel_enabled
        wifi_channel = settings.common.wifi_channel
        freq_sel_channels = list(settings.common.freq_sel_channels)
        self.channels = Channels(self, wifi_channel, wifi_channel, freq_sel_channels)
        self.hop_local = HopLocalOnly(manager, self.channels)
        self.hop_at_time = HopScheduledGS2Drone(manager, self.channels)
        # Только ссылки на текущие Deferred (не флаги). Очищаются при завершении/отмене — после
        # восстановления в connected/armed/disarmed новые запланированные хопы запускаются как обычно.
        self._pending_hop_request_d = None   # Deferred от request_hop() (ожидание ответа от дрона)
        self._pending_scheduled_hop_d = None  # Deferred от hop_at_drone_time (deferLater)
        # Лог канала раз в секунду и на ГС, и на дроне (на дроне stats могут приходить реже — лог не зависел от них)
        self._channel_log_task = task.LoopingCall(self._log_current_channel_once)
        self._channel_log_task.start(1.0)
        log.msg(f"[FS] Initialized (hops disabled). Channel: {format_channel_freq(self.channels.current.freq)}")

    def is_enabled(self):
        return self.enabled and self.channels.count > 1

    def _log_current_channel_once(self):
        """Раз в секунду вывести в лог канал, RSSI, PER, SNR, Score (одинаково на ГС и дроне)."""
        ch = self.channels.current
        rssi, per, snr, score = ch.get_stats_for_log()
        rssi_str = f"{rssi} dBm" if rssi is not None else "N/A"
        ch_str = format_channel_freq(ch.freq)
        log.msg(f"Канал:{ch_str} - RSSI: {rssi_str}, PER: {per}%, SNR: {snr:.2f} dB, Score: {score:.2f}")

    def reset_all_channels_stats(self):
        log.msg("[FS] Resetting all channel statistics")
        for channel in self.channels.all:
            channel._measurements.clear()
            channel._last_packet_time = 0
            channel._score = [100]

    # ------------------- Запланированный синхронный хоп GS ↔ дрон -------------------
    # ГС: request_hop() -> команда дрону. Дрон: handle_hop_command() (из manager) -> время в ответ, свой хоп. ГС: hop_at_drone_time(time).

    def get_action_time(self, interval=1.0):
        """Время для синхронного хопа (через interval секунд). Используется дроном при ответе на freq_sel_hop."""
        return time.time() + interval

    def handle_hop_command(self):
        """
        Дрон: при приёме freq_sel_hop — считает время хопа, планирует свой хоп, возвращает ответ для ГС.
        """
        if not self.is_enabled():
            return {"status": "error", "error": "freq_sel disabled or single channel"}
        action_time = self.get_action_time()
        self.hop_at_time.schedule(action_time, target_freq=None)
        log.msg(f"[FS] handle_hop_command: hop at {action_time:.2f}")
        return {"status": "success", "time": action_time}

    def hop_at_drone_time(self, action_time):
        """
        ГС: запланировать свой хоп на момент action_time (время от дрона).
        Вызывается после получения ответа с полем "time".
        Цель: если на wifi_channel — первый из freq_sel, иначе следующий канал.
        """
        def _run_hop():
            # если не channels.current.freq не равен channels.reserve.freq тогда хоп на следующий канал
            if self.channels.current.freq == self.channels.reserve.freq:
                target = self.channels.first_freq_sel_channel
            else:
                target = self.channels.next_channel()
            if target is None or target.freq == self.channels.current.freq:
                log.msg("[FS] hop_at_drone_time: skip (on target or no next)")
                return
            return switch_wifiradio_to_channel(self.manager, self.channels, target)

        delay = max(0.0, action_time - time.time())
        log.msg(f"[FS] hop_at_drone_time: hop in {delay:.2f}s")
        d = task.deferLater(reactor, delay, _run_hop)
        self._pending_scheduled_hop_d = d

        def _clear_pending_scheduled(_):
            self._pending_scheduled_hop_d = None
            return _

        d.addBoth(_clear_pending_scheduled)
        return d

    def request_hop(self):
        """
        ГС: отправить команду дрону (по исходящему или входящему соединению), по ответу вызвать hop_at_drone_time(time).
        Returns: Deferred. На ГС использует send_command_to_drone, если есть.
        """
        if not self.is_enabled():
            return defer.fail(Exception("freq_sel disabled or single channel"))
        cmd = {"command": "freq_sel_hop"}
        if hasattr(self.manager, "send_command_to_drone"):
            d = self.manager.send_command_to_drone(cmd)
        elif hasattr(self.manager, "client_f") and self.manager.client_f is not None:
            d = self.manager.client_f.send_command(cmd)
        else:
            return defer.fail(Exception("no way to send command to drone (no send_command_to_drone, no client_f)"))
        if d is None:
            return defer.fail(Exception("send_command returned None, connection not ready"))

        def on_response(res):
            action_time = res.get("time")
            if action_time is None:
                raise ValueError("No 'time' in hop response")
            return self.hop_at_drone_time(action_time)

        d.addCallback(on_response)
        return d

    def cancel_pending_scheduled_hop(self):
        """
        Отменить запланированный PER-based хоп (request_hop / hop_at_drone_time).
        Вызывается при входе в lost, чтобы выполнялся только локальный авто-хоп на первый канал.
        """
        cancelled = False
        for name in ("_pending_scheduled_hop_d", "_pending_hop_request_d"):
            pending = getattr(self, name, None)
            if pending is not None:
                try:
                    pending.cancel()
                except Exception:
                    pass
                setattr(self, name, None)
                cancelled = True
        if cancelled:
            log.msg("[FS] Отменён запланированный PER-хоп (приоритет — локальный хоп в lost)")

    def _on_channel_score_updated(self, channel, per=None):
        """
        При обновлении score канала: если PER в диапазоне [40, 80]% и статус
        connected/armed/disarmed — инициировать запланированный хоп (только на ГС).
        lost и recovery исключены: хопы по PER не выполняются.
        per передаётся из Channel._update_score() — тот же PER, что выведен в лог (нет разбежки).
        """
        if not self.is_enabled():
            return
        # Только для текущего канала и только в допустимых статусах
        # делаю проверки IF NOT что бы исключить нежелательный исход запуска 
        sm = getattr(self.manager, "status_manager", None)
        if not sm:
            return
        status = sm.get_status()
        if status not in ("connected", "armed", "disarmed"):
            return
        if channel is not self.channels.current:
            return
        # делаю проверки IF NOT что бы исключить нежелательный исход запуска

        if per is None:
            per = calculate_per(channel._measurements, SCORE_FRAMES)
        if per < PER_HOP_MIN or per > PER_HOP_MAX:
            return

        now = time.time()
        last = getattr(self, "_last_per_hop_time", None)
        if last is not None and (now - last) < PER_HOP_COOLDOWN_SEC:
            return

        # Инициировать запланированный PER-хоп может только ГС (у ГС есть send_command_to_drone).
        # На дроне этого метода нет — не вызываем request_hop() на дроне.
        if not hasattr(self.manager, "send_command_to_drone"):
            return

        self._last_per_hop_time = now
        log.msg(f"[FS] PER {per}% in [{PER_HOP_MIN},{PER_HOP_MAX}]%, status={status} -> scheduled hop")
        d = self.request_hop()
        if d is not None:
            self._pending_hop_request_d = d

            def _clear_pending_request(r):
                self._pending_hop_request_d = None
                return r

            d.addBoth(_clear_pending_request)
            d.addErrback(lambda err: log.msg(f"[FS] PER-based hop failed: {err}"))