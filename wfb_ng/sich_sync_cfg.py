"""
Синхронизация конфига дрон <-> ГС при первом переходе в connected после старта/перезагрузки дрона.
Один раз за сессию (флаг _is_start): сравнение хеша конфига, при отличии — дрон запрашивает конфиг с ГС.
"""
import hashlib
import json
from twisted.python import log

from .conf import station_settings


# добавил 18.02.2026 для попытки сделать синхронизацию конфинга между ГС

def _settings_to_dict(station_settings_obj):
    """Сериализация настроек из cfg в dict для передачи и хеширования что бы потом я мог сравнить с гс для синхронизации"""
    out = {}  # пустая коробка, наполню позже
    for section_name, section_obj in station_settings_obj.__dict__.items():
        # path — служебная секция (cfg_root и т.п.), не синхронизируем
        if section_name == "path" or not hasattr(section_obj, "__dict__"):  # тут мне АИ подсказала что надо проверять системный путь что бы не было ошибки path - служебная секция (cfg_root и т.п.)
            continue  # если это служебная секция или нет дикта , то пропускаем
        out[section_name] = {}  # создаю пустую коробку для секции с которой буду работать
        for property_name, property_value in section_obj.__dict__.items(): # проверяю по очереди все параметры секции
            if isinstance(property_value, (str, int, float, bool, type(None))): #встроенной питоновской функцией проверяю значения т.е они стандартные типы или нет
                out[section_name][property_name] = property_value #после проверки - добавляю в "коробку" т.е дикт out
            else:
                out[section_name][property_name] = str(property_value) # если не стандартный тип, то приводим к строке
    return out


def get_config_hash():
    """Хеш текущего конфига (station_settings) для сравнения."""
    # Беру hash по содержимому локального station_settings (дрон или ГС- один и тот же тип настроек так что по барабану).
    config_dict = _settings_to_dict(station_settings)
    raw = json.dumps(config_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_config_as_dict():
    """Текущий конфиг в виде dict для отправки (с ГС на дрон)."""
    return _settings_to_dict(station_settings)


class SyncCfgOnConnect:
    """
    Вызываю при переходе в статус connected.
    По сути задача сделать за сессию только один раз _is_start флаг из None в булевое значение т.е True 
    И потом если вдруг у меня снова разрыв связи, через проверку флага мы не будем повторно вызывать
    Синхронизацию конфинга, это основное что было надо.
    Если есть какие то отличия в hash дрону и hash гс - дрон запрашивает полный конфиг и применяет update_config.
    """

    def __init__(self):
        self._is_start = None # пустая коробка же, жду булевое значение после первого конекта

    def on_entered_connected(self, manager): # вызываю при переходе в статус connected
        if self._is_start is not None: # если фалг не отсуствует, то пропускаем
            return
        if manager.get_type() != "drone": # проверяю тип устройство, дрон или не дрон
            return 
        client_f = getattr(manager, "client_f", None) # вызываю клиент связи ТСП
        if not client_f or not getattr(client_f, "send_command", None): # подсказала АИ после ошибки 
            return
        # отправляю запрос на получение хеша конфига с ГС, получаю Deferred (обещание получить ответ асинхронно)
        hash_deferred = client_f.send_command({"request": "get_config_hash"}) # тут я отправляю запрос на получение хеша с ГС
        if hash_deferred is None: # если нет хеша то пропускаем синхронизацию,т.е что бы не было ошибки
            log.msg("[SyncCfg] Connection not ready, skip sync this time")
            return
        hash_deferred.addCallback(self._on_hash_response, manager) # добавляю callback для получения ответа от ГС
        hash_deferred.addErrback(self._on_sync_error) # добавляю errorback для обработки ошибок

    def _on_hash_response(self, response, manager): # вызываю при получении ответа от ГС по сути
        if response.get("status") != "ok": # у нас в скриптах этот статус ок, я не стал изменять условный подход, оставил как есть 
            log.msg("[SyncCfg] get_config_hash failed: %s" % response)
            self._is_start = True # тут я устанавливаю флаг в True что бы не было ошибки
            return
        gs_hash = response.get("config_hash")
        local_hash = get_config_hash() # получаю хеш конфига с ГС
        if gs_hash == local_hash: # если хеши совпадают, то пропускаем синхронизацию!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            log.msg("[SyncCfg] Config hash match, no sync needed")
            self._is_start = True # тут я устанавливаю флаг втру ч
            return
        log.msg("[SyncCfg] Config hash mismatch (local=%s, gs=%s), requesting full config" % (local_hash[:8], (gs_hash or "")[:8]))
        # Deferred, который мы получаем при отправке команды на запрос конфига у ГС, меняем название на config_request_deferred чтобы было понятно
        config_request_deferred = manager.client_f.send_command({"request": "get_config"})
        if config_request_deferred is None: # если нет конфига то пропускаем синхронизацию,т.е что бы не было ошибки
            self._is_start = True # тут я устанавливаю флаг в True что бы не было ошибки
            return
        config_request_deferred.addCallback(self._on_config_response, manager) # добавляю callback для получения ответа от ГС
        config_request_deferred.addErrback(self._on_sync_error) # добавляю errorback для обработки ошибок

    def _on_config_response(self, response, manager): # вызываю при получении ответа от ГС по сути
        if response.get("status") != "ok": # у нас в скриптах этот статус ок, я не стал изменять условный подход, оставил как есть 
            log.msg("[SyncCfg] get_config failed: %s" % response)
            self._is_start = True # тут я устанавливаю флаг в True что бы не было ошибки
            return
        config = response.get("config")
        if not config or not isinstance(config, dict): # если нет конфига или нет дикта, то пропускаем синхронизацию,т.е что бы не было ошибки
            log.msg("[SyncCfg] Invalid config in response")
            self._is_start = True # тут я устанавливаю флаг в True что бы не было ошибки
            return
        try:
            manager.update_config(config) # обновляю конфиг на дроне
            log.msg("[SyncCfg] Config synced from GS")
        except Exception as e:
            log.msg("[SyncCfg] update_config error: %s" % e)
        self._is_start = True # тут я устанавливаю флаг в True что бы не было ошибки

    def _on_sync_error(self, err):
        log.msg("[SyncCfg] Sync error: %s" % err) # вызываю при ошибке синхронизации
        self._is_start = True
