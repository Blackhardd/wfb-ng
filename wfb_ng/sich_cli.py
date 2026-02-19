# -*- coding: utf-8 -*-
"""
Минимальный CLI на Curses для просмотра heartbeat ГС/Дрон.
Запуск: sich-cli hb
"""
import argparse
import curses
import json
import select
import socket
import sys
from dataclasses import dataclass
from datetime import datetime

from .sich_heartbeat import (
    HEARTBEAT_STATS_PORT_RECEIVED,
    HEARTBEAT_STATS_PORT_SENT,
)


def _val(v):
    return v if v is not None else "n/a"


def _format_ts(v):
    """Если v — число (Unix sec или ms), вернуть '(DD.MM.YYYY HH:MM:SS) v', иначе _val(v)."""
    if v is None:
        return "n/a"
    try:
        t = float(v)
    except (TypeError, ValueError):
        return _val(v)
    if t >= 1e12:  # миллисекунды
        t = t / 1000.0
    try:
        dt = datetime.fromtimestamp(t)
        decoded = dt.strftime("%d.%m.%Y %H:%M:%S")
        return "(%s) %s" % (decoded, v)
    except (OSError, ValueError):
        return _val(v)


# ---------------------------------------------------------------------------
# Структура JSON heartbeat 
# ---------------------------------------------------------------------------
# Пример:
#   type, timestamp, status, local { rssi, per, snr }, remote { timestamp, status, rssi, per, snr, score }, score
# ---------------------------------------------------------------------------


def get_path(obj, path, default=None):
    """
    Взять значение из dict по пути через точку. Одно место для доступа к полям JSON.
    Пример: get_path(raw_received, "remote.status") -> "connected"
    """
    if not obj or not path:
        return default
    for key in path.strip().split("."):
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


# ---------------------------------------------------------------------------
# Класс-форматтер: через него пропускаем данные, здесь кастомизируешь вид
# ---------------------------------------------------------------------------


class HeartbeatBlock:
    """
    Блок данных для одной колонки. Данные в self.data (исходный JSON)
    append("Статус пира: %s" % status)
    """
    def __init__(self, data):
        self.data = data if isinstance(data, dict) else {}

    def to_lines(self, prefix=""):
        """Вернуть список строк."""
        return self._dict_to_lines(self.data, prefix)

    def _dict_to_lines(self, d, prefix=""):
        if not d or not isinstance(d, dict):
            return [prefix + "n/a"]
        out = []
        for k, v in d.items():
            if isinstance(v, dict):
                out.append(prefix + "%s:" % k)
                out.extend(self._dict_to_lines(v, prefix + "  "))
            else:
                out.append(prefix + "%s: %s" % (k, _val(v)))
        return out


