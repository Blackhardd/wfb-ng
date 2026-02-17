"""
sich_connection - модуль для получения данных от wfb_rx и вычисления метрик радиоканала
Объединяет: подключение к TCP, приём данных, расчёт PER/RSSI/SNR
"""
import math
import msgpack
from dataclasses import dataclass
from twisted.python import log
from twisted.internet import reactor
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver


# ==================== УТИЛИТЫ ====================

class Utils:
    """Утилиты для работы со счётчиками и данными"""

    @staticmethod
    def clamp(n, min_val, max_val):
        """Ограничение значения в диапазоне [min_val, max_val]"""
        return max(min_val, min(n, max_val))

    @staticmethod
    def safe_counter_diff(current, previous):
        """
        Безопасная разница счётчиков с защитой от переполнения

        Args:
            current: текущее значение счётчика
            previous: предыдущее значение счётчика

        Returns:
            int: разница счётчиков
        """
        diff = current - previous
        if diff < 0:
            return current
        return diff

    @staticmethod
    def linear_average_snr(snr_list):
        """
        Линейное усреднение SNR в логарифмической шкале (dB)
        """
        if not snr_list or not snr_list[0]:
            return 0
        s = 0.0
        n = 0
        for row in snr_list:
            for snr_db in row:
                if snr_db > 0:
                    s += 10 ** (snr_db / 10)
                    n += 1
        if n == 0:
            return 0
        avg_lin = s / n
        return 10.0 * math.log10(avg_lin)


# ==================== ПРИЁМ ДАННЫХ ОТ WFB_RX ====================

class Stats(Int32StringReceiver):
    """Протокол для получения данных статистики от wfb_rx"""
    MAX_LENGTH = 1024 * 1024

    def stringReceived(self, string):
        try:
            attrs = msgpack.unpackb(string, strict_map_key=False, use_list=False, raw=False)
            if not isinstance(attrs, dict):
                log.msg("WARNING: Received invalid data format (not a dict)")
                return
            if attrs.get('type') == 'rx':
                self.factory.update(attrs)
        except Exception as e:
            log.msg(f"ERROR: Failed to process received data: {e}")


class StatsFactory(ReconnectingClientFactory):
    """Фабрика для управления подключением к wfb_rx"""
    noisy = False
    maxDelay = 1.0

    def __init__(self, stats_callback=None):
        ReconnectingClientFactory.__init__(self)
        self.stats_callback = stats_callback
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
        try:
            rx_id = data.get('id')
            packets = data.get('packets')
            rx_ant_stats = data.get('rx_ant_stats')
            session = data.get('session')

            if rx_id is None:
                log.msg("WARNING: Received data without 'id' field")
                return
            if packets is None:
                log.msg(f"WARNING: Received data for rx_id={rx_id} without 'packets' field")
                return

            p_total = 0
            p_bad = 0
            rssi = 0
            snr = 0

            rx_id = str(rx_id).replace(" rx", "")

            if session is not None:
                try:
                    if rx_ant_stats and isinstance(rx_ant_stats, dict) and len(rx_ant_stats) > 0:
                        rssi_values = [v[2] for v in rx_ant_stats.values() if isinstance(v, (list, tuple)) and len(v) > 2]
                        snr_values = [v[5] for v in rx_ant_stats.values() if isinstance(v, (list, tuple)) and len(v) > 5]
                        if rssi_values:
                            rssi = int(round(sum(rssi_values) / len(rssi_values)))
                        if snr_values:
                            snr = int(round(sum(snr_values) / len(snr_values)))
                except (KeyError, IndexError, TypeError, ZeroDivisionError) as e:
                    log.msg(f"WARNING: Error calculating RSSI/SNR for rx_id={rx_id}: {e}")

                if not isinstance(packets, dict):
                    log.msg(f"WARNING: Invalid packets structure for rx_id={rx_id}")
                    return

                required_keys = ['all', 'lost', 'dec_err']
                for key in required_keys:
                    if key not in packets:
                        log.msg(f"WARNING: Missing '{key}' in packets for rx_id={rx_id}")
                        return
                    if not isinstance(packets[key], (list, tuple)) or len(packets[key]) < 2:
                        log.msg(f"WARNING: Invalid format for packets['{key}'] for rx_id={rx_id}")
                        return

                if rx_id not in self._prev:
                    p_total = packets['all'][1]
                    p_bad = packets['lost'][1] + packets['dec_err'][1]
                else:
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

            stats_dict = {
                'p_total': p_total,
                'p_bad': p_bad,
                'rssi': rssi,
                'snr': snr
            }

            if self.stats_callback:
                try:
                    self.stats_callback(rx_id, stats_dict)
                except Exception as e:
                    log.msg(f"ERROR: Callback failed for rx_id={rx_id}: {e}")

        except Exception as e:
            log.msg(f"ERROR: Unexpected error in update(): {e}")

    def reset(self):
        self._prev = {}


