# -*- coding: utf-8 -*-
"""
WLAN helper - поиск интерфейсов и команды get/set для работы с iw.

Централизованные функции:
- find_wifi_wlan() - нахождение wlan-интерфейсов с поддерживаемыми драйверами
- get_txpower(wlans) - получить текущую TX power в dBm
- set_txpower(wlan, value) - установить TX power (value в формате драйвера: 8812au = -dBm*100, 8812eu = dBm*100)
"""
import os
import re
import subprocess

from twisted.python import log

from . import call_and_check_rc

# Драйверы, поддерживаемые wifibroadcast
SUPPORTED_DRIVERS = ("rtl88xxau_wfb", "rtl88x2eu")
SYS_NET = "/sys/class/net"

_cached_wlan_list = None


def find_wifi_wlan():
    """
    Находит wlan-интерфейсы с поддерживаемыми драйверами (rtl88xxau_wfb, rtl88x2eu).
    Вызывается из get_wlan_list() когда wlans не передан и кеш пуст (при get_txpower без аргументов).
    """
    global _cached_wlan_list
    result = []
    if not os.path.isdir(SYS_NET):
        log.msg("[WLAN] find_wifi_wlan: %s not found" % SYS_NET)
        return result
    for name in sorted(os.listdir(SYS_NET)):
        if name.startswith("."):
            continue
        path = os.path.join(SYS_NET, name)
        if not os.path.islink(path):
            continue
        driver_link = os.path.join(path, "device", "driver")
        if not os.path.islink(driver_link):
            continue
        try:
            driver = os.path.basename(os.readlink(driver_link))
            if driver in SUPPORTED_DRIVERS:
                result.append(name)
        except (OSError, ValueError):
            pass
    log.msg("[WLAN] find_wifi_wlan: found %s" % (result if result else "[]"))
    return result


def get_wlan_list(wlans=None, use_cache=True):
    """
    Возвращает список wlan-интерфейсов.
    wlans: если передан - вернёт как список
    #TODO: переписать на более понятный код
    """
    global _cached_wlan_list
    if wlans:
        return list(wlans) if not isinstance(wlans, str) else [wlans]
    if use_cache and _cached_wlan_list is not None:
        return _cached_wlan_list
    _cached_wlan_list = find_wifi_wlan()
    return _cached_wlan_list


def get_txpower(wlans=None):
    """
    Получить текущую TX power с первого wlan в dBm.
    #TODO: переписать на более понятный код
    """
    wlan_list = get_wlan_list(wlans)
    if not wlan_list:
        return None
    wlan = wlan_list[0]
    try:
        out = subprocess.check_output(
            ["iw", "dev", wlan, "info"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True
        )
        match = re.search(r"txpower\s+([-\d.]+)\s*dBm", out, re.IGNORECASE)
        return float(match.group(1)) if match else None
        # txpower 17.00 dBm
    except Exception:  # ексешпн для случая если не получилось получить мощность
        return None


def set_txpower(wlan, value):
    """
    Установить TX power для интерфейса
    value: в формате драйвера -dBm*100, 8812eu: dBm*100
    т.е использую twisted и её функцию call_and_check_rc для вызова команды iw dev wlan0 set txpower fixed 17.00
    """
    return call_and_check_rc("iw", "dev", wlan, "set", "txpower", "fixed", str(value))




def get_driver(wlan):
    """
    Получить имя драйвера для интерфейса. Returns: str или None
    """
    try:
        driver_link = os.path.join(SYS_NET, wlan, "device", "driver")
        return os.path.basename(os.readlink(driver_link))
    except (OSError, ValueError):
        return None
