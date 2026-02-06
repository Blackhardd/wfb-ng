"""
Connection Metrics - модуль для вычисления метрик радиоканала
Stateless обработчик - только вычисления, без хранения состояния
"""
from dataclasses import dataclass
from .connection_receiver import Utils


# ==================== КАНАЛ ↔ МГц (как в iw dev) ====================

def channel_to_mhz(channel_or_freq: int) -> int | None:
    """
    Номер канала WiFi -> частота в МГц (как выводит `iw dev`: channel 161 (5805 MHz)).
    Если переданное значение > 2000, считается уже частотой в МГц и возвращается как есть.
    """
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
    """
    Для логов: "161 (5805 MHz)" или "5805 MHz" если уже частота.
    """
    if channel_or_freq is None:
        return "?"
    if channel_or_freq > 2000:
        return f"{channel_or_freq} MHz"
    mhz = channel_to_mhz(channel_or_freq)
    if mhz is not None:
        return f"{channel_or_freq} ({mhz} MHz)"
    return str(channel_or_freq)


@dataclass
class MeasurementStats:
    """
    Сырые данные одного измерения по потоку
    """
    p_total: int  # Всего пакетов
    p_bad: int    # Плохих пакетов (lost + dec_err)
    rssi: int     # RSSI в dBm
    snr: int      # SNR в dB


@dataclass
class ChannelMeasurements:
    """
    Коллекция измерений по типам потоков (video, mavlink, tunnel)
    """
    video: list
    mavlink: list
    tunnel: list
    
    def __init__(self):
        self.video = []
        self.mavlink = []
        self.tunnel = []
    
    def get(self, rx_id: str):
        """
        Получить список измерений по rx_id
        rx_id: идентификатор потока ('video', 'mavlink', 'tunnel')
        list: список измерений или None
        """
        if rx_id == 'video':
            return self.video
        elif rx_id == 'mavlink':
            return self.mavlink
        elif rx_id == 'tunnel':
            return self.tunnel
        return None
    
    def has(self, rx_id: str) -> bool:
        """
        Проверить наличие rx_id
        rx_id: идентификатор потока
        bool: True если rx_id существует
        """
        return rx_id in ['video', 'mavlink', 'tunnel']
    
    def append(self, rx_id: str, stats: MeasurementStats):
        """
        rx_id: идентификатор потока
        stats: данные измерения
        """
        measurements = self.get(rx_id)
        if measurements is not None:
            measurements.append(stats)
    
    def items(self):
        """
        Итератор по парам (rx_id, measurements)
        """
        return [
            ('video', self.video),
            ('mavlink', self.mavlink),
            ('tunnel', self.tunnel)
        ]
    
    def values(self):
        """
        Итератор по спискам измерений
        """
        return [self.video, self.mavlink, self.tunnel]
    
    def clear(self):
        """
        Очистить все измерения
        """
        self.video = []
        self.mavlink = []
        self.tunnel = []


# ==================== STATELESS ФУНКЦИИ ВЫЧИСЛЕНИЯ МЕТРИК ==============

def calculate_rssi(measurements: ChannelMeasurements):
    """
    Вычисление последнего усреднённого RSSI по каналу.
    measurements: коллекция измерений
    int or None: RSSI в dBm (может быть 0 или отрицательным) или None если данных нет
    """
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
    """
    Расчёт PER (Packet Error Rate) по всем rx за последние N фреймов
    measurements: коллекция измерений
    frames: количество фреймов для расчета (None = все доступные) (int: PER в процентах ) (0-100)
    """
    if frames is None or frames < 1:
        frames = 1
    
    meas_lengths = [len(meas) for meas in measurements.values() if len(meas) > 0]
    if not meas_lengths:
        return 100  # Нет измерений = 100% потерь
    
    max_frames = min(meas_lengths)
    if max_frames < frames:
        frames = max_frames

    p_total = 0
    p_bad = 0

    for rx_id, meas in measurements.items():
        # Пропускаем пустые измерения
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
        per = 100  # Нет пакетов -> 100% потерь

    return per


def calculate_snr(measurements: ChannelMeasurements, frames: int = None) -> float:
    """
    Расчёт среднего SNR по всем rx за последние N фреймов
    float: SNR в dB
    """
    if frames is None or frames < 1:
        frames = 1
    
    # Собираем только непустые измерения
    active_meas = [meas for meas in measurements.values() if len(meas) > 0]
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

    return Utils.linear_average_snr(snr_vals)