class DataHandler:
    """Главный класс для управления получением данных от wfb_rx"""
    def __init__(self, stats_port):
        if not isinstance(stats_port, int):
            raise TypeError(f"stats_port must be int, got {type(stats_port).__name__}")
        if not (1 <= stats_port <= 65535):
            raise ValueError(f"stats_port must be in range 1-65535, got {stats_port}")
        self.stats_port = stats_port
        self._callbacks = []
        self._callbacks_idents = []
        self._stats_factory = None

    def add_callback(self, callback, ident: str = None):
        if (ident and ident in self._callbacks_idents) or callback in self._callbacks:
            return
        self._callbacks.append(callback)

    @property
    def stats_callback(self):
        def multi_callback(rx_id, stats_dict):
            for cb in self._callbacks:
                cb(rx_id, stats_dict)
        return multi_callback if self._callbacks else None

    def start(self):
        if self._stats_factory is not None:
            log.msg("WARNING: DataHandler already started")
            return
        self._stats_factory = StatsFactory(stats_callback=self.stats_callback)
        reactor.connectTCP("127.0.0.1", self.stats_port, self._stats_factory)
        log.msg(f"[DataHandler] Connecting to stats port {self.stats_port}")

    def stop(self):
        if self._stats_factory:
            self._stats_factory.stopTrying()
            self._stats_factory = None
            log.msg("DataHandler: stopped")


# ==================== КАНАЛ ↔ МГц ====================

def channel_to_mhz(channel_or_freq: int) -> int | None:
    """Номер канала WiFi -> частота в МГц"""
    if channel_or_freq is None:
        return None
    if channel_or_freq > 2000:
        return channel_or_freq
    ch = channel_or_freq
    if 1 <= ch <= 14:
        return 2412 + (ch - 1) * 5
    if 36 <= ch <= 64:
        return 5180 + (ch - 36) * 5
    if 100 <= ch <= 144:
        return 5500 + (ch - 100) * 5
    if 149 <= ch <= 177:
        return 5745 + (ch - 149) * 5
    return None


def format_channel_freq(channel_or_freq: int) -> str:
    """Для логов: "161 (5805 MHz)" или "5805 MHz" """
    if channel_or_freq is None:
        return "?"
    if channel_or_freq > 2000:
        return f"{channel_or_freq} MHz"
    mhz = channel_to_mhz(channel_or_freq)
    if mhz is not None:
        return f"{channel_or_freq} ({mhz} MHz)"
    return str(channel_or_freq)


# ==================== ДАННЫЕ И МЕТРИКИ ====================

@dataclass
class MeasurementStats:
    """Сырые данные одного измерения по потоку"""
    p_total: int
    p_bad: int
    rssi: int
    snr: int


@dataclass
class ChannelMeasurements:
    """Коллекция измерений по типам потоков (video, mavlink, tunnel)"""
    video: list
    mavlink: list
    tunnel: list

    def __init__(self):
        self.video = []
        self.mavlink = []
        self.tunnel = []

    def get(self, rx_id: str):
        if rx_id == 'video':
            return self.video
        elif rx_id == 'mavlink':
            return self.mavlink
        elif rx_id == 'tunnel':
            return self.tunnel
        return None

    def has(self, rx_id: str) -> bool:
        return rx_id in ['video', 'mavlink', 'tunnel']

    def append(self, rx_id: str, stats: MeasurementStats):
        measurements = self.get(rx_id)
        if measurements is not None:
            measurements.append(stats)

    def items(self):
        return [
            ('video', self.video),
            ('mavlink', self.mavlink),
            ('tunnel', self.tunnel)
        ]

    def values(self):
        return [self.video, self.mavlink, self.tunnel]

    def clear(self):
        self.video = []
        self.mavlink = []
        self.tunnel = []


