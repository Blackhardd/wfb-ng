# -*- coding: utf-8 -*-
"""
Лог сохранения/синхронизации конфига в файл: /var/log/wfb-ng/drone_startup_log_дата_время.log или gs_startup_log_....
Только то, что связано с сохранением на ГС и дроне.
Один путь для всех пользователей — сервис чаще всего под root, ~/logs был бы в /root/logs и не виден при SSH.
"""
import os
import logging
from datetime import datetime

LOG_DIR = "/var/log/wfb-ng"
_startup_logger = None


def init_startup_log(profile):
    """Включить запись лога сохранения в файл. profile: "drone" или "gs"."""
    global _startup_logger
    if _startup_logger is not None:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = "drone" if profile == "drone" else "gs"
    path = os.path.join(LOG_DIR, "%s_startup_log_%s.log" % (name, now))
    _startup_logger = logging.getLogger("wfb_ng.startup")
    _startup_logger.setLevel(logging.INFO)
    _startup_logger.handlers.clear()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _startup_logger.addHandler(fh)


def log_startup(msg):
    """Записать строку в лог сохранения (файл /var/log/wfb-ng/...)."""
    if _startup_logger is not None:
        _startup_logger.info(msg)
