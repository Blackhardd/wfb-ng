"""
Frequency Selection — только модуль выбора и переключения частотного канала.
Патерн: State Pattern
Приемущества: лучшая структура, расширяемость, легкость поддержки.

"""

import time
from abc import ABC, abstractmethod
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


# ==================== НАСТРОЙКИ () ====================

SCORE_FRAMES = 3                # Количество фреймов для расчета метрик
# формула расчета score: 100 - (pen_per + pen_snr)
SCORE_PER_WEIGHT = 75           # Вес PER для расчета score
SCORE_SNR_WEIGHT = 25           # Вес SNR для расчета score
SCORE_PER_MAX_PENALTY = 10      # Максимальное значение PER для штрафа
SCORE_SNR_MIN_THRESHOLD = 20    # Минимальное значение SNR для штрафа
# Формула расчета score: 100 - (pen_per + pen_snr)

# настройки хоп-системы (переключения канала)
HOP_PER_THRESHOLD = 20          # Порог PER для перехода на другой канал
HOP_SCORE_THRESHOLD = 50        # Порог score для перехода на другой канал
HOP_TIMEOUT_SECONDS = 30        # Таймаут перехода на другой канал
HOP_TIMEOUT_SCORE = 80          # Порог score для перехода на другой канал
HOP_MIN_INTERVAL = 5.0          # Минимальное время и интервал на канале перед hop и между хопами
# настройки хоп-системы (переключения канала)

LINK_LOSS_NO_HOP_SEC = 1.0      # Защита от спама при потере связи
CHANNEL_KEEP_HISTORY = 5        # Количество измерений для хранения в истории


# ==================== Channel ====================

class Channel:
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

    def freq(self):
        return self._freq

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
    @classmethod
    def create(cls, freqs):
        return [Channel(freq) for freq in freqs]


def _single_freq(value):
    """Один канал из конфига: число как есть, для dict — первое значение (per-wlan)."""
    if isinstance(value, dict):
        return next(iter(value.values()))
    return value


class Channels:
    """
    Логика жёсткая: startup (wifi_channel), reserve (wifi_recovery), список freq_sel.
    Текущий канал хранится напрямую в _current_channel; _index только для next_channel() по списку.
    """

    ROLE_STARTUP = "startup"
    ROLE_RECOVERY = "recovery"
    ROLE_FREQ_SEL = "freq_sel"

    def __init__(self, frequency_selection, wifi_channel_freq, wifi_recovery_freq, freq_sel_frequencies):
        self.frequency_selection = frequency_selection
        self._startup = Channel(_single_freq(wifi_channel_freq))
        self._reserve = Channel(_single_freq(wifi_recovery_freq))
        self._list = ChannelsFactory.create(freq_sel_frequencies)
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
        self.current().add_measurement(rx_id, stats)

    def count(self):
        return len(self._list)

    def all(self):
        return [self._startup, self._reserve] + self._list

    def current(self):
        return self._current_channel

    def _index_of(self, channel):
        """Индекс канала в _list или None."""
        for i, c in enumerate(self._list):
            if c is channel:
                return i
        return None

    def next_channel(self):
        if not self._list:
            return None
        idx = self._index_of(self._current_channel)
        if idx is None:
            return self._list[0]
        next_index = (idx + 1) % len(self._list)
        return self._list[next_index]

    def first_freq_sel_channel(self):
        return self._list[0] if self._list else None

    def last_freq_sel_channel(self):
        return self._list[-1] if self._list else None

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
        if self._startup.freq() == freq:
            return self._startup
        if self._reserve.freq() == freq:
            return self._reserve
        for chan in self._list:
            if chan.freq() == freq:
                return chan
        return None

    def is_on_freq_sel(self):
        """Сейчас на канале из рабочего списка freq_sel."""
        return self._index_of(self._current_channel) is not None


# ==================== Состояния FrequencySelection ====================

class FrequencySelectionState(ABC):
    def __init__(self, fs):
        self.fs = fs

    @abstractmethod
    def name(self):
        pass

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def on_recovery_needed(self):
        pass

    def on_status_changed(self, old_status, new_status):
        pass

    def on_stats_received(self, rx_id, stats_dict):
        pass

    def on_channel_score_updated(self, channel):
        pass

    def hop(self, reason=None):
        pass