def calculate_rssi(measurements: ChannelMeasurements):
    rssi_list = []
    for meas in measurements.values():
        if len(meas) > 0:
            last_rssi = meas[-1].rssi
            if last_rssi is not None:
                rssi_list.append(last_rssi)
    if not rssi_list:
        return None
    return int(round(sum(rssi_list) / len(rssi_list)))


def calculate_per(measurements: ChannelMeasurements, frames: int = None) -> int:
    if frames is None or frames < 1:
        frames = 1
    meas_lengths = [len(meas) for meas in measurements.values() if len(meas) > 0]
    if not meas_lengths:
        return 100
    max_frames = min(meas_lengths)
    if max_frames < frames:
        frames = max_frames

    p_total = 0
    p_bad = 0

    for rx_id, meas in measurements.items():
        if len(meas) == 0:
            continue
        for stats in meas[-frames:]:
            if stats.p_total > 0:
                p_total += stats.p_total
                p_bad += stats.p_bad

    if p_total > 0:
        p_bad = min(p_bad, p_total)
        per = round((p_bad / p_total) * 100)
        per = Utils.clamp(per, 0, 100)
    else:
        per = 100

    return per


def calculate_snr(measurements: ChannelMeasurements, frames: int = None) -> float:
    if frames is None or frames < 1:
        frames = 1
    active_meas = [meas for meas in measurements.values() if len(meas) > 0]
    if not active_meas:
        return 0
    max_frames = min(len(meas) for meas in active_meas)
    if frames > max_frames:
        frames = max_frames

    snr_vals = []
    for meas in active_meas:
        window = [stats.snr for stats in meas[-frames:] if stats.snr != 0]
        if window:
            snr_vals.append(window)

    if not snr_vals:
        return 0

    return Utils.linear_average_snr(snr_vals)


def has_received_data(measurements: ChannelMeasurements) -> bool:
    for meas in measurements.values():
        if len(meas) > 0:
            for stats in meas:
                if stats.p_total > 0:
                    return True
    return False


def has_any_measurements(measurements: ChannelMeasurements) -> bool:
    for meas in measurements.values():
        if len(meas) > 0:
            return True
    return False


class ConnectionMetricsManager:
    """Менеджер метрик радиоканала (PER, RSSI, SNR)"""

    def __init__(self, frames_for_calculation=10, initial_freq=None):
        self._measurements = ChannelMeasurements()
        self._frames = frames_for_calculation
        self._last_per = None
        self._last_rssi = None
        self._last_snr = None
        self._current_freq = initial_freq
        self._metrics_callback = None

    def set_metrics_callback(self, callback):
        self._metrics_callback = callback

    def connect_to(self, data_handler):
        def on_stats(rx_id, stats_dict):
            try:
                stats = MeasurementStats(
                    p_total=int(stats_dict.get('p_total', 0)),
                    p_bad=int(stats_dict.get('p_bad', 0)),
                    rssi=int(stats_dict.get('rssi', 0)),
                    snr=int(stats_dict.get('snr', 0)),
                )
                self.add_measurement(rx_id, stats)
            except (TypeError, ValueError, KeyError):
                pass
        data_handler.add_callback(on_stats)

    def add_measurement(self, rx_id: str, stats: MeasurementStats):
        self._measurements.append(rx_id, stats)
        for stream in [self._measurements.video, self._measurements.mavlink, self._measurements.tunnel]:
            if len(stream) > 100:
                stream[:] = stream[-100:]
        self._calculate_and_notify()

    def _calculate_and_notify(self):
        if not has_any_measurements(self._measurements):
            return
        per = calculate_per(self._measurements, self._frames)
        rssi = calculate_rssi(self._measurements)
        snr = calculate_snr(self._measurements, self._frames)
        self._last_per = per
        self._last_rssi = rssi
        self._last_snr = snr
        if self._metrics_callback:
            self._metrics_callback(per, rssi, snr)

    def get_metrics(self):
        if self._last_per is None:
            return None
        return {
            'per': self._last_per,
            'rssi': self._last_rssi,
            'snr': self._last_snr
        }

    def reset(self):
        self._measurements.clear()
        self._last_per = None
        self._last_rssi = None
        self._last_snr = None

    def set_current_freq(self, freq):
        self._current_freq = freq

    def get_current_freq(self):
        return self._current_freq