# ==================== ФУНКЦИИ ПРОВЕРКИ ДАННЫХ ==============

def has_received_data(measurements: ChannelMeasurements) -> bool:
    """
    Проверка получения данных
    """
    for meas in measurements.values():
        if len(meas) > 0:
            for stats in meas:
                if stats.p_total > 0:
                    return True
    return False


def has_any_measurements(measurements: ChannelMeasurements) -> bool:
    """
    Проверка наличия измерений
    """
    for meas in measurements.values():
        if len(meas) > 0:
            return True
    return False


# ==================== МЕНЕДЖЕР МЕТРИК ==============

class ConnectionMetricsManager:
    """
    Stateful менеджер метрик радиоканала
    По сути главное хранилище текущей связи:
    - Метрики: PER, RSSI, SNR (вычисляются)
    - Текущий: частота (устанавливается frequency_selection)
    Главное из асаны: разбить по файлам и классам,сделано,назависимый от frequency_selection и power_selection
    """
    
    def __init__(self, frames_for_calculation=10, initial_freq=None):
        self._measurements = ChannelMeasurements()  # Хранилище измерений
        self._frames = frames_for_calculation  # Количество фреймов для расчета
        
        self._last_per = None
        self._last_rssi = None
        self._last_snr = None
        self._current_freq = initial_freq  # Текущая частота канала
        self._metrics_callback = None  # Callback для уведомления StatusManager
    
    def set_metrics_callback(self, callback):
        """
        Установить callback для уведомления об обновлении метрик
        callback: функция с сигнатурой callback(per, rssi, snr)
        """
        self._metrics_callback = callback

    def connect_to(self, data_handler):
        """
        Подписаться на данные от DataHandler (формат wfb_rx).
        Преобразование stats_dict -> MeasurementStats живёт здесь, рядом с метриками.
        data_handler: объект с методом add_callback(callback), callback(rx_id, stats_dict).
        """
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
                pass  # невалидный пакет — пропускаем
        data_handler.add_callback(on_stats)

    def add_measurement(self, rx_id: str, stats: MeasurementStats):
        """
        Добавить измерение от DataHandler
        rx_id: идентификатор потока (video/mavlink/tunnel)
        stats: данные измерения (MeasurementStats)
        """
        self._measurements.append(rx_id, stats)
        
        # Ограничиваем размер хранилища
        for stream in [self._measurements.video, self._measurements.mavlink, self._measurements.tunnel]:
            if len(stream) > 100:  # Храним максимум 100 измерений
                stream[:] = stream[-100:]
        
        # Вычисляем метрики после каждого добавления
        self._calculate_and_notify()
    
    def _calculate_and_notify(self):
        """
        Вычислить метрики и уведомить подписчиков
        """
        # Проверяем что есть данные
        if not has_any_measurements(self._measurements):
            return
        
        # Вычисляем метрики используя stateless функции
        per = calculate_per(self._measurements, self._frames)
        rssi = calculate_rssi(self._measurements)
        snr = calculate_snr(self._measurements, self._frames)
        
        # Сохраняем
        self._last_per = per
        self._last_rssi = rssi
        self._last_snr = snr
        
        # Уведомляем StatusManager
        if self._metrics_callback:
            self._metrics_callback(per, rssi, snr)
    
    def get_metrics(self):
        """
        Получить последние вычисленные метрики
        {'per': int, 'rssi': int, 'snr': float} \ none
        """
        if self._last_per is None:
            return None
        
        return {
            'per': self._last_per,
            'rssi': self._last_rssi,
            'snr': self._last_snr
        }
    
    def reset(self):
        """
        Сбросить все измерения и метрики
        """
        self._measurements.clear()
        self._last_per = None
        self._last_rssi = None
        self._last_snr = None
    
    # ==================== УПРАВЛЕНИ ЧАСТОТОЙ ====================
    
    def set_current_freq(self, freq):
        """
        Установить текущую частоту канала.
        Вызывается frequency_selection после успешного hop - freq (int): Частота в MHz (например, 5220)
        """
        self._current_freq = freq
    
    def get_current_freq(self):
        """
        Получить текущую частоту канала.
        Используется power_selection, frequency_selection
        """
        return self._current_freq