class ConnectionModeState(FrequencySelectionState):
    """
    Режим первого подключения: один hop на первый канал freq_sel. Дальше — normal.
    Recovery не обрабатываем здесь — при переходе SM в recovery перейдём в RecoveryModeState и hop сделает он.
    """

    def name(self):
        return "connection"

    def on_enter(self):
        log.msg("[FS] Entered CONNECTION mode (hop to first freq_sel channel)")
        self.fs.reset_all_channels_stats()
        target = self.fs.channels.first_freq_sel_channel()
        if target and self.fs.channels.current().freq() != target.freq():
            delay = RECOVERY_TO_CONNECTION_HOP_DELAY if self.fs._connection_after_recovery else 1.0
            self.fs._connection_after_recovery = False
            action_time = self.fs.get_action_time(delay)
            self.fs._schedule_hop_to(target, is_recovery=False, action_time=action_time)
            if delay > 1.0:
                log.msg(f"[FS] Recovery->connection: hop in {delay:.1f}s (stabilization)")
        else:
            self.fs._connection_after_recovery = False
            if target is None:
                log.msg("[FS] No freq_sel channels, staying on startup channel")
            else:
                log.msg("[FS] Already on first freq_sel channel")
            self.fs._transition_to("normal")


class NormalOperationState(FrequencySelectionState):
    """Обычный режим работы (Connected / Armed / Disarmed)."""

    def name(self):
        return "normal"

    def on_channel_score_updated(self, channel):
        if not self.fs.is_ready_for_hop():
            return
        if time.time() - channel._switched_at < HOP_MIN_INTERVAL:
            return
        if self.fs._should_skip_hop_due_to_link_loss(channel):
            return

        reason = self.fs._get_normal_hop_reason(channel)
        if reason:
            self.fs.hop(reason=reason)

    def hop(self, reason=None):
        """Запланировать переход на следующий канал (реальное переключение)."""
        if self.fs._is_scheduled_hop:
            return
        target = self.fs.channels.next_channel()
        if not target:
            return
        current_freq = self.fs.channels.current().freq()
        target_freq = target.freq()
        if target_freq == current_freq:
            log.msg(
                f"[FS] Hop not performed: target same as current ({format_channel_freq(current_freq)}). "
                "Check freq_sel_channels — need at least 2 different frequencies."
            )
            return
        if reason:
            log.msg(f"[FS] {reason} -> hop")
        self.fs._schedule_hop_to(target)


class RecoveryModeState(FrequencySelectionState):
    """
    Режим восстановления связи. Hop на wifi_recovery (reserve) — так же делает и дрон,
    обе стороны снова на одном канале.
    """

    def name(self):
        return "recovery"

    def on_enter(self):
        log.msg("[FS] Entered RECOVERY mode")
        self.fs.reset_all_channels_stats()

    def on_recovery_needed(self):
        if self.fs.is_already_on_recovery():
            log.msg("[FS] Already on wifi_recovery in recovery mode")
            return
        reserve = self.fs.channels.reserve()
        log.msg(f"[FS] Recovery hop -> wifi_recovery ({format_channel_freq(reserve.freq())})")
        self.fs._schedule_hop_to(reserve, is_recovery=True)


class DisabledState(FrequencySelectionState):
    """Частотный выбор выключен"""

    def name(self):
        return "disabled"

    def hop(self, reason=None):
        log.msg("[FS] Hop requested but frequency selection is disabled")


# ==================== Основной класс ====================

