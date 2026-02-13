"""
Connection Receiver - модуль для получения данных от wfb_rx
Отвечает за подключение к TCP сокету, приём и первичную обработку данных
"""
import math
import msgpack
from twisted.python import log
from twisted.internet import reactor, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver


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
            # Переполнение счётчика или сброс - берём абсолютное значение
            return current
        return diff
    
    @staticmethod
    def linear_average_snr(snr_list):
        """
        Линейное усреднение SNR в логарифмической шкале (dB)
        
        Алгоритм:
        1. Конвертация из dB в линейную область: 10^(snr/10)
        2. Усреднение в линейной области
        3. Конвертация обратно в dB: 10*log10(avg)
        
        Args:
            snr_list: список списков значений SNR в dB [[snr1, snr2], [snr3, snr4], ...]
        
        Returns:
            float: усреднённый SNR в dB, 0 если нет данных
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


class Stats(Int32StringReceiver):
    """Протокол для получения данных статистики от wfb_rx"""
    MAX_LENGTH = 1024 * 1024

    def stringReceived(self, string):
        """Обработка полученного сообщения"""
        try:
            attrs = msgpack.unpackb(string, strict_map_key=False, use_list=False, raw=False)
            
            # Проверка корректности данных
            if not isinstance(attrs, dict):
                log.msg("WARNING: Received invalid data format (not a dict)")
                return
            
            if attrs.get('type') == 'rx':
                self.factory.update(attrs)
        except Exception as e:
            log.msg(f"ERROR: Failed to process received data: {e}")


class StatsFactory(ReconnectingClientFactory):
    """
    Фабрика для управления подключением к wfb_rx и обработки данных
    Универсальный обработчик - только получает и обрабатывает данные, без привязки к конкретной логике
    """
    noisy = False
    maxDelay = 1.0

    def __init__(self, stats_callback=None):
        """
        Args:
            stats_callback: функция для передачи обработанных данных (rx_id, stats_dict)
        """
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
        """
        Обработка сырых данных от wfb_rx
        Вычисляет дельты счётчиков и формирует структуру данных
        """
        try:
            rx_id = data.get('id')
            packets = data.get('packets')
            rx_ant_stats = data.get('rx_ant_stats')
            session = data.get('session')

            # Проверка обязательных полей
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
                # Безопасный расчёт RSSI и SNR с проверками
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

                # Проверка структуры packets
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
                    # Первое измерение - берём абсолютные значения
                    p_total = packets['all'][1]
                    p_bad = packets['lost'][1] + packets['dec_err'][1]
                else:
                    # Обрабатываем "переполнение" счётчиков
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

            # Формируем словарь со статистикой
            stats_dict = {
                'p_total': p_total,
                'p_bad': p_bad,
                'rssi': rssi,
                'snr': snr
            }

            # Передаём обработанные данные через callback
            if self.stats_callback:
                try:
                    self.stats_callback(rx_id, stats_dict)
                except Exception as e:
                    log.msg(f"ERROR: Callback failed for rx_id={rx_id}: {e}")
        
        except Exception as e:
            log.msg(f"ERROR: Unexpected error in update(): {e}")
    
    def reset(self):
        """Сброс состояния (при переподключении)"""
        self._prev = {}


class DataHandler:
    """
    Главный класс для управления получением данных от wfb_rx
    Инициализирует подключение и передаёт обработанные данные через колбеки
    """
    def __init__(self, stats_port):
        """
        Инициализация DataHandler.
        """
        # Валидация порта
        if not isinstance(stats_port, int):
            raise TypeError(f"stats_port must be int, got {type(stats_port).__name__}")
        if not (1 <= stats_port <= 65535):
            raise ValueError(f"stats_port must be in range 1-65535, got {stats_port}")
        
        self.stats_port = stats_port
        
        # Список подписчиков (для множественной рассылки)
        self._callbacks = []
        self._callbacks_idents = []
        
        self._stats_factory = None
    
    def add_callback(self, callback, ident: str = None):
        """
        Добавить подписчика на данные
        """
        if (ident and ident in self._callbacks_idents) or callback in self._callbacks:
            return
        self._callbacks.append(callback)
    
    @property
    def stats_callback(self):
        """
        Обертка для совместимости - вызывает ВСЕ callbacks
        """
        def multi_callback(rx_id, stats_dict):
            for cb in self._callbacks:
                cb(rx_id, stats_dict)
        return multi_callback if self._callbacks else None
        
    def start(self):
        """
        Запуск подключения к wfb_rx
        """
        if self._stats_factory is not None:
            log.msg("WARNING: DataHandler already started")
            return
        
        self._stats_factory = StatsFactory(stats_callback=self.stats_callback)
        reactor.connectTCP("127.0.0.1", self.stats_port, self._stats_factory)
        log.msg(f"[DataHandler] Connecting to stats port {self.stats_port}")
    
    def stop(self):
        """
        Остановка подключения
        """
        if self._stats_factory:
            self._stats_factory.stopTrying()
            self._stats_factory = None
            log.msg("DataHandler: stopped")
