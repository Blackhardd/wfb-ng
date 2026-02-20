#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2018-2024 Vasily Evseenko <svpcom@p2ptech.org>

#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; version 3.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License along
#   with this program; if not, write to the Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import configparser
import ast
import copy
import glob
import os
import json
import re

from twisted.python import log


class ConfigError(Exception):
    pass


class Settings(object):
    # Used for interpolation of string values
    def __getitem__(self, attr):
        try:
            section, attr = attr.split('.')
        except ValueError:
            raise KeyError(attr)

        return getattr(getattr(self, section), attr)

    def __repr__(self):
        return repr(self.__dict__)

    def __deepcopy__(self, memo):
        return copy.deepcopy(self.__dict__, memo)
    
    def _to_ini_literal(self, value):
        if value is None or value == "None":
            return None
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        return repr(value)
    
    def _write(self, fp):
        config = configparser.ConfigParser()
        for section_name, section_obj in self.__dict__.items():
            if section_name == "path":
                continue

            items = {}
            for key, value in section_obj.__dict__.items():
                value = self._to_ini_literal(value)
                if value is not None:
                    items[key] = value
            if items:
                config[section_name] = items
        config.write(fp)

    def has_section(self, section_name):
        return hasattr(self, section_name)

    def add_section(self, section_name):
        setattr(self, section_name, Section())

    def get_section(self, section_name):
        return getattr(self, section_name)

    def _get_all_items(self):
        """Возвращает dict: (section_name, key) -> value для всех настроек."""
        items = {}
        for section_name, section_obj in self.__dict__.items():
            if section_name == "path" or not hasattr(section_obj, "__dict__"):
                continue
            for key, value in section_obj.__dict__.items():
                items[(section_name, key)] = value
        return items

    def save_to_file(self, fpath):
        """Сохраняет конфиг. Если файл существует — обновляет только значения, сохраняя комментарии и структуру."""
        os.makedirs(os.path.dirname(fpath) or ".", exist_ok=True)
        all_items = self._get_all_items()
        if os.path.exists(fpath):
            self._save_merged(fpath, all_items)
        else:
            tmp = f"{fpath}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                self._write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, fpath)

    def _save_merged(self, fpath, all_items):
        """Обновляет значения в файле, сохраняя комментарии и структуру."""
        key_val_re = re.compile(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*=\s*)(.*)$')
        current_section = None
        lines_out = []
        replaced = set()  # (section, key)
        last_key_line = {}  # section -> index в lines_out
        last_section_header = {}  # section -> index (для секций без ключей)
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped[1:-1].strip()
                    last_section_header[current_section] = len(lines_out)
                    lines_out.append(line)
                    continue
                m = key_val_re.match(line)
                if m and current_section is not None:
                    indent, key, eq_part, old_val = m.groups()
                    k = (current_section, key)
                    if k in all_items:
                        formatted = self._to_ini_literal(all_items[k])
                        if formatted is not None:
                            lines_out.append("%s%s%s%s\n" % (indent, key, eq_part, formatted))
                            replaced.add(k)
                        else:
                            lines_out.append(line)
                        last_key_line[current_section] = len(lines_out) - 1
                    else:
                        lines_out.append(line)
                else:
                    lines_out.append(line)
        # Добавляем ключи из all_items, которых не было в файле
        missing_by_section = {}
        for (sec, key) in all_items:
            if (sec, key) not in replaced:
                missing_by_section.setdefault(sec, []).append(key)
        inserts = []  # (index, [lines])
        for sec, keys in missing_by_section.items():
            insert_pos = last_key_line.get(sec, last_section_header.get(sec, -1)) + 1
            new_lines = ["%s = %s\n" % (k, self._to_ini_literal(all_items[(sec, k)])) for k in keys]
            if insert_pos == 0 and sec not in last_section_header:
                new_lines = ["[%s]\n" % sec] + new_lines
            inserts.append((insert_pos, new_lines))
        for pos, new_lines in sorted(inserts, key=lambda x: -x[0]):
            lines_out[pos:pos] = new_lines
        tmp = f"{fpath}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines_out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fpath)


class Section(object):
    def __repr__(self):
        return repr(self.__dict__)

    def __deepcopy__(self, memo):
        return copy.deepcopy(self.__dict__, memo)
    
    def set(self, name, value):
        setattr(self, name, value)


def parse_config(basedir, cfg_patterns, interpolate=True):
    settings = Settings()
    settings.path = Section()
    settings.path.cfg_root = basedir
    settings.common = Section()

    used_files = []

    for g in cfg_patterns:
        for f in (glob.glob(os.path.join(basedir, g)) if isinstance(g, str) else [g]):
            fd = open(f) if isinstance(f, str) else f
            filename = getattr(fd, 'name', str(fd))

            try:
                fd.seek(0) # handle case when source config is fd
                config = configparser.RawConfigParser(strict=False)

                try:
                    config.read_file(fd, source=filename)
                except Exception as v:
                    raise ConfigError(v)

                used_files.append(filename)
                fd.seek(0)

                for section in config.sections():
                    _s = getattr(settings, section, Section())

                    for item, value in config.items(section):
                        try:
                            value = ast.literal_eval(value)
                            if interpolate and isinstance(value, str):
                                # Interpolate string using current settings
                                value = value % settings
                        except:
                            err = '[%s] %s = %s' % (section, item, value)
                            log.msg('Config parse error: %s' % (err,), isError=1)
                            raise ConfigError('Parse error: %s' % (err,))

                        setattr(_s, item, value)

                    s_name=str(section)
                    setattr(settings, s_name, _s)
            finally:
                if isinstance(f, str):
                    fd.close()

    return settings, used_files