# ---------------------------------------------------------------------------
# Модель отображения: слева - что отправляем, справа - что получаем
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatDisplay:
    """
    Слева - что отправляем дрону, справа - что получаем от дрона.
    Данные проходят через HeartbeatBlock 
    """
    # Данные задаются в build_display(). Источники:
    raw_sent: dict          # raw_sent     - порт 14893 (то, что мы отправили)
    raw_received: dict      # raw_received - порт 14892 (то, что получили от дрона)

    # --- Левая колонка: что мы отправляем дрону - достаём из JSON что нужно через get_path
    def left_column_lines(self):
        d = self.raw_sent
        out = ["Graund Station -> отправляем на Drone", "-" * 20]
        out.append("Status: %s" % _val(get_path(d, "status")))
        out.append("Channel: %s" % _val(get_path(d, "channel")))
        out.append("Score: %s" % _val(get_path(d, "score")))
        out.append("L.Timestamp: %s" % _format_ts(get_path(d, "local.timestamp")))
        out.append("L.RSSI: %s" % _val(get_path(d, "local.rssi")))
        out.append("L.PER: %s" % _val(get_path(d, "local.per")))
        out.append("L.SNR: %s" % _val(get_path(d, "local.snr")))
        out.append("Получили от Drone, возвращаем ")
        out.append("-" * 20)
        out.append("r.Timestamp: %s" % _format_ts(get_path(d, "remote.timestamp")))
        out.append("r.Status: %s" % _val(get_path(d, "remote.status")))
        out.append("r.Channel: %s" % _val(get_path(d, "remote.channel")))
        out.append("r.RSSI: %s" % _val(get_path(d, "remote.rssi")))
        out.append("r.PER: %s" % _val(get_path(d, "remote.per")))
        out.append("r.SNR: %s" % _val(get_path(d, "remote.snr")))
        out.append("r.Score: %s" % _val(get_path(d, "remote.score")))
        return out

    def footer_line(self):
        """Одна строка в самом низу окна: локальный таймстамп (из отправленных данных)."""
        ts = _format_ts(get_path(self.raw_sent, "timestamp"))
        return "Локальный таймстамп: %s" % ts

    # --- Правая колонка: что получаем от дрона - то же самое из raw_received. ---
    def right_column_lines(self):
        d = self.raw_received
        out = ["Получаем от Drone", "-" * 20]
        out.append("Status: %s" % _val(get_path(d, "status")))
        out.append("Channel: %s" % _val(get_path(d, "channel")))
        out.append("Score: %s" % _val(get_path(d, "score")))
        out.append("L.Timestamp: %s" % _format_ts(get_path(d, "local.timestamp")))
        out.append("L.RSSI: %s" % _val(get_path(d, "local.rssi")))
        out.append("L.PER: %s" % _val(get_path(d, "local.per")))
        out.append("L.SNR: %s" % _val(get_path(d, "local.snr")))
        out.append("Получили от Ground-Station, возвращаем")
        out.append("-" * 20)
        out.append("r.Timestamp: %s" % _format_ts(get_path(d, "remote.timestamp")))
        out.append("r.Status: %s" % _val(get_path(d, "remote.status")))
        out.append("r.Channel: %s" % _val(get_path(d, "remote.channel")))
        out.append("r.RSSI: %s" % _val(get_path(d, "remote.rssi")))
        out.append("r.PER: %s" % _val(get_path(d, "remote.per")))
        out.append("r.SNR: %s" % _val(get_path(d, "remote.snr")))
        out.append("r.Score: %s" % _val(get_path(d, "remote.score")))
        return out


# ---------------------------------------------------------------------------
# Единое место откуда берутся данные для левой и правой секций
# ---------------------------------------------------------------------------


def build_display(sent_msg, received_msg):
    """
    Единая точка: откуда данные для колонок.
    - Левая колонка: sent_msg - то, что мы отправили. Источник порт 14893 
    - Правая колонка: received_msg - то, что получили от дрона. Источник порт 14892
    """
    return HeartbeatDisplay(
        raw_sent=sent_msg or {},
        raw_received=received_msg or {},
    )


# Отступ по контуру окна (сверху, снизу, слева, справа — 1 символ)
BORDER_MARGIN = 1
# Падинги внутри рамки
PAD_LEFT = 2
PAD_BETWEEN = 2
PAD_RIGHT = 2