class FrequencySelection:
    def __init__(self, manager):
        self.manager = manager
        self.enabled = settings.common.freq_sel_enabled

        wifi_channel = settings.common.wifi_channel
        wifi_recovery = getattr(settings.common, 'wifi_recovery', wifi_channel)  # отдельный канал для recovery
        freq_sel_channels = list(settings.common.freq_sel_channels)
        self.channels = Channels(self, wifi_channel, wifi_recovery, freq_sel_channels)

        self._last_hop_time = time.time()
        self._is_scheduled_hop = False

        # Состояния
        self._states = {
            "normal": NormalOperationState(self),
            "connection": ConnectionModeState(self),
            "recovery": RecoveryModeState(self),
            "disabled": DisabledState(self),
        }

        self._current_state = None

        # Начальное состояние
        if self.is_enabled():
            self._transition_to("normal")
        else:
            self._transition_to("disabled")

        log.msg(f"[FS] Initialized. Startup channel: {format_channel_freq(self.channels.current().freq())}")

    def _transition_to(self, state_name):
        if state_name not in self._states:
            log.msg(f"[FS] Unknown state: {state_name}")
            return

        new_state = self._states[state_name]

        if self._current_state:
            old_name = self._current_state.name()
            self._current_state.on_exit()
        else:
            old_name = "none"

        self._current_state = new_state
        self._current_state.on_enter()

        log.msg(f"[FS] State changed: {old_name} -> {state_name}")

    # ─── Внешний публичный интерфейс (сохранён полностью) ───────

    def is_enabled(self):
        return self.enabled and self.channels.count() > 1

    def subscribe_to_status_events(self, status_manager):
        if not status_manager:
            return

        status_manager.subscribe("recovery_needed", self._on_recovery_needed)
        status_manager.subscribe("status_changed", self._on_status_changed)
        log.msg("[FS] Subscribed to StatusManager events")

    def on_stats_received(self, rx_id, stats_dict):
        if self._current_state:
            self._current_state.on_stats_received(rx_id, stats_dict)
        self.channels.on_stats_received(rx_id, stats_dict)

    def hop(self, reason=None):
        if self._current_state:
            self._current_state.hop(reason=reason)

    def is_ready_for_hop(self):
        if not hasattr(self.manager, 'status_manager') or not self.manager.status_manager:
            return False

        status = self.manager.status_manager.get_status()
        # Hop разрешён при любом активном линке: connected, armed, disarmed
        return status in ["connected", "armed", "disarmed"]

    def is_hop_timed_out(self, timeout):
        return time.time() - self._last_hop_time >= timeout

    def is_already_on_recovery(self):
        """Уже на канале wifi_recovery (reserve)."""
        return self.channels.current() is self.channels.reserve()

    def _should_skip_hop_due_to_link_loss(self, channel):
        """Не делать hop при подозрении на потерю связи (ждём lost -> recovery)."""
        sm = getattr(self.manager, "status_manager", None)
        if sm is None:
            return False
        time_since = sm.get_time_since_last_packet()
        if time_since is None or time_since <= LINK_LOSS_NO_HOP_SEC:
            return False
        per = calculate_per(channel._measurements, SCORE_FRAMES)
        if per >= HOP_PER_THRESHOLD or channel.score() < HOP_SCORE_THRESHOLD:
            log.msg(f"[FS] No packet for {time_since:.1f}s (link loss?) -> skip hop, wait for lost/recovery")
            return True
        return False

    def _get_normal_hop_reason(self, channel):
        """Причина для normal hop или None, если hop не нужен."""
        per = calculate_per(channel._measurements, SCORE_FRAMES)
        if per >= HOP_PER_THRESHOLD:
            return f"PER {per}% >= {HOP_PER_THRESHOLD}%"
        if channel.score() < HOP_SCORE_THRESHOLD:
            return f"Score {channel.score():.1f} < {HOP_SCORE_THRESHOLD}"
        if self.is_hop_timed_out(HOP_TIMEOUT_SECONDS) and channel.score() < HOP_TIMEOUT_SCORE:
            return f"Timeout {HOP_TIMEOUT_SECONDS}s + low score"
        return None

    # ─── Внутренние обработчики событий ──────────────────────────

    def _on_recovery_needed(self):
        if self._current_state:
            self._current_state.on_recovery_needed()

    # Таблица переходов FS по статусу SM (только при включённом freq_sel)
    _STATUS_TO_STATE = {
        "recovery": "recovery",
        "connected": "connection",
        "armed": "normal",
        "disarmed": "normal",
    }

    def _on_status_changed(self, old_status, new_status):
        if self._current_state:
            self._current_state.on_status_changed(old_status, new_status)

        if not self.is_enabled():
            return

        new_state = self._STATUS_TO_STATE.get(new_status)
        if new_state == "normal" and self._current_state and self._current_state.name() == "connection":
            return  # из connection не выходим по armed/disarmed — только после завершения hop
        if new_state:
            self._transition_to(new_state)

        if new_status == "lost" and self._current_state and self._current_state.name() == "normal":
            self._try_lost_hop(old_status)

    def _on_channel_score_updated(self, channel):
        if self._current_state:
            self._current_state.on_channel_score_updated(channel)

    def _try_lost_hop(self, old_status):
        """
        При переходе в lost из armed: если мы на первом канале freq_sel — прыжок на последний,
        если на последнем — на первый. Попытка восстановить связь; каждая сторона (GS/дрон) делает
        это локально (команду не шлём — линк потерян). Если не выйдет — через 10 с SM перейдёт в recovery.
        """
        if old_status != "armed":
            return
        if not self.channels._list or len(self.channels._list) < 2:
            return
        if not self.channels.is_on_freq_sel():
            return
        current = self.channels.current()
        first_ch = self.channels.first_freq_sel_channel()
        last_ch = self.channels.last_freq_sel_channel()
        if current is first_ch:
            target = last_ch
        elif current is last_ch:
            target = first_ch
        else:
            return
        log.msg(f"[FS] Lost (from armed): hop to opposite end of freq_sel -> {format_channel_freq(target.freq())}")
        self._schedule_hop_to(target, is_recovery=False, action_time=None, send_to_drone=False)

    # ─── Планирование и выполнение hop ───────────────────────────

    def _schedule_hop_to(self, target_channel, is_recovery=False, action_time=None, send_to_drone=True):
        if self._is_scheduled_hop:
            return
        # Для обычного hop (не recovery/connection/lost) — соблюдаем минимальный интервал
        if send_to_drone and not is_recovery:
            if time.time() - self._last_hop_time < HOP_MIN_INTERVAL:
                log.msg(f"[FS] Hop skipped: min interval {HOP_MIN_INTERVAL}s not elapsed")
                return

        self._is_scheduled_hop = True

        if action_time is None:
            action_time = self.get_action_time()
        delay = max(0, action_time - time.time())

        task.deferLater(reactor, delay, self._do_hop, target_channel)

        log_msg = f"[FS] Hop scheduled to {format_channel_freq(target_channel.freq())} in {delay:.2f}s"
        if is_recovery:
            log_msg += " (recovery)"
        if not send_to_drone:
            log_msg += " (local only, no command to drone)"
        log.msg(log_msg)

        # Отправка команды дрону (если это GS и линк доступен)
        if send_to_drone and hasattr(self.manager, 'client_f'):
            cmd = {
                "command": "freq_sel_hop",
                "action_time": action_time,
                "target_freq": target_channel.freq(),
            }
            if is_recovery:
                cmd["is_recovery"] = True

            d = self.manager.client_f.send_command(cmd)
            if d:
                d.addCallback(lambda res: log.msg(f"[FS] Hop command sent: {res}"))
                d.addErrback(lambda err: log.msg(f"[FS] Hop command failed: {err}"))

    @defer.inlineCallbacks
    def _do_hop(self, target_channel):
        try:
            current_freq = self.channels.current().freq()
            target_freq = target_channel.freq()

            if target_freq == current_freq:
                log.msg(
                    "[FS] Hop skipped: target channel is same frequency (no change)."
                )
                self._is_scheduled_hop = False
                return

            log.msg(f"[HOP START] {format_channel_freq(current_freq)} -> {format_channel_freq(target_freq)}")

            for wlan in self.manager.wlans:
                yield call_and_check_rc(
                    "iw", "dev", wlan, "set", "freq" if target_freq > 2000 else "channel", str(target_freq)
                )

            self.channels.set_current(target_channel)
            target_channel.clear_measurements()

            self._last_hop_time = time.time()

            if hasattr(self.manager, 'metrics_manager'):
                self.manager.metrics_manager.set_current_freq(target_freq)

            log.msg(f"[HOP DONE] Now on {format_channel_freq(target_freq)}")

            if self._current_state and self._current_state.name() == "connection":
                self._transition_to("normal")

        except Exception as e:
            log.msg(f"[HOP FAILED] {e}")
        finally:
            self._is_scheduled_hop = False

    def get_action_time(self, interval=1.0):
        return time.time() + interval

    def schedule_hop(self, action_time, channel=None, is_recovery=False):
        """
        Вызов с дрона при получении команды freq_sel_hop от GS.
        Планирует hop на action_time. Если channel задан — переход на него (recovery или connection hop).
        Возвращает action_time для ответа GS.
        """
        if not self.is_enabled():
            return None
        if channel is not None:
            target = channel
        else:
            target = self.channels.next_channel()
        if target:
            self._schedule_hop_to(target, is_recovery=is_recovery, action_time=action_time)
        return action_time

    def reset_all_channels_stats(self):
        log.msg("[FS] Resetting all channel statistics")
        for channel in self.channels.all():
            channel._measurements.clear()
            channel._last_packet_time = 0
            channel._score = [100]
        self._last_hop_time = time.time()
        self._is_scheduled_hop = False