def _draw_table(stdscr, display):
    """
    Разметка - левая и правая колонки 50%/50%. Падинги задаются константами выше
    """
    try:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        m = BORDER_MARGIN
        # Внутренняя область: между левой "|" и правой "|"
        inner_left = m + 1
        inner_right = w - m - 2
        inner_width = max(0, inner_right - inner_left + 1)
        mid = inner_left + inner_width // 2  # здесь рисуем "|" между колонками
        left_x = inner_left + PAD_LEFT
        left_width = max(0, mid - left_x - 1)   # минус 1 под "|"
        right_x = mid + 1
        right_width = max(0, inner_right - right_x - PAD_RIGHT + 1)
        # y=0 margin, y=1 верхняя _ _ _, y=2.. контент, затем нижняя _ _ _, затем margin
        content_y_start = 2
        max_content_rows = max(0, h - 5)  # минус 1 строка под футер "Локальный таймстамп"

        left_lines = display.left_column_lines()
        right_lines = display.right_column_lines()
        num_rows = min(max(len(left_lines), len(right_lines), 1), max_content_rows)

        # Верхняя/нижняя граница: _ _ _ _ _ _ _ (на всю ширину внутри отступов)
        border_len = w - 2 * m
        top_bottom_line = ("_ " * (border_len // 2 + 1))[:border_len]
        try:
            stdscr.addstr(m, m, top_bottom_line)
        except curses.error:
            pass

        for i in range(num_rows):
            y = content_y_start + i
            try:
                stdscr.addstr(y, m, "|")
                stdscr.addstr(y, mid, "|")           # линия между левой и правой колонкой
                stdscr.addstr(y, w - m - 1, "|")
            except curses.error:
                pass
            left_s = (left_lines[i][:left_width] if i < len(left_lines) else "").replace("\n", " ")
            right_s = (right_lines[i][:right_width] if i < len(right_lines) else "").replace("\n", " ")
            try:
                if left_s:
                    stdscr.addstr(y, left_x, left_s)
                if right_s:
                    stdscr.addstr(y, right_x, right_s)
            except curses.error:
                pass

        try:
            stdscr.addstr(content_y_start + num_rows, m, top_bottom_line)
        except curses.error:
            pass

        # В самый низ окна — футер с локальным таймстампом
        footer_y = content_y_start + num_rows + 1
        if footer_y < h - m:
            footer_text = (display.footer_line() or "")[:inner_width].replace("\n", " ")
            try:
                stdscr.addstr(footer_y, inner_left, footer_text)
            except curses.error:
                pass

        stdscr.refresh()
    except curses.error:
        pass


def _parse_heartbeat(data):
    try:
        msg = json.loads(data.decode())
        if msg.get("type") != "heartbeat":
            return None
        return msg
    except Exception:
        return None


def _run_hb(stdscr, once):
    sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock_recv.bind(("", HEARTBEAT_STATS_PORT_RECEIVED))
        sock_send.bind(("", HEARTBEAT_STATS_PORT_SENT))
    except OSError:
        sock_recv.close()
        sock_send.close()
        raise
    curses.curs_set(0)
    REFRESH_INTERVAL = 0.5
    last_sent = None
    last_received = None
    last_display = None
    try:
        while True:
            ready, _, _ = select.select([sock_recv, sock_send], [], [], REFRESH_INTERVAL)
            if not ready:
                if last_display is not None:
                    _draw_table(stdscr, last_display)
                else:
                    try:
                        stdscr.erase()
                        stdscr.addstr(0, 0, "Waiting for heartbeat (ports %d, %d)... Ctrl+C to stop." % (
                            HEARTBEAT_STATS_PORT_RECEIVED, HEARTBEAT_STATS_PORT_SENT))
                        stdscr.refresh()
                    except curses.error:
                        pass
                continue
            for sock in ready:
                data, _ = sock.recvfrom(4096)
                msg = _parse_heartbeat(data)
                if not msg:
                    continue
                if sock is sock_recv:
                    last_received = msg
                else:
                    last_sent = msg
            display = build_display(last_sent, last_received)
            last_display = display
            _draw_table(stdscr, display)
            if once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock_recv.close()
        sock_send.close()
    if once and last_display is None:
        return 1
    return 0


def run_hb(once):
    try:
        rc = curses.wrapper(lambda stdscr: _run_hb(stdscr, once))
    except OSError as e:
        print("Bind error: %s" % e, file=sys.stderr)
        return 1
    if rc == 1 and once:
        print("No heartbeat received.", file=sys.stderr)
    return rc if rc is not None else 0


def main():
    parser = argparse.ArgumentParser(description="SICH CLI (heartbeat)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    hb = sub.add_parser("hb", help="Left: what we send (to drone), Right: what we receive (from drone)")
    hb.add_argument("--once", action="store_true", help="Print one packet and exit")
    args = parser.parse_args()
    if args.cmd == "hb":
        return run_hb(getattr(args, "once", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
